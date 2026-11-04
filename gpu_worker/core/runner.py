"""Worker 主循环：领任务 → 下载 → 渲染 → 回传 → 上报。

可靠性要点（对应 docs/product/算力部署方案.md §4）：
  · 心跳线程独立于渲染进程：LaMa 跑 90 秒不出声也不会被误判为失联；
  · 心跳一旦收到 lease_lost，立刻 abort 本地渲染 —— 否则算力白烧还可能写坏同一个对象；
  · 渲染阶段失败按「可重试/不可重试」分流，OOM 这类直接判不可重试，不耗三次机会；
  · 无论成败都清理工作区（合规承诺：素材处理完即删）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from .client import CloudClient, CloudError, LeaseLost
from .models import LocalTask, StageLog
from .pipeline import Aborted, Pipeline, PipelineError

logger = logging.getLogger(__name__)


class _Heartbeat(threading.Thread):
    """独立心跳线程。持有进度快照，定期续租。"""

    def __init__(self, client: CloudClient, task_id: str, pipeline: Pipeline, interval: int):
        super().__init__(daemon=True, name=f"hb-{task_id[:8]}")
        self.client = client
        self.task_id = task_id
        self.pipeline = pipeline
        self.interval = max(5, interval)
        self._stop = threading.Event()
        self.stage = "download"
        self.progress = 0.0
        self.lost_reason: str | None = None

    def update(self, stage: str, progress: float) -> None:
        self.stage = stage
        self.progress = progress

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.client.heartbeat(
                    self.task_id, stage=self.stage, progress=self.progress, message=f"阶段 {self.stage}"
                )
            except LeaseLost as exc:
                self.lost_reason = str(exc)
                logger.warning("租约丢失：%s，中止本地渲染", exc)
                self.pipeline.abort()
                return
            except CloudError as exc:
                # 网络抖动不该打断渲染，继续跑，下一轮再试
                logger.warning("心跳失败（将继续重试）：%s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("心跳异常（将继续重试）：%r", exc)


class Runner:
    def __init__(self, client: CloudClient, pipeline: Pipeline | None = None):
        self.client = client
        self.pipeline = pipeline or Pipeline()
        self._running = True

    def capabilities(self) -> dict:
        caps = {
            "device": settings.TORCH_DEVICE,
            "hostname": settings.WORKER_NAME,
            "max_concurrency": settings.MAX_CONCURRENCY,
        }
        try:  # 有 torch 就报显卡，没有也不影响领取任务
            import torch

            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                caps.update({"gpu": props.name, "vram_gb": round(props.total_memory / 1024**3, 1)})
        except Exception:  # noqa: BLE001
            pass
        return caps

    def run_forever(self) -> None:
        logger.info(
            "worker 启动：id=%s cloud=%s device=%s",
            settings.WORKER_ID,
            settings.CLOUD_BASE_URL,
            settings.TORCH_DEVICE,
        )
        while self._running:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("收到中断，退出")
                self._running = False
            except CloudError as exc:
                if exc.status == 401:
                    logger.error("令牌无效，退出（请检查 WORKER_TOKEN）")
                    return
                logger.warning("云端错误，5 秒后重试：%s", exc)
                time.sleep(5)
            except Exception:  # noqa: BLE001
                logger.exception("未预期错误，10 秒后重试")
                time.sleep(10)

    def run_once(self) -> None:
        payload = self.client.claim(self.capabilities())
        if not payload:
            return

        task_id = payload["task_id"]
        meta = payload.get("meta") or {}
        local, _ = LocalTask.objects.update_or_create(
            task_id=task_id,
            defaults={
                "status": LocalTask.Status.RUNNING,
                "duration_seconds": (meta.get("duration_ms") or 0) / 1000,
                "stage": "download",
                "progress": 0,
                "error_code": "",
                "error_message": "",
            },
        )
        logger.info("领到任务 %s（时长 %.1fs）", task_id, local.duration_seconds)

        self.pipeline.abort_event.clear()
        hb = _Heartbeat(self.client, task_id, self.pipeline, settings.HEARTBEAT_INTERVAL)
        hb.start()
        started = time.monotonic()

        def on_progress(stage: str, frac: float) -> None:
            hb.update(stage, frac)
            LocalTask.objects.filter(pk=local.pk).update(stage=stage, progress=frac)

        try:
            outcome = self.pipeline.run(payload, on_progress)

            for st in outcome.stages:
                StageLog.objects.create(
                    task_id=task_id,
                    stage=st.name,
                    exit_code=st.exit_code,
                    elapsed_seconds=round(st.elapsed, 2),
                    tail=st.tail,
                )

            on_progress("upload", 0.96)
            self.pipeline.upload(
                payload["result_upload_url"], outcome.out_path, content_type="video/mp4"
            )
            if payload.get("report_upload_url") and outcome.report_path.exists():
                try:
                    self.pipeline.upload(
                        payload["report_upload_url"], outcome.report_path, content_type="application/json"
                    )
                except PipelineError as exc:
                    # 报告传不上去不该判整单失败：成片才是交付物
                    logger.warning("报告回传失败（不影响交付）：%s", exc)

            hb.stop()
            self.client.complete(
                task_id,
                result_key=payload["result_key"],
                metrics=outcome.metrics,
                report_key=payload.get("report_key", ""),
            )
            LocalTask.objects.filter(pk=local.pk).update(
                status=LocalTask.Status.SUCCEEDED,
                progress=1.0,
                stage="done",
                metrics=outcome.metrics,
                finished_at=timezone.now(),
            )
            logger.info("任务 %s 完成，用时 %.1fs", task_id, time.monotonic() - started)

        except Aborted:
            reason = hb.lost_reason or "本地主动中止"
            LocalTask.objects.filter(pk=local.pk).update(
                status=LocalTask.Status.ABORTED,
                stage="aborted",
                error_code="ABORTED",
                error_message=reason,
                finished_at=timezone.now(),
            )
            logger.warning("任务 %s 中止：%s", task_id, reason)

        except PipelineError as exc:
            logger.error("任务 %s 失败：%s", task_id, exc)
            self._report_failure(local, task_id, exc.code, exc.message, exc.retryable)

        except Exception as exc:  # noqa: BLE001
            logger.exception("任务 %s 未预期失败", task_id)
            self._report_failure(local, task_id, "WORKER_UNEXPECTED", repr(exc), True)

        finally:
            hb.stop()
            self.pipeline.cleanup(task_id)

    # ------------------------------------------------------------------
    def _report_failure(
        self, local: LocalTask, task_id: str, code: str, message: str, retryable: bool
    ) -> None:
        try:
            self.client.fail(task_id, code, message, retryable=retryable)
        except LeaseLost:
            logger.warning("上报失败时租约已丢（云端会自己回收）")
        except Exception as exc:  # noqa: BLE001
            logger.error("失败上报也失败了：%r", exc)

        LocalTask.objects.filter(pk=local.pk).update(
            status=LocalTask.Status.FAILED,
            stage="failed",
            error_code=code,
            error_message=message[:2000],
            finished_at=timezone.now(),
        )


def build_client() -> CloudClient:
    if not settings.WORKER_TOKEN:
        raise RuntimeError("WORKER_TOKEN 未配置。请在云端执行 create_worker 生成后写入 .env")
    return CloudClient(
        settings.CLOUD_BASE_URL, settings.WORKER_ID, settings.WORKER_TOKEN, timeout=settings.HTTP_TIMEOUT
    )

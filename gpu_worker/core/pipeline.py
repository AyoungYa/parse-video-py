"""本机处理管线：下载 → 建 mask → LaMa 修复 → 验收 → 回传。

三条来自踩坑手册的铁律，直接固化在代码里：
  1. **判定成功只看退出码**，不看日志里有没有「完成」——`echo 'ALL DONE'` 是 shell 回显，
     中间命令全崩也照打（历史上把崩溃报成成功过）。
  2. **子进程 cwd 换了就必须传绝对路径**，否则 ffprobe 返回空、脚本崩。
  3. **抽帧按内存分流**（BATCH_DECODE_LIMIT）已在脚本内实现，这里不重复解码。

阶段命令来自 settings.WM_STAGES_TEMPLATE，可改 .env 的脚本参数而不动代码。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from django.conf import settings

logger = logging.getLogger(__name__)

FRAME_RE = re.compile(r"(\d{1,7})\s*/\s*(\d{1,7})")
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
TAIL_LINES = 200


class PipelineError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = True):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


class Aborted(Exception):
    """租约丢失或用户取消，主动中止。"""


@dataclass
class StageResult:
    name: str
    exit_code: int
    elapsed: float
    tail: str = ""


@dataclass
class PipelineOutcome:
    out_path: Path
    report_path: Path
    metrics: dict = field(default_factory=dict)
    stages: list[StageResult] = field(default_factory=list)


ProgressFn = Callable[[str, float], None]  # (stage, progress 0~1)


class Pipeline:
    def __init__(
        self,
        scripts_dir: Path | None = None,
        workspace: Path | None = None,
        device: str | None = None,
        python: str | None = None,
    ):
        self.scripts_dir = Path(scripts_dir or settings.WM_SCRIPTS_DIR).resolve()
        self.workspace = Path(workspace or settings.WORKSPACE_DIR).resolve()
        self.device = device or settings.TORCH_DEVICE
        self.python = python or settings.WM_PYTHON
        self.abort_event = threading.Event()

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def run(self, payload: dict, on_progress: ProgressFn) -> PipelineOutcome:
        task_id = payload["task_id"]
        work = self.workspace / task_id
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        video = work / "input.mp4"
        mask = work / "mask.png"
        out = work / "output.mp4"
        report = work / "report.json"

        try:
            on_progress("download", 0.02)
            self._download(payload["video_url"], video)

            if payload.get("mask_url"):
                self._download(payload["mask_url"], mask)

            bbox = self._bbox_str(payload.get("params") or {})
            ctx = {
                "python": self.python,
                "scripts": str(self.scripts_dir),
                "work": str(work),
                "input": str(video),
                "mask": str(mask),
                "out": str(out),
                "report": str(report),
                "bbox": bbox,
                "pad": str((payload.get("params") or {}).get("pad", settings.DEFAULT_PAD)),
                "device": self.device,
            }

            stages = self._plan_stages(payload, mask)
            stage_results: list[StageResult] = []
            total = max(1, len(stages))
            for idx, stage in enumerate(stages):
                if self.abort_event.is_set():
                    raise Aborted("租约丢失，中止处理")

                def stage_progress(frac: float, _idx=idx, _total=total) -> None:
                    overall = (_idx + max(0.0, min(1.0, frac))) / _total
                    # 渲染占 0.05~0.95，给下载和回传留头尾
                    on_progress(stage["name"], 0.05 + overall * 0.9)

                result = self._run_stage(stage, ctx, stage_progress)
                stage_results.append(result)
                if result.exit_code != 0:
                    raise PipelineError(
                        f"STAGE_{stage['name'].upper()}_FAILED",
                        f"阶段 {stage['name']} 退出码 {result.exit_code}\n{result.tail[-800:]}",
                        retryable=not self._is_oom(result.tail),
                    )
                # 阶段产物缺失也算失败（退出码 0 但没产出，历史踩过）
                if stage["name"] == "lama_apply" and not out.exists():
                    raise PipelineError("NO_OUTPUT", f"渲染结束但 {out} 不存在", retryable=True)

            metrics = self._read_report(report)
            if not out.exists():
                raise PipelineError("NO_OUTPUT", "未生成成片", retryable=True)

            return PipelineOutcome(out_path=out, report_path=report, metrics=metrics, stages=stage_results)
        except Aborted:
            raise
        except PipelineError:
            raise
        except requests.RequestException as exc:
            raise PipelineError("NETWORK", f"文件传输失败：{exc}", retryable=True) from exc
        except Exception as exc:  # noqa: BLE001
            raise PipelineError("PIPELINE_UNEXPECTED", repr(exc), retryable=True) from exc

    def abort(self) -> None:
        self.abort_event.set()

    def cleanup(self, task_id: str) -> None:
        shutil.rmtree(self.workspace / task_id, ignore_errors=True)

    # ------------------------------------------------------------------
    # 阶段编排
    # ------------------------------------------------------------------
    def _plan_stages(self, payload: dict, mask_path: Path) -> list[dict]:
        has_mask = bool(payload.get("mask_url")) or mask_path.exists()
        planned = []
        for stage in settings.WM_STAGES_TEMPLATE:
            if stage.get("enabled_when") == "no_mask" and has_mask:
                continue
            planned.append(stage)
        return planned

    def _run_stage(self, stage: dict, ctx: dict, on_progress: Callable[[float], None]) -> StageResult:
        cmd = [part.format(**ctx) for part in stage["cmd"]]
        name = stage["name"]
        logger.info("阶段 %s 开始：%s", name, " ".join(cmd))

        started = time.monotonic()
        tail: list[str] = []
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.scripts_dir),  # 脚本用绝对路径，cwd 只影响相对资源
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=_creation_flags(),
        )
        assert proc.stdout is not None

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                tail.append(line)
                if len(tail) > TAIL_LINES:
                    tail = tail[-TAIL_LINES:]
                frac = _parse_progress(line)
                if frac is not None:
                    on_progress(frac)
                if self.abort_event.is_set():
                    logger.warning("检测到中止信号，终止阶段 %s", name)
                    _kill(proc)
                    raise Aborted("租约丢失，已终止本地渲染")
            proc.wait()
        finally:
            if proc.poll() is None:
                _kill(proc)

        elapsed = time.monotonic() - started
        logger.info("阶段 %s 结束：exit=%s 用时 %.1fs", name, proc.returncode, elapsed)
        return StageResult(name=name, exit_code=proc.returncode or 0, elapsed=elapsed, tail="\n".join(tail))

    # ------------------------------------------------------------------
    # 传输
    # ------------------------------------------------------------------
    def _download(self, url: str, dest: Path) -> None:
        with requests.get(url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if self.abort_event.is_set():
                        raise Aborted("下载中收到中止信号")
                    fh.write(chunk)
        if dest.stat().st_size == 0:
            raise PipelineError("EMPTY_DOWNLOAD", "下载到的文件为空", retryable=True)

    def upload(self, url: str, path: Path, content_type: str = "application/octet-stream") -> None:
        """PUT 到云端签发的签名 URL。算力机不持有 OSS 凭证。"""
        size = path.stat().st_size
        with open(path, "rb") as fh:
            resp = requests.put(
                url, data=fh, headers={"Content-Type": content_type}, timeout=1800
            )
        if resp.status_code >= 400:
            raise PipelineError(
                "UPLOAD_FAILED", f"回传失败 {resp.status_code}：{resp.text[:300]}", retryable=True
            )
        logger.info("回传完成 %s（%.1f MB）", path.name, size / 1048576)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _bbox_str(params: dict) -> str:
        bbox = params.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            return " ".join(str(int(v)) for v in bbox)
        if isinstance(bbox, str) and bbox.strip():
            return bbox.strip()
        return ""  # 空则交给 build_mask 自动定位（脚本内走 min 投影）

    @staticmethod
    def _read_report(report_path: Path) -> dict:
        if not report_path.exists():
            return {}
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            logger.warning("报告解析失败：%s", exc)
            return {}
        return _normalize_metrics(data)

    @staticmethod
    def _is_oom(output: str) -> bool:
        lower = output.lower()
        return "out of memory" in lower or "cuda error" in lower


def _normalize_metrics(raw: dict) -> dict:
    """把 verify.py 的报告收敛成四项前端能直接展示的指标。

    不同版本的 verify.py 字段名略有差异，这里做一次归一化 + 缺省标记，
    前端永远只认这四个 key，避免报告格式变化把页面搞挂。
    """
    flat = raw
    if isinstance(raw.get("summary"), dict):
        flat = {**raw, **raw["summary"]}

    def pick(*names, default=None):
        for n in names:
            if n in flat and isinstance(flat[n], (int, float)):
                return float(flat[n])
        return default

    metrics = {
        "residual_max": pick("residual_max", "residual", "max_residual"),
        "gradient_p50_ratio": pick("gradient_p50_ratio", "detail_ratio", "sigma_ratio"),
        "flicker_p90": pick("flicker_p90", "flicker", "temporal_p90"),
    }
    checks = {
        "residual_max": metrics["residual_max"] is not None and metrics["residual_max"] < 3,
        "gradient_p50_ratio": metrics["gradient_p50_ratio"] is not None
        and metrics["gradient_p50_ratio"] > 0.85,
        "flicker_p90": metrics["flicker_p90"] is not None and metrics["flicker_p90"] < 1.5,
    }
    measured = [v for v in checks.values() if v is not None]
    metrics["all_green"] = bool(measured) and all(checks.values()) and all(
        metrics[k] is not None for k in checks
    )
    metrics["raw"] = raw
    return metrics


def _creation_flags() -> int:
    """Windows 下不弹黑窗（子进程常驻后台）。"""
    if os.name == "nt":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _parse_progress(line: str) -> float | None:
    """从脚本输出里抠进度：优先识别 `1843/4428` 帧号，其次 `42%`。"""
    m = FRAME_RE.search(line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        if total > 0:
            return min(1.0, done / total)
    m = PCT_RE.search(line)
    if m:
        return min(1.0, float(m.group(1)) / 100)
    return None

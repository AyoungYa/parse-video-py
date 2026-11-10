"""租约与状态机：全系统最关键的业务逻辑。

不变量（改代码前先读）：
  I1. 一个任务同一时刻只能被一个 worker 持有；
  I2. 任何在途状态都必须有 lease_expires_at，且超时后必须能被回收；
  I3. `complete` 与 `fail` 必须幂等 —— worker 网络重试不得重复扣分或重复退款；
  I4. 状态变更必须写 TaskEvent。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.services import refund_points

from ..models import IN_FLIGHT, Task, TaskEvent, TaskStatus, WorkerNode
from . import oss

logger = logging.getLogger(__name__)

#: worker 上报这个码 = 「自动定位不可信，请人工框选」。
#: 与 gpu_worker/scripts/wm_common.py 的 EXIT_LOCATE_LOW_CONFIDENCE 对应，改一处要同步另一处。
LOCATE_LOW_CONFIDENCE_CODE = "LOCATE_LOW_CONFIDENCE"


class LeaseLost(Exception):
    """租约已失效（超时被回收 / 任务被取消）。worker 必须立刻停止本地渲染。"""


class InvalidTransition(Exception):
    """非法状态迁移。"""


def authenticate_worker(raw_token: str) -> WorkerNode | None:
    if not raw_token:
        return None
    token_hash = WorkerNode.hash_token(raw_token)
    return WorkerNode.objects.filter(token_hash=token_hash, status__in=["online", "offline"]).first()


def touch_worker(worker: WorkerNode, capabilities: dict | None = None) -> None:
    updates = {"last_heartbeat_at": timezone.now(), "status": WorkerNode.Status.ONLINE}
    if capabilities:
        updates["capabilities"] = capabilities
    WorkerNode.objects.filter(pk=worker.pk).update(**updates)


def _log(task: Task, from_status: str, to_status: str, actor: str, **detail) -> None:
    TaskEvent.objects.create(
        task=task, from_status=from_status, to_status=to_status, actor=actor, detail=detail
    )


def log_transition(task: Task, from_status: str, to_status: str, actor: str, **detail) -> None:
    """对外的状态变更记录入口（视图层用它，别直接调 _log）。"""
    _log(task, from_status, to_status, actor, **detail)


# --------------------------------------------------------------------------
# 领取
# --------------------------------------------------------------------------
def claim_next_task(worker: WorkerNode) -> Task | None:
    """原子领取最早的一个 pending 任务。

    `select_for_update(skip_locked=True)` 是 MySQL 8 / PostgreSQL 的并发关键：
    两个 worker 同时来领，第二个会跳过被锁的行，绝不会领到同一个任务。
    """
    now = timezone.now()
    with transaction.atomic():
        task = (
            Task.objects.select_for_update(skip_locked=True)
            .filter(status__in=(TaskStatus.PENDING, TaskStatus.WORKER_OFFLINE))
            .order_by("created_at")
            .first()
        )
        if task is None:
            return None

        from_status = task.status
        task.status = TaskStatus.LEASED
        task.worker = worker
        task.lease_expires_at = now + timedelta(seconds=settings.LEASE_SECONDS)
        task.attempt += 1
        if from_status == TaskStatus.PENDING and not (task.checkpoints or {}).get("_retry_display_status"):
            task.progress = 0
        task.started_at = task.started_at or now
        task.error_code = ""
        task.error_message = ""
        checkpoints = dict(task.checkpoints or {})
        run_id = uuid.uuid4().hex
        checkpoints["_active_run_id"] = run_id
        checkpoints["_run_ids"] = [*(checkpoints.get("_run_ids") or []), run_id]
        task.checkpoints = checkpoints
        task.save(
            update_fields=[
                "status",
                "worker",
                "lease_expires_at",
                "attempt",
                "progress",
                "started_at",
                "error_code",
                "error_message",
                "checkpoints",
            ]
        )
        _log(task, from_status, TaskStatus.LEASED, actor=worker.name, attempt=task.attempt)
    return task


# --------------------------------------------------------------------------
# 心跳
# --------------------------------------------------------------------------
def heartbeat(
    worker: WorkerNode, task_id: str, stage: str = "", progress: float | None = None, message: str = ""
) -> int:
    """续租并记录进度。返回新的租约秒数；租约已丢失则抛 LeaseLost。"""
    now = timezone.now()
    with transaction.atomic():
        task = (
            Task.objects.select_for_update()
            .filter(id=task_id, worker=worker, status__in=IN_FLIGHT)
            .first()
        )
        if task is None:
            raise LeaseLost(f"任务 {task_id} 不在该 worker 手中或已结束")

        if task.lease_expires_at and task.lease_expires_at < now:
            raise LeaseLost("租约已过期")

        from_status = task.status
        task.lease_expires_at = now + timedelta(seconds=settings.LEASE_SECONDS)
        if progress is not None:
            task.progress = max(0.0, min(1.0, float(progress)))
        confirmed = bool((task.params or {}).get("bbox_confirmed"))
        if stage == "verifying" and confirmed:
            task.status = TaskStatus.VERIFYING
        elif stage in ("extracting_frames", "locating_watermark") and not confirmed:
            task.status = stage
        elif stage == "removing_watermark" and confirmed and task.status != TaskStatus.VERIFYING:
            task.status = TaskStatus.REMOVING_WATERMARK
        elif task.status == TaskStatus.LEASED:
            task.status = TaskStatus.REMOVING_WATERMARK if confirmed else TaskStatus.EXTRACTING_FRAMES

        checkpoints = dict(task.checkpoints or {})
        checkpoints.pop("_offline_display_status", None)
        checkpoints.pop("_retry_display_status", None)
        task.checkpoints = checkpoints
        task.save(update_fields=["lease_expires_at", "progress", "status", "checkpoints"])
        if task.status != from_status:
            _log(task, from_status, task.status, actor=worker.name, stage=stage, message=message)
    return settings.LEASE_SECONDS


def release_for_restart(worker: WorkerNode, task_id: str) -> Task:
    """计划重启时主动归还本节点的任务；重启不消耗处理失败次数。"""
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(id=task_id).first()
        if task is None or task.worker_id != worker.id or task.status not in IN_FLIGHT:
            raise LeaseLost("任务已不在该节点手中")
        from_status = task.status
        checkpoints = dict(task.checkpoints or {})
        checkpoints["_retry_display_status"] = (
            checkpoints.get("_retry_display_status")
            or checkpoints.get("_offline_display_status")
            or from_status
        )
        task.status = TaskStatus.PENDING
        task.worker = None
        task.lease_expires_at = None
        task.checkpoints = checkpoints
        task.attempt = max(0, task.attempt - 1)
        task.error_code = "WORKER_RESTARTED"
        task.error_message = "处理节点重启，任务已重新排队"
        task.save(update_fields=[
            "status", "worker", "lease_expires_at", "attempt",
            "error_code", "error_message", "checkpoints",
        ])
        _log(task, from_status, TaskStatus.PENDING, actor=worker.name, reason="worker_restart")
        return task


def record_checkpoint(
    worker: WorkerNode, task_id: str, stage: str, metrics: dict | None = None,
) -> Task:
    """OSS 产物存在且租约有效后，原子登记可供其他 worker 恢复的阶段。"""
    if stage not in oss.CHECKPOINT_FILES:
        raise InvalidTransition("未知处理阶段")
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(id=task_id).first()
        if task is None or task.worker_id != worker.id or task.status not in IN_FLIGHT:
            raise LeaseLost("任务已不在该节点手中")
        if task.lease_expires_at and task.lease_expires_at < timezone.now():
            raise LeaseLost("租约已过期")
        if not (task.params or {}).get("bbox_confirmed"):
            raise InvalidTransition("水印位置尚未确认")
        checkpoints = dict(task.checkpoints or {})
        key = oss.checkpoint_key(task_id, stage, checkpoints.get("_active_run_id", ""))
        if not oss.object_exists(key):
            raise InvalidTransition("阶段产物尚未上传到 OSS")
        dependencies = {
            "lama_apply": ("build_mask",),
            "verify": ("lama_apply",),
            "texture_fix": ("verify",),
            "texture_fix_rowinterp": ("verify",),
            "verify_final": ("texture_fix", "texture_fix_rowinterp"),
        }
        needed = dependencies.get(stage, ())
        if stage == "lama_apply" and task.mask_key:
            needed = ()
        if stage == "verify_final":
            if not any(name in checkpoints for name in needed):
                raise InvalidTransition("缺少修复阶段检查点")
        elif any(name not in checkpoints for name in needed):
            raise InvalidTransition("前一阶段尚未登记")
        previous = checkpoints.get(stage)
        if isinstance(previous, dict) and previous.get("key") == key:
            return task
        downstream = {
            "build_mask": ("lama_apply", "verify", "texture_fix", "texture_fix_rowinterp", "verify_final"),
            "lama_apply": ("verify", "texture_fix", "texture_fix_rowinterp", "verify_final"),
            "verify": ("texture_fix", "texture_fix_rowinterp", "verify_final"),
            "texture_fix": ("verify_final",),
            "texture_fix_rowinterp": ("verify_final",),
        }
        for later in downstream.get(stage, ()):
            checkpoints.pop(later, None)
        entry: dict = {"key": key}
        if stage in ("verify", "verify_final"):
            if not isinstance(metrics, dict):
                raise InvalidTransition("验收阶段缺少指标")
            entry["metrics"] = metrics
        checkpoints[stage] = entry
        task.checkpoints = checkpoints
        task.attempt = 0  # 只累计当前阶段连续失败；已完成阶段给后续阶段重新计数
        task.save(update_fields=["checkpoints", "attempt"])
        _log(task, task.status, task.status, actor=worker.name, checkpoint=stage)
        return task


def record_keyframe(worker: WorkerNode, task_id: str, index: int) -> Task:
    """逐帧登记已上传的图片，接力 worker 只复用云端确认过的版本。"""
    if index not in range(3):
        raise InvalidTransition("关键帧序号无效")
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(id=task_id).first()
        if task is None or task.worker_id != worker.id or task.status not in (
            TaskStatus.LEASED, TaskStatus.EXTRACTING_FRAMES, TaskStatus.LOCATING_WATERMARK
        ):
            raise LeaseLost("任务已不在该节点的准备阶段")
        if task.lease_expires_at and task.lease_expires_at < timezone.now():
            raise LeaseLost("租约已过期")
        checkpoints = dict(task.checkpoints or {})
        key = oss.keyframe_key(task_id, index, checkpoints.get("_active_run_id", ""))
        if not oss.object_exists(key):
            raise InvalidTransition("关键帧尚未上传到 OSS")
        frames = dict(checkpoints.get("keyframes") or {})
        if frames.get(str(index)) == key:
            return task
        frames[str(index)] = key
        checkpoints["keyframes"] = frames
        task.checkpoints = checkpoints
        task.attempt = 0
        task.save(update_fields=["checkpoints", "attempt"])
        _log(task, task.status, task.status, actor=worker.name, keyframe=index)
        return task


def prepare_task(worker: WorkerNode, task_id: str, bbox: list, candidates: list) -> Task:
    """定位结束后释放租约，等待用户确认，绝不直接开始渲染。"""
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(id=task_id).first()
        if task is None or task.worker_id != worker.id or task.status not in (
            TaskStatus.LEASED, TaskStatus.EXTRACTING_FRAMES, TaskStatus.LOCATING_WATERMARK
        ):
            raise InvalidTransition("任务不在该节点的准备阶段")
        if (task.params or {}).get("bbox_confirmed"):
            raise InvalidTransition("任务已确认水印位置")
        if task.lease_expires_at and task.lease_expires_at < timezone.now():
            raise InvalidTransition("租约已过期")
        from_status = task.status
        params = dict(task.params or {})
        params["locate_candidates"] = candidates[:5] or ([dict(zip(("x", "y", "w", "h"), bbox))] if bbox else [])
        params["suggested_bbox"] = bbox
        task.params = params
        task.status = TaskStatus.AWAITING_CONFIRMATION
        task.worker = None
        task.lease_expires_at = None
        task.progress = 0
        task.save(update_fields=["params", "status", "worker", "lease_expires_at", "progress"])
        _log(task, from_status, task.status, actor=worker.name, suggested_bbox=bbox)
        return task


# --------------------------------------------------------------------------
# 完成
# --------------------------------------------------------------------------
def complete_task(
    worker: WorkerNode, task_id: str, result_key: str, metrics: dict, report_key: str = ""
) -> Task:
    """完成上报。幂等：重复提交直接返回当前任务，不改状态、不重复扣分。"""
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(id=task_id).first()
        if task is None:
            raise InvalidTransition(f"任务 {task_id} 不存在")

        if task.status == TaskStatus.SUCCEEDED:
            logger.info("complete 幂等命中 task=%s", task_id)
            return task

        if task.status != TaskStatus.VERIFYING or not (task.params or {}).get("bbox_confirmed"):
            raise InvalidTransition(f"任务处于 {task.status}，不能标记完成")

        from_status = task.status
        task.status = TaskStatus.SUCCEEDED
        task.result_key = result_key or task.result_key
        task.report_key = report_key or task.report_key
        task.metrics = metrics or {}
        task.progress = 1.0
        task.lease_expires_at = None
        task.finished_at = timezone.now()
        task.save(
            update_fields=[
                "status",
                "result_key",
                "report_key",
                "metrics",
                "progress",
                "lease_expires_at",
                "finished_at",
            ]
        )
        _log(task, from_status, TaskStatus.SUCCEEDED, actor=worker.name, all_green=metrics.get("all_green"))
    return task


# --------------------------------------------------------------------------
# 失败
# --------------------------------------------------------------------------
def fail_task(
    worker: WorkerNode,
    task_id: str,
    error_code: str,
    error_message: str = "",
    retryable: bool = True,
    candidates: list | None = None,
) -> Task:
    """失败上报。可重试且未耗尽次数 → 打回 pending；否则判 failed 并退款。

    ★ 特例：`LOCATE_LOW_CONFIDENCE`（自动定位不可信）**不判失败**，
      而是转 `needs_manual` 等用户手动框选。
      理由：定位失败不是「片子有问题」，重试也没用（同样的输入还是同样定位不到），
      但只要用户框一下就能继续 —— 这时判失败 + 退款是最差的处理
      （用户白等一轮，钱还退了，体验断掉）。
    """
    with transaction.atomic():
        task = Task.objects.select_for_update().filter(id=task_id).first()
        if task is None:
            raise InvalidTransition(f"任务 {task_id} 不存在")
        if task.is_terminal:
            return task  # 幂等

        from_status = task.status
        now = timezone.now()

        # ── 定位置信度不足 → 转人工框选 ──
        if error_code == LOCATE_LOW_CONFIDENCE_CODE:
            task.status = TaskStatus.NEEDS_MANUAL
            task.worker = None
            task.lease_expires_at = None
            task.progress = 0
            task.error_code = error_code
            task.error_message = error_message[:2000]
            # 把候选框存进 params，前端直接拿来当默认框，用户微调即可
            params = dict(task.params or {})
            params["locate_candidates"] = (candidates or [])[:5]
            task.params = params
            task.save(
                update_fields=[
                    "status",
                    "worker",
                    "lease_expires_at",
                    "progress",
                    "error_code",
                    "error_message",
                    "params",
                ]
            )
            _log(task, from_status, TaskStatus.NEEDS_MANUAL, actor=worker.name, code=error_code)
            return task

        can_retry = retryable and task.attempt < settings.MAX_ATTEMPT

        if can_retry:
            checkpoints = dict(task.checkpoints or {})
            checkpoints["_retry_display_status"] = (
                checkpoints.get("_retry_display_status")
                or checkpoints.get("_offline_display_status")
                or from_status
            )
            task.status = TaskStatus.PENDING
            task.worker = None
            task.lease_expires_at = None
            task.checkpoints = checkpoints
            task.error_code = error_code
            task.error_message = error_message[:2000]
            task.save(
                update_fields=["status", "worker", "lease_expires_at", "checkpoints", "error_code", "error_message"]
            )
            _log(task, from_status, TaskStatus.PENDING, actor=worker.name, code=error_code, retry=True)
            return task

        task.status = TaskStatus.FAILED
        task.error_code = error_code
        task.error_message = error_message[:2000]
        task.lease_expires_at = None
        task.finished_at = now
        task.save(
            update_fields=["status", "error_code", "error_message", "lease_expires_at", "finished_at"]
        )
        _log(task, from_status, TaskStatus.FAILED, actor=worker.name, code=error_code, retry=False)

    _refund_if_needed(task, reason=f"处理失败：{error_code}")
    return task


# --------------------------------------------------------------------------
# 回收（定时任务调用）
# --------------------------------------------------------------------------
def mark_offline_workers(batch: int = 100) -> dict:
    """超过 60 秒没有进程级 alive 的节点离线，释放其任务供接力。"""
    cutoff = timezone.now() - timedelta(seconds=60)
    worker_ids = list(
        WorkerNode.objects.filter(status=WorkerNode.Status.ONLINE, last_heartbeat_at__lt=cutoff)
        .values_list("id", flat=True)[:batch]
    )
    stats = {"offline": 0, "released": 0}
    for worker_id in worker_ids:
        with transaction.atomic():
            worker = WorkerNode.objects.select_for_update().filter(pk=worker_id).first()
            if not worker or worker.status != WorkerNode.Status.ONLINE or not worker.last_heartbeat_at or worker.last_heartbeat_at >= cutoff:
                continue
            worker.status = WorkerNode.Status.OFFLINE
            worker.save(update_fields=["status"])
            stats["offline"] += 1
            for task in Task.objects.select_for_update().filter(worker=worker, status__in=IN_FLIGHT):
                previous = task.status
                checkpoints = dict(task.checkpoints or {})
                checkpoints["_offline_display_status"] = previous
                task.status = TaskStatus.WORKER_OFFLINE
                task.worker = None
                task.lease_expires_at = None
                task.attempt = max(0, task.attempt - 1)
                task.error_code = "WORKER_OFFLINE"
                task.error_message = "处理节点超过 60 秒未上报存活，等待其他节点接力"
                task.checkpoints = checkpoints
                task.save(update_fields=["status", "worker", "lease_expires_at", "attempt", "error_code", "error_message", "checkpoints"])
                _log(task, previous, TaskStatus.WORKER_OFFLINE, actor="reaper", worker=worker.name)
                stats["released"] += 1
    return stats


def reclaim_expired_leases(batch: int = 100) -> dict:
    """把租约超时的在途任务收回。

    这是「本地 worker 会随时消失」的兜底：断电后 90 秒，任务自动回到队列，
    用户看到的是「排队中」而不是永远「处理中」。
    """
    now = timezone.now()
    expired_ids = list(
        Task.objects.filter(status__in=IN_FLIGHT, lease_expires_at__lt=now)
        .values_list("id", flat=True)[:batch]
    )
    stats = {"reclaimed": 0, "failed": 0}
    for task_id in expired_ids:
        with transaction.atomic():
            task = Task.objects.select_for_update().filter(id=task_id).first()
            if task is None or task.status not in IN_FLIGHT:
                continue
            if not task.lease_expires_at or task.lease_expires_at >= timezone.now():
                continue  # 期间被心跳续租了

            from_status = task.status
            if task.attempt >= settings.MAX_ATTEMPT:
                task.status = TaskStatus.FAILED
                task.error_code = "LEASE_EXPIRED"
                task.error_message = f"租约连续 {task.attempt} 次超时，判定失败"
                task.finished_at = timezone.now()
                task.lease_expires_at = None
                task.save(
                    update_fields=["status", "error_code", "error_message", "finished_at", "lease_expires_at"]
                )
                _log(task, from_status, TaskStatus.FAILED, actor="reaper", attempt=task.attempt)
                stats["failed"] += 1
                need_refund = True
            else:
                checkpoints = dict(task.checkpoints or {})
                checkpoints["_retry_display_status"] = (
                    checkpoints.get("_retry_display_status")
                    or checkpoints.get("_offline_display_status")
                    or from_status
                )
                task.status = TaskStatus.PENDING
                task.worker = None
                task.lease_expires_at = None
                task.checkpoints = checkpoints
                task.error_code = "LEASE_EXPIRED"
                task.error_message = "处理节点失联，已重新排队"
                task.save(
                    update_fields=[
                        "status",
                        "worker",
                        "lease_expires_at",
                        "checkpoints",
                        "error_code",
                        "error_message",
                    ]
                )
                _log(task, from_status, TaskStatus.PENDING, actor="reaper", attempt=task.attempt)
                stats["reclaimed"] += 1
                need_refund = False

        if need_refund:
            _refund_if_needed(task, reason="算力节点失联，自动退款")
    if expired_ids:
        logger.info("回收租约：%s", stats)
    return stats


def _refund_if_needed(task: Task, reason: str) -> None:
    """退还预扣积分。用 refunded_at 做幂等标记，避免重复退。"""
    if task.points_cost <= 0 or task.refunded_at:
        return
    Task.objects.filter(pk=task.pk, refunded_at__isnull=True).update(refunded_at=timezone.now())
    task.refresh_from_db(fields=["refunded_at"])
    if task.refunded_at:
        refund_points(task.user, task.points_cost, str(task.id), note=reason)
        _log(task, task.status, task.status, actor="system", refund=task.points_cost, note=reason)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def make_worker(name: str) -> tuple[WorkerNode, str]:
    """创建一个算力节点并返回明文 token（只此一次可见）。"""
    import secrets

    raw = "wkr_" + secrets.token_urlsafe(32)
    worker = WorkerNode.objects.create(name=name, token_hash=WorkerNode.hash_token(raw))
    return worker, raw


def task_id_hash(task_id) -> str:
    return hashlib.sha256(str(task_id).encode()).hexdigest()[:16]

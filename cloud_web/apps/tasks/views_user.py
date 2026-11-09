"""用户侧 API：签发直传凭证、确认上传、查进度、取下载链接、取消。

额度规则（见 docs/product/商业化方案.md §三）：
  · 免费用户：单条 ≤ FREE_TRIAL_MAX_SECONDS（30s），靠新人赠送积分支撑；
  · 包月用户：单条 ≤ 30min，全月 ≤ MONTHLY_MINUTES_LIMIT（100min）；
  · 积分在「确认上传」时才预扣 —— 上传失败不该扣钱。
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.billing.services import (
    InsufficientPoints,
    active_subscription,
    consume_points,
    get_balance,
    points_for_duration,
)

from .models import Task, TaskStatus
from .serializers import TaskDetailSerializer, TaskListSerializer
from .services import lease, oss
from .services.quota import month_minutes_used as _month_minutes_used

logger = logging.getLogger(__name__)


class _UploadThrottle(ScopedRateThrottle):
    scope = "upload_sign"


def _task_filter(ref: str) -> dict:
    """路径参数既接受内部 UUID，也接受对外 task_no。

    业务号是给用户端用的稳定标识；UUID 继续兼容，老前端不用改。
    ★ 只负责解析标识，用户隔离必须由调用方显式加 user=... 条件。
    """
    try:
        uuid.UUID(str(ref))
    except (ValueError, AttributeError, TypeError):
        return {"task_no": ref}
    return {"id": ref}


def _quota_error(message: str, code: str = "quota_exceeded"):
    return Response({"code": code, "message": message}, status=status.HTTP_402_PAYMENT_REQUIRED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([_UploadThrottle])
def sign_upload(request):
    """① 签发 OSS 直传凭证。此时不扣积分，只做基本资格校验。"""
    size = int(request.data.get("size") or 0)
    if size <= 0:
        return Response({"code": "bad_request", "message": "缺少文件大小"}, status=400)
    if size > settings.OSS_MAX_UPLOAD_BYTES:
        mb = settings.OSS_MAX_UPLOAD_BYTES // (1024 * 1024)
        return Response({"code": "file_too_large", "message": f"文件超过 {mb}MB 上限"}, status=413)

    user = request.user
    sub = active_subscription(user)

    if sub is None and get_balance(user) <= 0:
        return _quota_error("试用额度已用完，签到或订阅后继续使用")

    if sub is not None:
        used = _month_minutes_used(user)
        if used >= sub.minutes_limit:
            return _quota_error(f"本月已用 {used:.0f}/{sub.minutes_limit} 分钟，超限后可加购")

    task = Task.objects.create(user=user, status=TaskStatus.AWAITING_UPLOAD, size_bytes=size)
    key = oss.upload_key(str(task.id))
    task.original_key = key
    task.save(update_fields=["original_key"])

    try:
        policy = oss.build_post_policy(str(task.id), key)
    except oss.OssNotConfigured as exc:
        task.delete()
        return Response(
            {"code": "oss_not_configured", "message": f"存储未配置：{exc}"},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return Response({"task_id": str(task.id), "task_no": task.task_no, "upload": policy})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def confirm_upload(request, task_id):
    """② 浏览器直传成功后确认。这一步才校验时长、预扣积分、进入队列。"""
    user = request.user
    try:
        task = Task.objects.get(**_task_filter(task_id), user=user)
    except Task.DoesNotExist:
        return Response({"code": "not_found", "message": "任务不存在"}, status=404)

    if task.status != TaskStatus.AWAITING_UPLOAD:
        return Response({"code": "invalid_state", "message": f"任务当前为 {task.status}"}, status=409)

    duration_ms = int(request.data.get("duration_ms") or 0)
    if duration_ms <= 0:
        return Response({"code": "bad_request", "message": "缺少视频时长"}, status=400)

    if not oss.object_exists(task.original_key):
        return Response({"code": "upload_missing", "message": "文件尚未上传成功"}, status=409)

    sub = active_subscription(user)
    max_seconds = 30 * 60 if sub else settings.FREE_TRIAL_MAX_SECONDS
    if duration_ms / 1000 > max_seconds:
        hint = "订阅后可处理 30 分钟内视频" if not sub else "单条视频请控制在 30 分钟内"
        return Response(
            {"code": "duration_exceeded", "message": f"当前上限 {max_seconds // 60} 分钟，{hint}"},
            status=422,
        )

    cost = 0 if sub else points_for_duration(duration_ms)
    try:
        with transaction.atomic():
            task = Task.objects.select_for_update().get(pk=task.pk)
            if cost:
                consume_points(user, cost, str(task.id), note=f"{duration_ms / 1000:.0f}s 视频")
            task.duration_ms = duration_ms
            task.width = int(request.data.get("width") or 0)
            task.height = int(request.data.get("height") or 0)
            task.fps = float(request.data.get("fps") or 0)
            task.size_bytes = oss.object_size(task.original_key) or task.size_bytes
            task.points_cost = cost
            task.params = request.data.get("params") or {}
            task.status = TaskStatus.PENDING
            task.save(
                update_fields=[
                    "duration_ms",
                    "width",
                    "height",
                    "fps",
                    "size_bytes",
                    "points_cost",
                    "params",
                    "status",
                ]
            )
            lease.log_transition(task, TaskStatus.AWAITING_UPLOAD, TaskStatus.PENDING, actor=f"user:{user.id}")
    except InsufficientPoints as exc:
        return _quota_error(str(exc), code="insufficient_points")

    return Response({"ok": True, "task_id": str(task.id), "task_no": task.task_no,
                    "status": task.status, "points_cost": cost})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def submit_bbox(request, task_id):
    """③ 用户手动框选水印位置后提交，重新入队。

    只对 `needs_manual` 状态开放（自动定位失败的任务）。提交后：
      · bbox 写进 params（worker 拿到就会跳过自动定位）
      · 状态回到 pending，worker 重新领走
      · ★ **不重新扣积分**（第一次已经扣过了，这里只是换个处理方式）
    """
    user = request.user
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(**_task_filter(task_id), user=user)
        except Task.DoesNotExist:
            return Response({"code": "not_found", "message": "任务不存在"}, status=404)

        if task.status != TaskStatus.NEEDS_MANUAL:
            return Response(
                {"code": "invalid_state", "message": f"任务当前为 {task.status}，不需要手动框选"},
                status=409,
            )

        bbox = _parse_bbox(request.data.get("bbox"))
        if bbox is None:
            return Response(
                {"code": "bad_bbox", "message": "框选区域无效，请重新框选"}, status=400
            )

        from_status = task.status
        params = dict(task.params or {})
        params["bbox"] = list(bbox)
        params.pop("locate_candidates", None)
        task.params = params
        task.status = TaskStatus.PENDING
        task.worker = None
        task.lease_expires_at = None
        task.progress = 0
        task.error_code = ""
        task.error_message = ""
        # 重试次数重置：用户已经人工确认过，不该再受上一轮的 attempt 影响
        task.attempt = 0
        task.save(
            update_fields=[
                "params",
                "status",
                "worker",
                "lease_expires_at",
                "progress",
                "error_code",
                "error_message",
                "attempt",
            ]
        )
        lease.log_transition(
            task, from_status, TaskStatus.PENDING, actor=f"user:{user.id}", manual_bbox=list(bbox)
        )

    return Response({"ok": True, "task_id": str(task.id), "status": task.status, "bbox": list(bbox)})


def _parse_bbox(raw) -> tuple[int, int, int, int] | None:
    """解析前端传来的 bbox：[x, y, w, h]（全图坐标，像素）。

    严格校验：必须 4 个正整数，且不能超出画面范围 ——
    框到画面外会让 LaMa 的 ROI 越界崩掉。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        x, y, w, h = (int(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return None
    # 画面尺寸未知时只做基本校验（前端拿不到 meta 的极端情况）
    if w > 20000 or h > 20000:
        return None
    return x, y, w, h


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_tasks(request):
    qs = Task.objects.filter(user=request.user).exclude(status=TaskStatus.AWAITING_UPLOAD)
    return Response(TaskListSerializer(qs[:50], many=True).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def task_detail(request, task_id):
    try:
        task = Task.objects.get(**_task_filter(task_id), user=request.user)
    except Task.DoesNotExist:
        return Response({"code": "not_found", "message": "任务不存在"}, status=404)

    data = TaskDetailSerializer(task).data
    # 原片预览：结果页的「拖动对比」需要它，10 分钟够看完一次对比
    if task.original_key:
        data["preview_url"] = oss.presign_get(task.original_key, expire_seconds=600)
    if task.status == TaskStatus.SUCCEEDED:
        data["download_url"] = oss.presign_get(task.result_key, expire_seconds=600)
        data["report_url"] = oss.presign_get(task.report_key, expire_seconds=600) if task.report_key else ""
    # 人工框选：把候选框与建议尺寸一并给前端
    if task.status == TaskStatus.NEEDS_MANUAL:
        params = task.params or {}
        data["locate_candidates"] = params.get("locate_candidates") or []
        data["locate_reason"] = task.error_message
        # 框选页要按原视频尺寸换算坐标
        data["frame_width"] = task.width
        data["frame_height"] = task.height
    return Response(data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_task(request, task_id):
    """取消：未开始的直接取消并退分；已在算力机上跑的交给租约/worker 处理。"""
    with transaction.atomic():
        try:
            task = Task.objects.select_for_update().get(**_task_filter(task_id), user=request.user)
        except Task.DoesNotExist:
            return Response({"code": "not_found", "message": "任务不存在"}, status=404)

        if task.is_terminal:
            return Response({"code": "invalid_state", "message": "任务已结束"}, status=409)
        if task.status in (TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.VERIFYING):
            return Response(
                {"code": "in_progress", "message": "任务正在处理，请稍候重试取消"}, status=409
            )

        from_status = task.status
        task.status = TaskStatus.CANCELLED
        task.finished_at = timezone.now()
        task.save(update_fields=["status", "finished_at"])
        lease.log_transition(task, from_status, TaskStatus.CANCELLED, actor=f"user:{request.user.id}")

    lease._refund_if_needed(task, reason="用户取消")
    return Response({"ok": True, "status": task.status})

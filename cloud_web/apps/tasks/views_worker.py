"""Worker 协议端点（算力机调用）。

鉴权：`X-Worker-Token`。token 明文只在创建节点时出现一次，库里只存 sha256。

⚠️ 部署提醒：`claim` 是长轮询（默认挂 25 秒）。用 gunicorn 同步 worker 时要保证
worker 数量 > 并发算力机数 × 2，否则连接会被长轮询占满。4 台以内预算 16~20 个 worker 足够。
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .services import lease, oss

logger = logging.getLogger(__name__)


def _require_worker(request):
    raw = request.headers.get("X-Worker-Token", "")
    worker = lease.authenticate_worker(raw)
    if worker is None:
        return None, Response(
            {"code": "invalid_worker_token", "message": "算力节点令牌无效"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if worker.status == "disabled":
        return None, Response(
            {"code": "worker_disabled", "message": "该节点已被禁用"}, status=status.HTTP_403_FORBIDDEN
        )
    return worker, None


def _build_payload(task, lease_seconds: int) -> dict:
    """下发给 worker 的任务包。视频走签名 URL，不下发 OSS 凭证。"""
    rkey = oss.result_key(str(task.id))
    pkey = oss.report_key(str(task.id))
    fkeys = [oss.keyframe_key(str(task.id), i) for i in range(3)]
    payload = {
        "task_id": str(task.id),
        "lease_seconds": lease_seconds,
        "params": task.params or {},
        "meta": {
            "duration_ms": task.duration_ms,
            "size_bytes": task.size_bytes,
            "width": task.width,
            "height": task.height,
            "fps": task.fps,
        },
        "result_key": rkey,
        "report_key": pkey,
        "phase": "render" if (task.params or {}).get("bbox_confirmed") else "prepare",
        "keyframe_keys": fkeys,
    }
    try:
        payload["video_url"] = oss.presign_get(task.original_key)
        if task.mask_key:
            payload["mask_url"] = oss.presign_get(task.mask_key)
        # 回传通道：worker 只能往这两个 key 写，写完自己报 complete
        payload["result_upload_url"] = oss.presign_put(rkey, content_type="video/mp4")
        payload["report_upload_url"] = oss.presign_put(pkey, content_type="application/json")
        payload["keyframe_upload_urls"] = [
            oss.presign_put(key, content_type="image/jpeg") for key in fkeys
        ]
    except oss.OssNotConfigured:
        # 不静默降级：没有签名 URL 的 worker 什么也干不了
        raise
    return payload


@api_view(["POST"])
@permission_classes([AllowAny])
def prepared(request):
    worker, error = _require_worker(request)
    if error:
        return error
    task_id = request.data.get("task_id")
    bbox = request.data.get("bbox")
    if not task_id or not isinstance(bbox, list) or len(bbox) not in (0, 4):
        return Response({"code": "bad_request", "message": "缺少 task_id 或 bbox"}, status=400)
    if bbox and (any(type(v) is not int for v in bbox) or bbox[0] < 0 or bbox[1] < 0
                 or bbox[2] < 8 or bbox[3] < 8):
        return Response({"code": "bad_bbox", "message": "建议区域无效"}, status=400)
    if not all(oss.object_exists(oss.keyframe_key(task_id, i)) for i in range(3)):
        return Response({"code": "keyframe_missing", "message": "关键帧尚未上传"}, status=409)
    try:
        task = lease.prepare_task(worker, task_id, bbox, request.data.get("candidates") or [])
    except lease.InvalidTransition as exc:
        return Response({"code": "invalid_transition", "message": str(exc)}, status=409)
    return Response({"ok": True, "task_id": str(task.id), "status": task.status})


@api_view(["POST"])
@permission_classes([AllowAny])
def claim(request):
    """① 领任务（长轮询）。无任务返回 204，worker 立即重新发起。"""
    worker, error = _require_worker(request)
    if error:
        return error

    capabilities = request.data.get("capabilities") or {}
    lease.touch_worker(worker, capabilities)

    deadline = time.monotonic() + settings.WORKER_CLAIM_TIMEOUT
    while True:
        task = lease.claim_next_task(worker)
        if task is not None:
            try:
                payload = _build_payload(task, settings.LEASE_SECONDS)
            except oss.OssNotConfigured as exc:
                lease.fail_task(worker, str(task.id), "OSS_NOT_CONFIGURED", str(exc), retryable=True)
                return Response(
                    {"code": "oss_not_configured", "message": "云端 OSS 未配置，无法签发下载 URL"},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            logger.info("worker=%s 领取任务 %s（第 %s 次）", worker.name, task.id, task.attempt)
            return Response(payload)

        if time.monotonic() >= deadline:
            return Response(status=status.HTTP_204_NO_CONTENT)
        time.sleep(1.0)


@api_view(["POST"])
@permission_classes([AllowAny])
def heartbeat(request):
    """② 心跳续租 + 进度。返回 409 表示租约已丢，worker 必须立刻停止本地渲染。"""
    worker, error = _require_worker(request)
    if error:
        return error

    task_id = request.data.get("task_id")
    if not task_id:
        return Response({"code": "missing_task_id", "message": "缺少 task_id"}, status=400)

    try:
        lease_seconds = lease.heartbeat(
            worker,
            task_id,
            stage=request.data.get("stage", ""),
            progress=request.data.get("progress"),
            message=request.data.get("message", ""),
        )
    except lease.LeaseLost as exc:
        return Response(
            {"ok": False, "code": "lease_lost", "message": str(exc)}, status=status.HTTP_409_CONFLICT
        )

    lease.touch_worker(worker)
    return Response({"ok": True, "lease_seconds": lease_seconds})


@api_view(["POST"])
@permission_classes([AllowAny])
def complete(request):
    """③ 完成上报（幂等）。"""
    worker, error = _require_worker(request)
    if error:
        return error

    task_id = request.data.get("task_id")
    result_key = request.data.get("result_key")
    if not (task_id and result_key):
        return Response({"code": "bad_request", "message": "缺少 task_id 或 result_key"}, status=400)

    try:
        task = lease.complete_task(
            worker,
            task_id,
            result_key=result_key,
            metrics=request.data.get("metrics") or {},
            report_key=request.data.get("report_key", ""),
        )
    except lease.InvalidTransition as exc:
        return Response({"code": "invalid_transition", "message": str(exc)}, status=409)

    lease.touch_worker(worker)
    return Response(
        {
            "ok": True,
            "task_id": str(task.id),
            "status": task.status,
            "all_green": bool((task.metrics or {}).get("all_green")),
        }
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def fail(request):
    """④ 失败上报。retryable 且未耗尽次数会打回队列。

    ★ `LOCATE_LOW_CONFIDENCE` 会被 lease.fail_task 转成 `needs_manual`
      （等用户手动框选），并把 `candidates`（定位候选框）存进 task.params。
    """
    worker, error = _require_worker(request)
    if error:
        return error

    task_id = request.data.get("task_id")
    if not task_id:
        return Response({"code": "bad_request", "message": "缺少 task_id"}, status=400)

    try:
        task = lease.fail_task(
            worker,
            task_id,
            error_code=request.data.get("error_code", "UNKNOWN")[:64],
            error_message=request.data.get("error_message", ""),
            retryable=bool(request.data.get("retryable", True)),
            candidates=request.data.get("candidates") or [],
        )
    except lease.InvalidTransition as exc:
        return Response({"code": "invalid_transition", "message": str(exc)}, status=409)

    lease.touch_worker(worker)
    return Response({"ok": True, "task_id": str(task.id), "status": task.status, "attempt": task.attempt})

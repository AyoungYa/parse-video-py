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
    checkpoint_stages = list(oss.CHECKPOINT_FILES)
    active_run_id = (task.checkpoints or {}).get("_active_run_id", "")
    fkeys = [oss.keyframe_key(str(task.id), i, active_run_id) for i in range(3)]
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
        "checkpoint_get_urls": {},
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
        saved_frames = (task.checkpoints or {}).get("keyframes") or {}
        payload["keyframe_resume_urls"] = {}
        if payload["phase"] == "prepare":
            for i in range(3):
                saved_key = saved_frames.get(str(i))
                if not saved_key and not active_run_id:
                    saved_key = oss.keyframe_key(str(task.id), i)
                if saved_key and oss.object_exists(saved_key):
                    payload["keyframe_resume_urls"][str(i)] = oss.presign_get(saved_key)
        payload["checkpoint_upload_urls"] = {
            stage: oss.presign_put(
                oss.checkpoint_key(str(task.id), stage, active_run_id),
                # 每次领取独立路径，旧 worker 的未过期签名无法覆盖接力产物。
                content_type="application/json" if stage in ("verify", "verify_final")
                else "image/png" if stage == "build_mask" else "video/mp4",
            )
            for stage in checkpoint_stages
        }
        if payload["phase"] == "render":
            recorded = task.checkpoints or {}
            for stage in checkpoint_stages:
                if stage == "build_mask" and task.mask_key:
                    continue
                if stage == "verify_final" and not any(
                    name in payload["checkpoint_get_urls"]
                    for name in ("texture_fix", "texture_fix_rowinterp")
                ):
                    continue
                entry = recorded.get(stage)
                key = entry.get("key") if isinstance(entry, dict) else None
                if not key or not oss.object_exists(key):
                    if stage in ("texture_fix", "texture_fix_rowinterp", "verify_final"):
                        continue
                    break
                payload["checkpoint_get_urls"][stage] = oss.presign_get(key)
            payload["checkpoint_metrics"] = {
                stage: recorded[stage].get("metrics", {})
                for stage in ("verify", "verify_final") if stage in payload["checkpoint_get_urls"]
            }
    except oss.OssNotConfigured:
        # 不静默降级：没有签名 URL 的 worker 什么也干不了
        raise
    return payload


@api_view(["POST"])
@permission_classes([AllowAny])
def checkpoint(request):
    worker, error = _require_worker(request)
    if error:
        return error
    task_id = request.data.get("task_id")
    stage = request.data.get("stage")
    if not task_id or not isinstance(stage, str) or stage not in oss.CHECKPOINT_FILES:
        return Response({"code": "bad_request", "message": "任务或处理阶段无效"}, status=400)
    try:
        task = lease.record_checkpoint(worker, task_id, stage, request.data.get("metrics"))
    except lease.LeaseLost as exc:
        return Response({"code": "lease_lost", "message": str(exc)}, status=409)
    except lease.InvalidTransition as exc:
        return Response({"code": "invalid_transition", "message": str(exc)}, status=409)
    return Response({"ok": True, "task_id": str(task.id), "stage": stage})


@api_view(["POST"])
@permission_classes([AllowAny])
def frame(request):
    worker, error = _require_worker(request)
    if error:
        return error
    task_id = request.data.get("task_id")
    index = request.data.get("index")
    if not task_id or type(index) is not int or index not in range(3):
        return Response({"code": "bad_request", "message": "任务或关键帧序号无效"}, status=400)
    try:
        task = lease.record_keyframe(worker, task_id, index)
    except lease.LeaseLost as exc:
        return Response({"code": "lease_lost", "message": str(exc)}, status=409)
    except lease.InvalidTransition as exc:
        return Response({"code": "invalid_transition", "message": str(exc)}, status=409)
    return Response({"ok": True, "task_id": str(task.id), "index": index})


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
    from .models import Task
    task = Task.objects.filter(id=task_id).first()
    saved_frames = (task.checkpoints or {}).get("keyframes") if task else {}
    legacy = not (task and (task.checkpoints or {}).get("_active_run_id"))
    if not all(
        (key := (saved_frames or {}).get(str(i)) or (oss.keyframe_key(task_id, i) if legacy else ""))
        and oss.object_exists(key)
        for i in range(3)
    ):
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
    if capabilities:
        type(worker).objects.filter(pk=worker.pk).update(capabilities=capabilities)

    deadline = time.monotonic() + settings.WORKER_CLAIM_TIMEOUT
    next_reclaim = 0.0
    while True:
        if time.monotonic() >= next_reclaim:
            lease.mark_offline_workers(batch=20)
            lease.reclaim_expired_leases(batch=20)
            next_reclaim = time.monotonic() + 5
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
def alive(request):
    """进程级存活上报，不依赖任务领取或任务心跳。"""
    worker, error = _require_worker(request)
    if error:
        return error
    lease.touch_worker(worker)
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([AllowAny])
def release(request):
    """计划重启时释放当前任务，让下一次领取从所属阶段重新执行。"""
    worker, error = _require_worker(request)
    if error:
        return error
    task_id = request.data.get("task_id")
    if not task_id:
        return Response({"code": "missing_task_id", "message": "缺少 task_id"}, status=400)
    try:
        task = lease.release_for_restart(worker, task_id)
    except lease.LeaseLost as exc:
        return Response({"code": "lease_lost", "message": str(exc)}, status=409)
    return Response({"ok": True, "task_id": str(task.id), "status": task.status})


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

    return Response({"ok": True, "task_id": str(task.id), "status": task.status, "attempt": task.attempt})

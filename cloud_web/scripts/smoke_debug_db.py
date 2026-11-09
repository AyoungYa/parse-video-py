"""调试环境功能冒烟（不建测试库、不留脏数据）。

用途：云端调试库账号通常只有库级授权，没有全局 CREATE DATABASE，
`manage.py test` 建不了 test_xxx 临时库。本脚本直接在**真实调试库**上
跑一遍用户侧 + Worker 侧 API，用一个外层事务包住，结束时 set_rollback，
因此所有写入都会回滚，只验证代码路径是否通。

跑法：
    uv run python scripts/smoke_debug_db.py

OSS 的「对象存在 / 大小」用桩替换（真实上传需要浏览器直传，不在本脚本范围）；
其余全部走真实代码：ORM、额度账本、租约状态机、鉴权、序列化。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

# 从 scripts/ 直接启动时 sys.path[0] 是 scripts/，需把项目根加进来才能 import config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.billing.models import PointLedger, PointReason  # noqa: E402
from apps.tasks.models import Task, TaskEvent, TaskStatus  # noqa: E402
from apps.tasks.services import lease, oss  # noqa: E402
from apps.tasks import views_user, views_worker  # noqa: E402

PASS: list[str] = []


def ok(step: str, detail: str = "") -> None:
    line = f"  ✅ {step}" + (f"  → {detail}" if detail else "")
    PASS.append(line)
    print(line, flush=True)


def expect(resp, code: int = 200, label: str = "") -> dict:
    assert resp.status_code == code, (
        f"{label}: 期望 {code} 实得 {resp.status_code}，body={getattr(resp, 'data', None)}"
    )
    return resp.data


def main() -> int:
    rf = APIRequestFactory()
    stamp = str(int(time.time()))
    phone = "139" + stamp[-8:]

    with transaction.atomic():
        # ── 0. 准备用户与额度 ─────────────────────────────────────
        user = User.objects.create_user(
            username=f"smoke_{stamp}", phone=phone, nickname="冒烟"
        )
        PointLedger.objects.create(
            user=user, delta=10, balance_after=10, reason=PointReason.SIGN_IN
        )
        other = User.objects.create_user(
            username=f"other_{stamp}", phone="138" + stamp[-8:]
        )
        ok("创建用户 + 初始积分", f"user_no={user.user_no} phone={phone}")

        # ── 1. 用户侧：签发上传 ────────────────────────────────────
        req = rf.post("/api/v1/uploads/sign", {"size": 2 * 1024 * 1024}, format="json")
        force_authenticate(req, user=user)
        data = expect(views_user.sign_upload(req), label="uploads/sign")
        task_id, task_no = data["task_id"], data["task_no"]
        assert task_no and task_no.startswith("T"), data
        ok("POST /uploads/sign", f"task_no={task_no} 已签发直传凭证")

        # ── 2. 确认上传 → 扣积分 → 入队 ────────────────────────────
        # OSS 对象检查用桩：真实文件要靠浏览器直传，冒烟只验证服务端状态机
        with patch.object(oss, "object_exists", return_value=True), patch.object(
            oss, "object_size", return_value=2 * 1024 * 1024
        ):
            req = rf.post(
                f"/api/v1/tasks/{task_id}/confirm",
                {"duration_ms": 12_000, "width": 1920, "height": 1080, "fps": 30},
                format="json",
            )
            force_authenticate(req, user=user)
            data = expect(views_user.confirm_upload(req, task_id=task_id), label="confirm")
        assert data["status"] == TaskStatus.PENDING, data
        assert data["points_cost"] > 0, data
        ok("POST /tasks/{id}/confirm", f"status=pending 扣分={data['points_cost']}")

        # ── 3. 按业务号查询（task_no 也要能查）────────────────────
        req = rf.get(f"/api/v1/tasks/{task_no}")
        force_authenticate(req, user=user)
        data = expect(views_user.task_detail(req, task_id=task_no), label="detail by no")
        assert data["task_no"] == task_no and data["preview_url"], data
        ok("GET /tasks/{task_no}", "业务号查询通、原片签名地址已返回")

        # 他人拿同一个 task_no 查 → 404
        req = rf.get(f"/api/v1/tasks/{task_no}")
        force_authenticate(req, user=other)
        expect(views_user.task_detail(req, task_id=task_no), code=404, label="越权")
        ok("越权隔离", "他人用 task_no 查询 → 404")

        # ── 4. Worker 领取 ────────────────────────────────────────
        worker, raw_token = lease.make_worker(f"smoke-worker-{stamp}")
        req = rf.post(
            "/api/v1/worker/claim",
            {"capabilities": {"device": "cpu"}},
            format="json",
            HTTP_X_WORKER_TOKEN=raw_token,
        )
        data = expect(views_worker.claim(req), label="claim")
        assert data["task_id"].replace("-", "") == task_id.replace("-", ""), data
        assert data["video_url"] and data["result_upload_url"], data
        ok("POST /worker/claim", f"节点 {worker.worker_no} 领到任务，签名 URL 齐全")

        # ── 5. 定位失败上报 → 转 needs_manual（核心 M3.1）──────────
        candidates = [
            {"x": 1484, "y": 44, "w": 392, "h": 136, "std_ratio": 0.72},
            {"x": 1520, "y": 60, "w": 300, "h": 90, "std_ratio": 0.88},
        ]
        req = rf.post(
            "/api/v1/worker/fail",
            {
                "task_id": task_id,
                "error_code": "LOCATE_LOW_CONFIDENCE",
                "error_message": "自动定位不可信：候选块位置不恒定",
                "retryable": False,
                "candidates": candidates,
            },
            format="json",
            HTTP_X_WORKER_TOKEN=raw_token,
        )
        data = expect(views_worker.fail(req), label="fail")
        assert data["status"] == TaskStatus.NEEDS_MANUAL, data
        ok("POST /worker/fail (定位低置信度)", f"status={data['status']} 未判失败、未退款")

        task = Task.objects.get(pk=task_id)
        assert task.params.get("locate_candidates"), task.params
        assert not task.refunded_at, "转人工不该退款"
        ok("候选框已落库", f"{len(task.params['locate_candidates'])} 个候选，refunded_at 为空")

        # ── 6. 用户拉详情，拿到框选所需上下文 ─────────────────────
        req = rf.get(f"/api/v1/tasks/{task_id}")
        force_authenticate(req, user=user)
        data = expect(views_user.task_detail(req, task_id=task_id), label="detail manual")
        for key in ("locate_candidates", "locate_reason", "frame_width", "frame_height"):
            assert key in data, f"detail 缺字段 {key}；实得 {sorted(data)}"
        assert data["frame_width"] == 1920 and data["frame_height"] == 1080, data
        ok("GET /tasks/{id} (needs_manual)", "候选框 / 原因 / 画面尺寸齐全，前端可框选")

        # ── 7. 提交 bbox → 回队列，且不再扣积分 ───────────────────
        from apps.billing.services import get_balance

        balance_before = get_balance(user)
        bbox = [1484, 44, 392, 136]
        req = rf.post(f"/api/v1/tasks/{task_id}/bbox", {"bbox": bbox}, format="json")
        force_authenticate(req, user=user)
        data = expect(views_user.submit_bbox(req, task_id=task_id), label="submit bbox")
        assert data["status"] == TaskStatus.PENDING and data["bbox"] == bbox, data

        task.refresh_from_db()
        balance_after = get_balance(user)
        assert task.params["bbox"] == bbox, task.params
        assert task.attempt == 0 and task.worker is None, "attempt/worker 未重置"
        assert balance_after == balance_before, f"重复扣分：{balance_before} → {balance_after}"
        ok("POST /tasks/{id}/bbox", f"bbox={bbox} 回 pending、attempt=0、积分不变 {balance_after}")

        # 非法 bbox 要拦住
        req = rf.post(f"/api/v1/tasks/{task_id}/bbox", {"bbox": [1, 2, -3, 4]}, format="json")
        force_authenticate(req, user=user)
        expect(views_user.submit_bbox(req, task_id=task_id), code=409, label="重复提交")
        ok("状态守卫", "非 needs_manual 再提交 → 409")

        # ── 8. 重新入队后 worker 能再领走 ─────────────────────────
        req = rf.post(
            "/api/v1/worker/claim",
            {"capabilities": {"device": "cpu"}},
            format="json",
            HTTP_X_WORKER_TOKEN=raw_token,
        )
        data = expect(views_worker.claim(req), label="claim again")
        assert data.get("params", {}).get("bbox") == bbox, data
        ok("POST /worker/claim (二次)", "任务带 bbox 重新入队，worker 可跳过自动定位")

        # 模型默认 ordering = ['-id']（时间线最新在上），这里按 id 正序还原真实流转
        events = list(
            TaskEvent.objects.filter(task=task)
            .order_by("id")
            .values_list("to_status", flat=True)
        )
        assert "needs_manual" in events, events
        ok("状态流水完整", " → ".join(events))

        # ★ 关键：全部回滚，调试库不留任何冒烟数据
        transaction.set_rollback(True)

    print(f"\n✅ 调试库冒烟全部通过（{len(PASS)} 项），事务已回滚，库内无残留")
    return 0


if __name__ == "__main__":
    # 调试库通常只配了 MySQL，OSS 三个凭据为空。外部存储调用统一打桩：
    # 签名策略（本地计算）与签名 URL 给假值，对象存在/大小给真值桩。
    # 这样不改 main 里任何业务逻辑，也能完整验证服务端状态机。
    _stubs = [
        patch.object(
            oss,
            "build_post_policy",
            return_value={"host": "https://oss.example.com", "key": "stub", "policy": "stub"},
        ),
        patch.object(oss, "presign_get", return_value="https://oss.example.com/signed"),
        patch.object(oss, "presign_put", return_value="https://oss.example.com/upload"),
        patch.object(oss, "object_exists", return_value=True),
        patch.object(oss, "object_size", return_value=2 * 1024 * 1024),
    ]
    for _stub in _stubs:
        _stub.start()
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 冒烟失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise
    finally:
        for _stub in reversed(_stubs):
            _stub.stop()

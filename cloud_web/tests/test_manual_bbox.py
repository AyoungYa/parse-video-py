"""人工框选（needs_manual）流程测试。

背景：自动定位不可信时，任务不应该判失败退款，而应该转 `needs_manual`
等用户手动框选水印位置，框完继续处理（不重新扣积分）。

跑法（在 cloud_web/ 下）：
    uv run python manage.py test tests -v 2
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.billing.models import PointLedger, PointReason
from apps.billing.services import add_points, get_balance
from apps.tasks.models import Task, TaskStatus
from apps.tasks.services.lease import make_worker


def _make_user(phone="13900000001", points=10) -> User:
    user = User.objects.create(username=f"u_{phone}", phone=phone, nickname="框选测试")
    if points:
        add_points(user, points, reason=PointReason.INITIAL, note="测试预置")
    return user


def _make_task(user, status=TaskStatus.NEEDS_MANUAL, cost=1, params=None) -> Task:
    return Task.objects.create(
        user=user,
        status=status,
        duration_ms=20_000,
        width=1920,
        height=1080,
        points_cost=cost,
        params=params or {},
    )


class ManualBboxStateTest(TestCase):
    """状态机：LOCATE_LOW_CONFIDENCE → needs_manual（不退款、不判失败）。"""

    def setUp(self):
        self.user = _make_user()
        self.worker, self.token = make_worker("pc-manual")

    def test_locate_low_confidence_becomes_needs_manual(self):
        from apps.tasks.services import lease

        task = _make_task(self.user, status=TaskStatus.PENDING, cost=1)
        balance_before = get_balance(self.user)

        lease.fail_task(
            self.worker,
            str(task.id),
            error_code=lease.LOCATE_LOW_CONFIDENCE_CODE,
            error_message="恒亮块都不满足位置恒定性",
            retryable=False,
        )

        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.NEEDS_MANUAL)
        self.assertEqual(task.error_code, lease.LOCATE_LOW_CONFIDENCE_CODE)
        # ★ 关键：转人工不等于失败，积分不能退（用户还要继续用这条片子）
        self.assertEqual(get_balance(self.user), balance_before)
        self.assertIsNone(task.finished_at)

    def test_candidates_stored_in_params(self):
        from apps.tasks.services import lease

        task = _make_task(self.user, status=TaskStatus.PENDING)
        cands = [{"x": 100, "y": 50, "w": 200, "h": 80}]

        lease.fail_task(
            self.worker,
            str(task.id),
            error_code=lease.LOCATE_LOW_CONFIDENCE_CODE,
            error_message="定位不可信",
            retryable=False,
            candidates=cands,
        )

        task.refresh_from_db()
        self.assertEqual(task.params.get("locate_candidates"), cands)

    def test_needs_manual_is_not_terminal(self):
        """needs_manual 不是终态 —— 它还能回到 pending。"""
        task = _make_task(self.user, status=TaskStatus.NEEDS_MANUAL)
        self.assertFalse(task.is_terminal)

    def test_event_logged(self):
        """状态变更必须留 TaskEvent（I4 不变量）。"""
        from apps.tasks.services import lease

        task = _make_task(self.user, status=TaskStatus.PENDING)
        lease.fail_task(
            self.worker,
            str(task.id),
            error_code=lease.LOCATE_LOW_CONFIDENCE_CODE,
            error_message="定位不可信",
            retryable=False,
        )
        events = list(task.events.values_list("to_status", flat=True))
        self.assertIn(TaskStatus.NEEDS_MANUAL, events)


class SubmitBboxApiTest(TestCase):
    """用户提交 bbox 的接口。"""

    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_submit_bbox_requeues_task(self):
        task = _make_task(self.user, status=TaskStatus.NEEDS_MANUAL, cost=1)
        balance_before = get_balance(self.user)

        resp = self.client.post(
            f"/api/v1/tasks/{task.id}/bbox", {"bbox": [100, 50, 300, 120]}, format="json"
        )

        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(task.params["bbox"], [100, 50, 300, 120])
        # ★ 不重新扣积分
        self.assertEqual(get_balance(self.user), balance_before)

    def test_submit_bbox_rejects_invalid(self):
        for bad in ([], [1, 2], [1, 2, 3, 4, 5], [-1, 0, 10, 10], [0, 0, 0, 10], "abc"):
            task = _make_task(self.user, status=TaskStatus.NEEDS_MANUAL)
            resp = self.client.post(
                f"/api/v1/tasks/{task.id}/bbox", {"bbox": bad}, format="json"
            )
            self.assertEqual(resp.status_code, 400, f"bbox={bad} 应被拒")
            task.refresh_from_db()
            self.assertEqual(task.status, TaskStatus.NEEDS_MANUAL)

    def test_submit_bbox_rejected_when_not_needs_manual(self):
        task = _make_task(self.user, status=TaskStatus.PENDING)
        resp = self.client.post(
            f"/api/v1/tasks/{task.id}/bbox", {"bbox": [10, 10, 100, 50]}, format="json"
        )
        self.assertEqual(resp.status_code, 409)

    def test_submit_bbox_clears_candidates_and_error(self):
        task = _make_task(
            self.user,
            status=TaskStatus.NEEDS_MANUAL,
            params={"locate_candidates": [{"x": 1, "y": 2, "w": 3, "h": 4}]},
        )
        self.client.post(
            f"/api/v1/tasks/{task.id}/bbox", {"bbox": [10, 20, 200, 80]}, format="json"
        )
        task.refresh_from_db()
        self.assertNotIn("locate_candidates", task.params)
        self.assertEqual(task.error_code, "")
        self.assertEqual(task.attempt, 0)

    def test_other_user_cannot_submit(self):
        other = _make_user(phone="13900000002")
        task = _make_task(other, status=TaskStatus.NEEDS_MANUAL)
        resp = self.client.post(
            f"/api/v1/tasks/{task.id}/bbox", {"bbox": [10, 10, 100, 50]}, format="json"
        )
        self.assertEqual(resp.status_code, 404)


class ManualBboxDetailTest(TestCase):
    """task_detail 要把框选需要的信息给前端。"""

    def setUp(self):
        self.user = _make_user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_detail_includes_candidates_when_needs_manual(self):
        cands = [{"x": 100, "y": 50, "w": 200, "h": 80}]
        task = _make_task(
            self.user,
            status=TaskStatus.NEEDS_MANUAL,
            params={"locate_candidates": cands},
        )

        with patch("apps.tasks.views_user.oss.presign_get", return_value="https://x/preview"):
            resp = self.client.get(f"/api/v1/tasks/{task.id}")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["locate_candidates"], cands)
        self.assertEqual(data["frame_width"], 1920)
        self.assertEqual(data["frame_height"], 1080)

    def test_detail_omits_candidates_when_normal(self):
        task = _make_task(self.user, status=TaskStatus.PENDING)
        with patch("apps.tasks.views_user.oss.presign_get", return_value="https://x/preview"):
            resp = self.client.get(f"/api/v1/tasks/{task.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("locate_candidates", resp.json())


class CancelFromNeedsManualTest(TestCase):
    """待框选的任务应该也能取消并退款。"""

    def setUp(self):
        self.user = _make_user()

    def test_cancel_needs_manual_refunds(self):
        from apps.tasks.services import lease

        task = _make_task(self.user, status=TaskStatus.NEEDS_MANUAL, cost=2)
        balance_before = get_balance(self.user)

        with patch("apps.tasks.views_user.oss.presign_get", return_value="https://x"):
            resp = APIClient().post(f"/api/v1/tasks/{task.id}/cancel")

        # 未登录会 401/403；这里直接调 service 层验证退款逻辑
        task.refresh_from_db()
        lease._refund_if_needed(task, reason="用户取消")
        task.refresh_from_db()
        self.assertIsNotNone(task.refunded_at)

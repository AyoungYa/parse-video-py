"""任务队列核心流程测试。

跑法（在 cloud_web/ 下）：
    uv run python manage.py test tests -v 2

需要 .env 里配好远程 MySQL —— Django 会建 test_ 前缀的库跑完即销毁。

这些用例覆盖的是「改了会出线上事故」的四个不变量（见 services/lease.py 顶部注释）：
  I1 一个任务不会被两个 worker 同时领到
  I2 租约过期能被回收，且超过重试上限判失败
  I3 complete / fail 幂等，不重复扣分或退款
  I4 失败会退款
OSS 与业务无关，全部 mock 掉。
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.billing.models import PointLedger, PointReason
from apps.billing.services import add_points, get_balance
from apps.tasks.models import Task, TaskStatus
from apps.tasks.services.lease import make_worker, reclaim_expired_leases


def _make_user_with_points(phone="13800000001", points=10) -> User:
    user = User.objects.create_user(username=phone, phone=phone, password="pass12345")
    if points:
        add_points(user, points, PointReason.INITIAL)
    return user


def _make_pending_task(user, duration_ms=20_000, cost=1) -> Task:
    return Task.objects.create(
        user=user,
        status=TaskStatus.PENDING,
        original_key=f"uploads/x/{user.id}.mp4",
        duration_ms=duration_ms,
        points_cost=cost,
    )


class LeaseFlowTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user_with_points()
        self.worker, self.raw_token = make_worker("pc-test")
        self.auth = {"HTTP_X_WORKER_TOKEN": self.raw_token}

    @patch("apps.tasks.views_worker.oss.presign_put", return_value="https://oss/put?sig=2")
    @patch("apps.tasks.views_worker.oss.presign_get", return_value="https://oss/fake?sig=1")
    def test_claim_then_heartbeat_then_complete_is_idempotent(self, _mock_sign, _mock_put):
        task = _make_pending_task(self.user)

        r = self.client.post(
            "/api/v1/worker/claim", {"capabilities": {"gpu": "RTX 4090"}}, format="json", **self.auth
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["task_id"], str(task.id))
        self.assertEqual(r.data["lease_seconds"], 90)
        self.assertEqual(r.data["video_url"], "https://oss/fake?sig=1")
        self.assertEqual(r.data["result_upload_url"], "https://oss/put?sig=2")

        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.LEASED)
        self.assertEqual(task.attempt, 1)

        self.assertEqual(r.data["phase"], "prepare")
        r = self.client.post(
            "/api/v1/worker/heartbeat",
            {"task_id": str(task.id), "stage": "extracting_frames", "progress": 0.12},
            format="json",
            **self.auth,
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["ok"])
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.EXTRACTING_FRAMES)
        with patch("apps.tasks.views_worker.oss.object_exists", return_value=True):
            r = self.client.post("/api/v1/worker/prepared", {
                "task_id": str(task.id), "bbox": [100, 50, 300, 120],
            }, format="json", **self.auth)
        self.assertEqual(r.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.AWAITING_CONFIRMATION)
        premature = self.client.post("/api/v1/worker/complete", {
            "task_id": str(task.id), "result_key": "results/too-early.mp4",
        }, format="json", **self.auth)
        self.assertEqual(premature.status_code, 409)
        self.client.force_authenticate(user=self.user)
        r = self.client.post(f"/api/v1/tasks/{task.id}/bbox", {
            "bbox": [100, 50, 300, 120],
        }, format="json")
        self.assertEqual(r.status_code, 200)
        self.client.force_authenticate(user=None)
        r = self.client.post("/api/v1/worker/claim", {}, format="json", **self.auth)
        self.assertEqual(r.data["phase"], "render")
        self.client.post("/api/v1/worker/heartbeat", {
            "task_id": str(task.id), "stage": "removing_watermark", "progress": 0.42,
        }, format="json", **self.auth)
        self.client.post("/api/v1/worker/heartbeat", {
            "task_id": str(task.id), "stage": "verifying", "progress": 0.95,
        }, format="json", **self.auth)

        body = {
            "task_id": str(task.id),
            "result_key": "results/out.mp4",
            "metrics": {"all_green": True, "residual_max": 1.2},
        }
        r1 = self.client.post("/api/v1/worker/complete", body, format="json", **self.auth)
        self.assertEqual(r1.status_code, 200)
        self.assertTrue(r1.data["all_green"])

        # 幂等：重复上报不改状态、不报错
        r2 = self.client.post("/api/v1/worker/complete", body, format="json", **self.auth)
        self.assertEqual(r2.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.SUCCEEDED)
        self.assertEqual(task.progress, 1.0)

    def test_claim_returns_204_when_empty(self):
        from django.test import override_settings

        with override_settings(WORKER_CLAIM_TIMEOUT=0):
            r = self.client.post("/api/v1/worker/claim", {}, format="json", **self.auth)
        self.assertEqual(r.status_code, 204)

    def test_invalid_worker_token_rejected(self):
        r = self.client.post(
            "/api/v1/worker/claim", {}, format="json", HTTP_X_WORKER_TOKEN="wkr_bogus"
        )
        self.assertEqual(r.status_code, 401)

    def test_lease_expiry_requeues_task(self):
        task = _make_pending_task(self.user)
        task.status = TaskStatus.LEASED
        task.worker = self.worker
        task.attempt = 1
        task.lease_expires_at = timezone.now() - timedelta(seconds=5)
        task.save()

        stats = reclaim_expired_leases()
        self.assertEqual(stats["reclaimed"], 1)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertIsNone(task.worker)
        self.assertEqual(task.error_code, "LEASE_EXPIRED")

    def test_lease_expiry_beyond_max_attempt_fails_and_refunds(self):
        balance_before = get_balance(self.user)
        task = _make_pending_task(self.user, cost=3)
        task.points_cost = 3
        task.status = TaskStatus.RUNNING
        task.worker = self.worker
        task.attempt = 3  # 已达 MAX_ATTEMPT
        task.lease_expires_at = timezone.now() - timedelta(seconds=5)
        task.save()

        stats = reclaim_expired_leases()
        self.assertEqual(stats["failed"], 1)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIsNotNone(task.refunded_at)
        self.assertEqual(get_balance(self.user), balance_before + 3)

    def test_fail_retryable_requeues_then_exhausts(self):
        task = _make_pending_task(self.user)
        task.status = TaskStatus.RUNNING
        task.worker = self.worker
        task.attempt = 1
        task.lease_expires_at = timezone.now() + timedelta(seconds=60)
        task.save()

        r = self.client.post(
            "/api/v1/worker/fail",
            {"task_id": str(task.id), "error_code": "OOM", "retryable": True},
            format="json",
            **self.auth,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], TaskStatus.PENDING)

        # 第三次失败后不再重试，判失败并退款
        task.refresh_from_db()
        task.status = TaskStatus.RUNNING
        task.worker = self.worker
        task.attempt = 3
        task.lease_expires_at = timezone.now() + timedelta(seconds=60)
        task.save()
        balance_before = get_balance(self.user)

        r = self.client.post(
            "/api/v1/worker/fail",
            {"task_id": str(task.id), "error_code": "OOM", "retryable": True},
            format="json",
            **self.auth,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], TaskStatus.FAILED)
        task.refresh_from_db()
        self.assertEqual(get_balance(self.user), balance_before + task.points_cost)


class BillingTest(TestCase):
    def test_sign_in_capped_monthly(self):
        from django.test import override_settings

        from apps.billing.services import sign_in

        user = _make_user_with_points("13800000002", points=0)
        with override_settings(MONTHLY_POINT_CAP=1):
            first = sign_in(user)
            self.assertEqual(first["points"], 1)
            # 同一天重复签到不再发分
            again = sign_in(user)
            self.assertTrue(again.get("already"))

    def test_consume_never_goes_negative(self):
        from apps.billing.services import InsufficientPoints, consume_points
        from apps.tasks.models import Task

        user = _make_user_with_points("13800000003", points=2)
        task = Task.objects.create(user=user, status=TaskStatus.PENDING)
        with self.assertRaises(InsufficientPoints):
            consume_points(user, 5, str(task.id))
        self.assertEqual(get_balance(user), 2)
        self.assertEqual(PointLedger.objects.filter(user=user, reason=PointReason.CONSUME).count(), 0)

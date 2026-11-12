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
            for index in range(3):
                frame_response = self.client.post("/api/v1/worker/frame", {
                    "task_id": str(task.id), "index": index,
                }, format="json", **self.auth)
                self.assertEqual(frame_response.status_code, 200)
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

    def test_quality_not_green_refunds_once_after_delivery(self):
        task = _make_pending_task(self.user, cost=2)
        task.status = TaskStatus.VERIFYING
        task.worker = self.worker
        task.params = {"bbox_confirmed": True, "bbox": [100, 50, 300, 120]}
        task.lease_expires_at = timezone.now() + timedelta(seconds=60)
        task.save()
        balance_before = get_balance(self.user)
        body = {"task_id": str(task.id), "result_key": "results/not-green.mp4",
                "metrics": {"all_green": False}}
        for _ in range(2):
            response = self.client.post("/api/v1/worker/complete", body, format="json", **self.auth)
            self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.SUCCEEDED)
        self.assertIsNotNone(task.refunded_at)
        self.assertEqual(get_balance(self.user), balance_before + 2)

    def test_invalid_worker_token_rejected(self):
        r = self.client.post(
            "/api/v1/worker/claim", {}, format="json", HTTP_X_WORKER_TOKEN="wkr_bogus"
        )
        self.assertEqual(r.status_code, 401)

    @patch("apps.tasks.views_worker.oss.presign_put", return_value="https://oss/put")
    @patch("apps.tasks.views_worker.oss.presign_get", return_value="https://oss/get")
    def test_restart_releases_only_owned_task_without_spending_attempt(self, _get, _put):
        task = _make_pending_task(self.user)
        self.client.post("/api/v1/worker/claim", {}, format="json", **self.auth)
        other, other_token = make_worker("other-node")
        denied = self.client.post(
            "/api/v1/worker/release", {"task_id": str(task.id)}, format="json",
            HTTP_X_WORKER_TOKEN=other_token,
        )
        self.assertEqual(denied.status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.LEASED)
        released = self.client.post(
            "/api/v1/worker/release", {"task_id": str(task.id)}, format="json", **self.auth,
        )
        self.assertEqual(released.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertIsNone(task.worker)
        self.assertIsNone(task.lease_expires_at)
        self.assertEqual(task.attempt, 0)
        self.assertTrue(task.events.filter(to_status=TaskStatus.PENDING).exists())
        again = self.client.post("/api/v1/worker/claim", {}, format="json", **self.auth)
        self.assertEqual(again.data["task_id"], str(task.id))
        task.refresh_from_db()
        self.assertEqual(task.attempt, 1)

    @patch("apps.tasks.views_worker.oss.presign_put", return_value="https://oss/put")
    @patch("apps.tasks.views_worker.oss.presign_get", return_value="https://oss/get")
    def test_claim_reclaims_expired_task_after_hard_crash(self, _get, _put):
        task = _make_pending_task(self.user)
        task.status = TaskStatus.RUNNING
        task.worker = self.worker
        task.attempt = 1
        task.lease_expires_at = timezone.now() - timedelta(seconds=1)
        task.save()
        response = self.client.post("/api/v1/worker/claim", {}, format="json", **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["task_id"], str(task.id))
        task.refresh_from_db()
        self.assertEqual(task.attempt, 2)

    @patch("apps.tasks.services.oss.object_exists", return_value=True)
    def test_checkpoint_reports_each_render_stage_and_rejects_other_worker(self, _exists):
        task = _make_pending_task(self.user)
        task.status = TaskStatus.REMOVING_WATERMARK
        task.worker = self.worker
        task.attempt = 2
        task.lease_expires_at = timezone.now() + timedelta(minutes=5)
        task.params = {"bbox_confirmed": True, "bbox": [10, 20, 100, 50]}
        task.save()
        other, other_token = make_worker("other-checkpoint-node")
        denied = self.client.post(
            "/api/v1/worker/checkpoint", {"task_id": str(task.id), "stage": "build_mask"},
            format="json", HTTP_X_WORKER_TOKEN=other_token,
        )
        self.assertEqual(denied.status_code, 409)
        for stage in ("build_mask", "lama_apply", "verify"):
            response = self.client.post(
                "/api/v1/worker/checkpoint",
                {"task_id": str(task.id), "stage": stage, "metrics": {"all_green": True}},
                format="json", **self.auth,
            )
            self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(set(task.checkpoints), {"build_mask", "lama_apply", "verify"})
        self.assertEqual(task.attempt, 0)
        self.assertEqual(task.checkpoints["verify"]["metrics"], {"all_green": True})
        self.assertEqual(task.events.filter(detail__checkpoint="verify").count(), 1)

    @patch("apps.tasks.services.oss.object_exists", return_value=True)
    def test_keyframe_report_persists_for_takeover(self, _exists):
        task = _make_pending_task(self.user)
        task.status = TaskStatus.EXTRACTING_FRAMES
        task.worker = self.worker
        task.attempt = 2
        task.lease_expires_at = timezone.now() + timedelta(minutes=5)
        task.checkpoints = {"_active_run_id": "first-run", "_run_ids": ["first-run"]}
        task.save()
        response = self.client.post(
            "/api/v1/worker/frame", {"task_id": str(task.id), "index": 0},
            format="json", **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        from apps.tasks.services import oss
        self.assertEqual(task.attempt, 0)
        self.assertEqual(
            task.checkpoints["keyframes"]["0"],
            oss.keyframe_key(str(task.id), 0, "first-run"),
        )
        self.assertEqual(task.events.filter(detail__keyframe=0).count(), 1)

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


class RetryExistingTaskTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _make_user_with_points(phone="13800000009")
        self.client.force_authenticate(user=self.user)
        self.task = Task.objects.create(
            user=self.user,
            status=TaskStatus.FAILED,
            original_key="uploads/retry/source.mp4",
            points_cost=1,
            refunded_at=timezone.now(),
            finished_at=timezone.now(),
            attempt=3,
            error_code="STAGE_FAILED",
            error_message="Traceback: internal path",
            checkpoints={"build_mask": {"key": "checkpoints/mask.png"}},
        )

    @patch("apps.tasks.views_user.oss.object_exists", return_value=True)
    def test_retry_reuses_upload_and_checkpoints_without_charge(self, _exists):
        balance = get_balance(self.user)
        url = f"/api/v1/tasks/{self.task.id}/retry"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TaskStatus.PENDING)
        self.assertEqual(self.task.original_key, "uploads/retry/source.mp4")
        self.assertIn("build_mask", self.task.checkpoints)
        self.assertEqual(self.task.points_cost, 0)
        self.assertEqual(self.task.attempt, 0)
        self.assertIsNone(self.task.finished_at)
        self.assertEqual(get_balance(self.user), balance)
        self.assertEqual(self.client.post(url).status_code, 409)

    @patch("apps.tasks.views_user.oss.object_exists", return_value=False)
    def test_retry_requires_original_object(self, _exists):
        response = self.client.post(f"/api/v1/tasks/{self.task.id}/retry")
        self.assertEqual(response.status_code, 410)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TaskStatus.FAILED)


class BillingTest(TestCase):
    def test_sign_in_once_per_day(self):
        from apps.billing.services import sign_in

        user = _make_user_with_points("13800000002", points=0)
        first = sign_in(user)
        self.assertEqual(first["points"], 1)
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

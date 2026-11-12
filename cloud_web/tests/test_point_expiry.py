"""免费积分批次、到期、扣减与退款的关键不变量。"""

from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import PointLot, PointReason, SignInRecord
from apps.billing.services import (
    add_points, consume_points, ensure_initial_grant, get_balance, points_for_duration, refund_points, sign_in,
)


class PointPriceTest(SimpleTestCase):
    def test_every_full_ten_seconds_minimum_one(self):
        for milliseconds, expected in ((1000, 1), (9999, 1), (10000, 1), (36000, 3), (30000, 3)):
            self.assertEqual(points_for_duration(milliseconds), expected)


class PointExpiryTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="13800009999", phone="13800009999", password="test")

    def test_new_user_gets_100_points_once(self):
        ensure_initial_grant(self.user)
        ensure_initial_grant(self.user)
        self.assertEqual(get_balance(self.user), 100)
        lot = PointLot.objects.get(user=self.user)
        self.assertEqual(lot.remaining, 100)
        self.assertAlmostEqual((lot.expires_at - lot.created_at).total_seconds(), 10 * 86400, delta=2)

    def test_gift_expires_after_ten_days(self):
        now = timezone.now()
        with patch("apps.billing.services.timezone.now", return_value=now):
            add_points(self.user, 3, PointReason.INITIAL)
        self.assertEqual(get_balance(self.user), 3)
        with patch("apps.billing.services.timezone.now", return_value=now + timedelta(days=10, seconds=1)):
            self.assertEqual(get_balance(self.user), 0)
            self.assertEqual(get_balance(self.user), 0)
        self.assertEqual(PointLot.objects.get(user=self.user).remaining, 0)

    def test_earliest_expiring_points_spent_first_and_refund_keeps_value(self):
        now = timezone.now()
        with patch("apps.billing.services.timezone.now", return_value=now):
            add_points(self.user, 3, PointReason.INITIAL)
        with patch("apps.billing.services.timezone.now", return_value=now + timedelta(days=1)):
            add_points(self.user, 2, PointReason.SIGN_IN)
            consume_points(self.user, 4, "task-1")
        self.assertEqual(list(PointLot.objects.filter(user=self.user).order_by("id").values_list("remaining", flat=True)), [0, 1])
        with patch("apps.billing.services.timezone.now", return_value=now + timedelta(days=12)):
            refund_points(self.user, 4, "task-1")
            self.assertEqual(get_balance(self.user), 4)

    def test_streak_bonus_continues_after_seventh_day(self):
        today = timezone.localdate()
        for day in range(8):
            date = today + timedelta(days=day)
            with patch("apps.billing.services.timezone.localdate", return_value=date):
                result = sign_in(self.user, date)
            self.assertEqual(result["points"], 6 if day == 6 else 1)
        self.assertEqual(SignInRecord.objects.filter(user=self.user).count(), 8)

"""手机号 + 验证码登录：自动建号、单次核销、限流、错误次数。

这些是全站唯一的入口，坏了等于所有人都登不进来，所以覆盖得细一点。
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import SmsCode, User

PHONE = "13800138000"


@override_settings(SMS_PROVIDER="console", SMS_DEV_ECHO=True)
class SmsLoginTests(TestCase):
    def setUp(self):
        cache.clear()  # 限流计数在缓存里，避免测试之间互相影响
        self.client = APIClient()

    def _send(self, phone: str = PHONE) -> dict:
        r = self.client.post("/api/v1/auth/sms/send", {"phone": phone}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _login(self, code: str, phone: str = PHONE):
        return self.client.post(
            "/api/v1/auth/sms/login", {"phone": phone, "code": code}, format="json"
        )

    # ---- 正常路径 ----

    def test_first_login_creates_account_and_grants_trial(self):
        code = self._send()["dev_code"]

        r = self._login(code)

        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["is_new"])
        self.assertTrue(r.data["token"].startswith("utk_"))
        user = User.objects.get(phone=PHONE)
        self.assertEqual(user.nickname, "用户8000")
        self.assertFalse(user.has_usable_password())  # 验证码登录，无密码

        # token 立即可用
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['token']}")
        me = self.client.get("/api/v1/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.data["phone"], PHONE)
        self.assertGreater(me.data["points"], 0)  # 新用户试用额度已发

    def test_second_login_reuses_account(self):
        code = self._send()["dev_code"]
        first = self._login(code)
        self.assertTrue(first.data["is_new"])

        # 绕过 60s 重发间隔（模拟隔天再登录）
        SmsCode.objects.update(created_at=timezone.now() - timedelta(minutes=5))
        code2 = self._send()["dev_code"]
        second = self._login(code2)

        self.assertEqual(second.status_code, 200, second.data)
        self.assertFalse(second.data["is_new"])
        self.assertEqual(User.objects.filter(phone=PHONE).count(), 1)

    # ---- 验证码校验 ----

    def test_wrong_code_rejected(self):
        self._send()
        r = self._login("000000")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "sms_code_wrong")
        self.assertEqual(User.objects.count(), 0)

    def test_code_is_single_use(self):
        code = self._send()["dev_code"]
        self.assertEqual(self._login(code).status_code, 201)

        replay = self._login(code)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.data["code"], "sms_code_not_found")

    def test_expired_code_rejected(self):
        code = self._send()["dev_code"]
        SmsCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        r = self._login(code)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "sms_code_expired")

    def test_too_many_attempts_locks_code(self):
        code = self._send()["dev_code"]
        for _ in range(5):
            self._login("000000")

        r = self._login(code)  # 正确码也已被锁
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "sms_code_locked")

    def test_login_without_sending_rejected(self):
        r = self._login("123456")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "sms_code_not_found")

    # ---- 发送限流 / 入参 ----

    def test_resend_within_60s_rejected(self):
        self._send()
        r = self.client.post("/api/v1/auth/sms/send", {"phone": PHONE}, format="json")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.data["code"], "sms_too_frequent")
        self.assertGreater(r.data["retry_after"], 0)

    def test_daily_phone_cap(self):
        # 直接造 10 条历史记录（绕过 60s 间隔），第 11 次应被拦
        now = timezone.now()
        for i in range(10):
            record = SmsCode.objects.create(
                phone=PHONE,
                code_hash=SmsCode.hash_code("123456"),
                expires_at=now + timedelta(minutes=5),
            )
            # auto_now_add 会覆盖传入值，只能建完再改
            SmsCode.objects.filter(pk=record.pk).update(
                created_at=now - timedelta(minutes=30 - i * 2)
            )
        r = self.client.post("/api/v1/auth/sms/send", {"phone": PHONE}, format="json")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.data["code"], "sms_daily_phone_cap")

    def test_invalid_phone_rejected(self):
        for bad in ("12345", "12800138000", "1380013800a", ""):
            r = self.client.post("/api/v1/auth/sms/send", {"phone": bad}, format="json")
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.data["code"], "invalid_phone")

    def test_dev_echo_can_be_disabled(self):
        with override_settings(SMS_DEV_ECHO=False):
            r = self.client.post("/api/v1/auth/sms/send", {"phone": PHONE}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("dev_code", r.data)

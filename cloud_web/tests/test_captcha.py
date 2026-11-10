"""图形验证码：签发、单次核销、错误次数、过期，以及它对短信接口的闸门作用。

图形码是短信费的第一道闸门，坏了等于给刷子开门，所以单测按「攻击者视角」覆盖。
"""

from __future__ import annotations

import base64
from datetime import timedelta

from django.utils import timezone
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import CaptchaChallenge, SmsCode

PHONE = "13800138000"


@override_settings(SMS_PROVIDER="console", SMS_DEV_ECHO=True, CAPTCHA_DEV_ECHO=True)
class CaptchaTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def _issue(self) -> dict:
        r = self.client.get("/api/v1/auth/captcha")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _answer(self, item: dict) -> str:
        # 生产拿不到答案（只存哈希）；测试用 dev 回显
        return item["dev_code"]

    def _send_sms(self, item: dict, answer: str | None = None, phone: str = PHONE):
        return self.client.post(
            "/api/v1/auth/sms/send",
            {
                "phone": phone,
                "captcha_id": item["captcha_id"],
                "captcha_code": answer if answer is not None else self._answer(item),
            },
            format="json",
        )

    # ---- 签发 ----

    def test_issue_returns_png_and_no_answer(self):
        item = self._issue()
        # 挑战号 = 1 位前缀 P + 8 位日期 + 12 位 hex（apps.bizno._gen）
        self.assertEqual(len(item["captcha_id"]), 21)
        self.assertIn("data:image/png;base64,", item["image"])
        raw = base64.b64decode(item["image"].split(",", 1)[1])
        self.assertTrue(raw.startswith(b"\x89PNG"))  # 真的是 PNG，不是占位图
        self.assertEqual(item["expires_in"], 120)
        # 库里只存哈希，明文不落库
        row = CaptchaChallenge.objects.get(captcha_no=item["captcha_id"])
        self.assertNotIn(row.code_hash, item["dev_code"].lower())

    def test_answer_is_case_insensitive(self):
        item = self._issue()
        r = self._send_sms(item, answer=self._answer(item).lower())
        self.assertEqual(r.status_code, 200, r.data)

    # ---- 校验失败路径 ----

    def test_missing_captcha_rejected_before_sms(self):
        r = self.client.post("/api/v1/auth/sms/send", {"phone": PHONE}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "captcha_required")
        self.assertEqual(SmsCode.objects.count(), 0)  # 没碰短信通道

    def test_wrong_answer_rejected_and_sms_not_sent(self):
        item = self._issue()
        r = self._send_sms(item, answer="XXXX")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "captcha_wrong")
        self.assertEqual(SmsCode.objects.count(), 0)

    def test_three_wrong_answers_lock_challenge(self):
        item = self._issue()
        for _ in range(3):
            r = self._send_sms(item, answer="XXXX")
            self.assertEqual(r.status_code, 400)
        # 第 4 次（哪怕答对）也已被锁
        r = self._send_sms(item)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "captcha_locked")

    def test_expired_challenge_rejected(self):
        item = self._issue()
        CaptchaChallenge.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        r = self._send_sms(item)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "captcha_expired")

    def test_challenge_is_single_use(self):
        item = self._issue()
        self.assertEqual(self._send_sms(item).status_code, 200)
        replay = self._send_sms(item)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.data["code"], "captcha_used")

    def test_unknown_challenge_id_rejected(self):
        r = self.client.post(
            "/api/v1/auth/sms/send",
            {"phone": PHONE, "captcha_id": "P" + "0" * 19, "captcha_code": "ABCD"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "captcha_not_found")

    # ---- 与短信限流的配合 ----

    def test_correct_captcha_still_hits_resend_gap(self):
        """图形码过了，短信自己的 60s 间隔照样生效——两道闸互不代替。"""
        self._send_sms(self._issue())
        r = self._send_sms(self._issue())
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.data["code"], "sms_too_frequent")

    def test_wrong_captcha_does_not_burn_sms_resend_window(self):
        """答错图形码不该把 60s 重发窗口也烧掉，否则人被自己人误伤。"""
        self._send_sms(self._issue())
        # 立刻答错一次（未过 60s）
        r = self._send_sms(self._issue(), answer="XXXX")
        self.assertEqual(r.status_code, 400)
        # 把上一条短信记录挪出 60s 窗口，再用正确的图形码应能发出去
        SmsCode.objects.update(created_at=timezone.now() - timedelta(minutes=2))
        r = self._send_sms(self._issue())
        self.assertEqual(r.status_code, 200, r.data)

    def test_invalid_phone_does_not_consume_captcha(self):
        """手机号写错时图形码不核销，改对号码后同一张图还能用。"""
        item = self._issue()
        r = self.client.post(
            "/api/v1/auth/sms/send",
            {
                "phone": "12345",
                "captcha_id": item["captcha_id"],
                "captcha_code": "ABCD",
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "invalid_phone")
        # 挑战未被核销
        row = CaptchaChallenge.objects.get(captcha_no=item["captcha_id"])
        self.assertIsNone(row.used_at)

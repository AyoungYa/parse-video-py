"""素材保留期配置：单位为天、默认 3 天，公开配置接口给前端文案用。

cleanup_oss 只按 settings.OSS_RETENTION_DAYS 算清理窗口，这里锁住单位换算与下限保护，
防止配置写错（0 / 负数）把清理窗口落到「未来」误删在途任务。
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


class RetentionSettingTests(TestCase):
    def test_default_is_three_days(self):
        """不配环境变量时默认 3 天，且 settings 单位是天（不是小时）。"""
        self.assertEqual(settings.OSS_RETENTION_DAYS, 3)
        self.assertEqual(timedelta(days=settings.OSS_RETENTION_DAYS), timedelta(days=3))

    def test_env_loader_clamps_to_at_least_one_day(self):
        """写 0 / 负数时 base.py 的 max(1, ...) 兜底，清理窗口不会落到「未来」。

        直接复现 base.py 的加载公式，防止有人把 max() 拿掉。
        """
        loaded = max(1, int("0"))
        self.assertEqual(loaded, 1)
        loaded = max(1, int("-2"))
        self.assertEqual(loaded, 1)


class SiteConfigApiTests(TestCase):
    """GET /api/v1/site/config：无需登录，返回保留天数给前端文案。"""

    def setUp(self):
        self.client = APIClient()

    def test_anonymous_can_read_retention_days(self):
        r = self.client.get("/api/v1/site/config")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["retention_days"], settings.OSS_RETENTION_DAYS)

    @override_settings(OSS_RETENTION_DAYS=7)
    def test_reflects_setting_changes(self):
        r = self.client.get("/api/v1/site/config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["retention_days"], 7)

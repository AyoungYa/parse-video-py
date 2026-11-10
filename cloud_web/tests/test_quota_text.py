"""时长上限文案：秒级额度不能写成「0 分钟」。

2026-11-09 冒烟发现：免费额度 30 **秒**，代码用 `max_seconds // 60` 取整，
用户看到「当前上限 0 分钟」——像额度坏了。这里锁住人话格式。

用 SimpleTestCase：纯函数，不碰数据库，本地没 MySQL 也能跑。
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.tasks.services.quota import MEMBER_MAX_SECONDS, fmt_seconds


class FmtSecondsTests(SimpleTestCase):
    def test_under_one_minute_shows_seconds(self):
        self.assertEqual(fmt_seconds(30), "30 秒")
        self.assertEqual(fmt_seconds(59), "59 秒")

    def test_whole_minutes(self):
        self.assertEqual(fmt_seconds(60), "1 分钟")
        self.assertEqual(fmt_seconds(1800), "30 分钟")

    def test_partial_minutes_keep_one_decimal(self):
        self.assertEqual(fmt_seconds(90), "1.5 分钟")

    def test_member_limit_matches_subscription_rule(self):
        self.assertEqual(MEMBER_MAX_SECONDS, 1800)
        self.assertEqual(fmt_seconds(MEMBER_MAX_SECONDS), "30 分钟")

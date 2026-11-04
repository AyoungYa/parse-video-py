"""积分账本与订阅。

设计原则（见 docs/product/架构设计.md §九）：
  · 积分只用「只增不改的账本行」表达，每行带 balance_after 快照；
  · 绝不对余额做 UPDATE 覆盖，避免并发扣成负数；
  · 预扣发生在「确认上传」，失败全额退还。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class PointReason(models.TextChoices):
    INITIAL = "initial", "新人赠送"
    SIGN_IN = "sign_in", "每日签到"
    STREAK = "streak", "连签奖励"
    CONSUME = "consume", "任务消耗"
    REFUND = "refund", "失败退还"
    SUBSCRIBE = "subscribe", "订阅赠送"


class PointLedger(models.Model):
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="point_entries")
    delta = models.IntegerField("变动值（正加负减）")
    balance_after = models.IntegerField("变动后余额")
    reason = models.CharField(max_length=20, choices=PointReason.choices)
    ref_id = models.CharField("关联对象 ID（任务/订单）", max_length=64, blank=True, default="")
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "point_ledger"
        verbose_name = "积分流水"
        verbose_name_plural = "积分流水"
        indexes = [models.Index(fields=["user", "-created_at"])]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.user_id} {self.delta:+d} → {self.balance_after}"


class SignInRecord(models.Model):
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="sign_ins")
    sign_date = models.DateField("签到日期")
    points = models.IntegerField("获得积分", default=0)
    streak = models.IntegerField("连续天数", default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sign_in_record"
        verbose_name = "签到记录"
        verbose_name_plural = "签到记录"
        constraints = [models.UniqueConstraint(fields=["user", "sign_date"], name="uniq_user_sign_date")]


class Plan(models.TextChoices):
    MONTHLY = "monthly", "包月"
    YEARLY = "yearly", "包年"


class Subscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "生效中"
        EXPIRED = "expired", "已过期"
        CANCELLED = "cancelled", "已取消"

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="subscriptions")
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.MONTHLY)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    minutes_used = models.IntegerField("本月已用分钟", default=0)
    minutes_limit = models.IntegerField("月度分钟上限", default=settings.MONTHLY_MINUTES_LIMIT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "subscription"
        verbose_name = "订阅"
        verbose_name_plural = "订阅"

    @property
    def is_active(self) -> bool:
        from django.utils import timezone

        return self.status == self.Status.ACTIVE and self.expires_at > timezone.now()

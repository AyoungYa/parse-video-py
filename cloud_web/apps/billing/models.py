"""积分账本与订阅。

设计原则（见 docs/product/架构设计.md §九）：
  · 积分只用「只增不改的账本行」表达，每行带 balance_after 快照；
  · 绝不对余额做 UPDATE 覆盖，避免并发扣成负数；
  · 预扣发生在「确认上传」，失败全额退还。

数据库规范：无 DB 外键、字段/表带中文 comment、索引 idx_/uniq_ 显式命名、
每张表一个对外唯一号（见 apps/bizno.py）。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.bizno import (
    NO_MAX_LENGTH,
    gen_ledger_no,
    gen_sign_in_no,
    gen_subscription_no,
)


class PointReason(models.TextChoices):
    INITIAL = "initial", "新人赠送"
    SIGN_IN = "sign_in", "每日签到"
    STREAK = "streak", "连签奖励"
    CONSUME = "consume", "任务消耗"
    REFUND = "refund", "失败退还"
    SUBSCRIBE = "subscribe", "订阅赠送"


class PointLedger(models.Model):
    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="point_entries",
        db_constraint=False, db_index=False,
        db_comment="所属用户 ID（逻辑外键，不建 DB 约束）",
    )
    ledger_no = models.CharField(
        "流水号", max_length=NO_MAX_LENGTH, default=gen_ledger_no, editable=False,
        db_comment="对外流水号 L+日期+随机；对账与用户端查询用此值",
    )
    delta = models.IntegerField(
        "变动值（正加负减）",
        db_comment="积分变动值，正数为入账、负数为扣减；只增不改",
    )
    balance_after = models.IntegerField(
        "变动后余额",
        db_comment="本行写入后的余额快照，禁止用 UPDATE 覆盖余额",
    )
    reason = models.CharField(
        max_length=20, choices=PointReason.choices,
        db_comment="变动原因：initial/sign_in/streak/consume/refund/subscribe",
    )
    ref_id = models.CharField(
        "关联对象 ID（任务/订单）", max_length=64, blank=True, default="",
        db_comment="关联的任务号或订单号，可空",
    )
    note = models.CharField(
        max_length=200, blank=True, default="",
        db_comment="备注，可空",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_comment="入账时间",
    )

    class Meta:
        db_table = "point_ledger"
        db_table_comment = "积分流水：只增不改的账本行，每行带余额快照"
        verbose_name = "积分流水"
        verbose_name_plural = "积分流水"
        constraints = [
            models.UniqueConstraint(fields=["ledger_no"], name="uniq_point_ledger_no"),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="idx_point_ledger_user_created"),
            models.Index(fields=["created_at"], name="idx_point_ledger_created_at"),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.user_id} {self.delta:+d} → {self.balance_after}"


class SignInRecord(models.Model):
    sign_in_no = models.CharField(
        "签到号", max_length=NO_MAX_LENGTH, default=gen_sign_in_no, editable=False,
        db_comment="对外签到号 S+日期+随机",
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="sign_ins",
        db_constraint=False, db_index=False,
        db_comment="所属用户 ID（逻辑外键，不建 DB 约束）",
    )
    sign_date = models.DateField(
        "签到日期",
        db_comment="签到自然日；与 user 组成唯一约束，一天只能签一次",
    )
    points = models.IntegerField(
        "获得积分", default=0,
        db_comment="本次实得积分（含连签奖励）",
    )
    streak = models.IntegerField(
        "连续天数", default=1,
        db_comment="截至本次的连续签到天数",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_comment="签到时间",
    )

    class Meta:
        db_table = "sign_in_record"
        db_table_comment = "签到记录：每用户每天至多一条，月度积分封顶 10 分"
        verbose_name = "签到记录"
        verbose_name_plural = "签到记录"
        constraints = [
            models.UniqueConstraint(fields=["sign_in_no"], name="uniq_sign_in_record_no"),
            models.UniqueConstraint(fields=["user", "sign_date"], name="uniq_sign_in_record_user_date"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.sign_date}"


class Plan(models.TextChoices):
    MONTHLY = "monthly", "包月"
    YEARLY = "yearly", "包年"


class Subscription(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "生效中"
        EXPIRED = "expired", "已过期"
        CANCELLED = "cancelled", "已取消"

    subscription_no = models.CharField(
        "订阅号", max_length=NO_MAX_LENGTH, default=gen_subscription_no, editable=False,
        db_comment="对外订阅号 B+日期+随机；支付订单关联用此值",
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="subscriptions",
        db_constraint=False, db_index=False,
        db_comment="所属用户 ID（逻辑外键，不建 DB 约束）",
    )
    plan = models.CharField(
        max_length=20, choices=Plan.choices, default=Plan.MONTHLY,
        db_comment="套餐：monthly / yearly",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE,
        db_comment="订阅状态：active / expired / cancelled",
    )
    started_at = models.DateTimeField(
        db_comment="生效时间",
    )
    expires_at = models.DateTimeField(
        db_comment="到期时间；到期后额度回落到积分制",
    )
    minutes_used = models.IntegerField(
        "本月已用分钟", default=0,
        db_comment="当前计费周期已用分钟数",
    )
    minutes_limit = models.IntegerField(
        "月度分钟上限", default=settings.MONTHLY_MINUTES_LIMIT,
        db_comment="月度分钟硬闸，默认取 MONTHLY_MINUTES_LIMIT",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_comment="订阅创建时间",
    )

    class Meta:
        db_table = "subscription"
        db_table_comment = "订阅：包月/包年状态机与月度分钟额度"
        verbose_name = "订阅"
        verbose_name_plural = "订阅"
        constraints = [
            models.UniqueConstraint(fields=["subscription_no"], name="uniq_subscription_no"),
        ]
        indexes = [
            models.Index(fields=["user", "-expires_at"], name="idx_subscription_user_expires"),
            models.Index(fields=["expires_at"], name="idx_subscription_expires_at"),
        ]

    @property
    def is_active(self) -> bool:
        from django.utils import timezone

        return self.status == self.Status.ACTIVE and self.expires_at > timezone.now()

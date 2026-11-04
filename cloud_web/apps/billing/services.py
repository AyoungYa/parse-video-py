"""积分核心服务。

并发安全的关键：所有写操作都在 transaction 里先 `select_for_update` 锁住该用户
**最后一条流水**，再基于它的 balance_after 计算新余额。这样即使两个请求同时到，
第二个也会等第一个提交后重新读取余额，不会出现双花。
"""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import PointLedger, PointReason, SignInRecord


class InsufficientPoints(Exception):
    """积分不足。"""


def get_balance(user) -> int:
    """当前余额。没有流水时为 0。"""
    last = PointLedger.objects.filter(user=user).order_by("-id").only("balance_after").first()
    return last.balance_after if last else 0


def _append_locked(
    user, delta: int, reason: str, ref_id: str = "", note: str = ""
) -> PointLedger:
    """在已开启的事务中追加一条流水。调用方必须持有用户锁。"""
    last = (
        PointLedger.objects.select_for_update()
        .filter(user=user)
        .order_by("-id")
        .only("balance_after", "id")
        .first()
    )
    balance = (last.balance_after if last else 0) + delta
    if balance < 0:
        raise InsufficientPoints(f"积分不足：需 {-delta}，现有 {balance + -delta}")
    return PointLedger.objects.create(
        user=user,
        delta=delta,
        balance_after=balance,
        reason=reason,
        ref_id=ref_id,
        note=note,
    )


def add_points(user, delta: int, reason: str, ref_id: str = "", note: str = "") -> PointLedger:
    with transaction.atomic():
        return _append_locked(user, delta, reason, ref_id, note)


def consume_points(user, amount: int, task_id: str, note: str = "") -> PointLedger:
    """预扣积分。不足则抛 InsufficientPoints。"""
    if amount <= 0:
        raise ValueError("amount 必须为正")
    with transaction.atomic():
        return _append_locked(user, -amount, PointReason.CONSUME, ref_id=task_id, note=note)


def refund_points(user, amount: int, task_id: str, note: str = "处理失败退还") -> PointLedger:
    """退还积分（幂等由调用方按 task 状态保证）。"""
    if amount <= 0:
        raise ValueError("amount 必须为正")
    with transaction.atomic():
        return _append_locked(user, amount, PointReason.REFUND, ref_id=task_id, note=note)


def ensure_initial_grant(user) -> None:
    """新人一次性赠送试用额度。重复调用安全。"""
    if PointLedger.objects.filter(user=user, reason=PointReason.INITIAL).exists():
        return
    add_points(
        user,
        settings.FREE_TRIAL_COUNT * settings.POINTS_PER_SHORT_VIDEO,
        PointReason.INITIAL,
        note=f"新人赠送 {settings.FREE_TRIAL_COUNT} 条 {settings.FREE_TRIAL_MAX_SECONDS}s 试用",
    )


def points_for_duration(duration_ms: int) -> int:
    """按时长换算积分消耗。规则见 docs/product/商业化方案.md §三.2。"""
    seconds = duration_ms / 1000
    if seconds <= 30:
        return settings.POINTS_PER_SHORT_VIDEO
    if seconds <= 300:
        return settings.POINTS_PER_MEDIUM_VIDEO
    # > 5 分钟：按时长线性折算成「中档」倍数，向上取整
    return settings.POINTS_PER_MEDIUM_VIDEO * (int(seconds // 300) + 1)


def monthly_earned(user, today: date | None = None) -> int:
    """本月已获得的签到类积分（用于月封顶判断）。"""
    today = today or timezone.localdate()
    start = today.replace(day=1)
    rows = PointLedger.objects.filter(
        user=user,
        reason__in=[PointReason.SIGN_IN, PointReason.STREAK],
        created_at__date__gte=start,
    )
    return sum(r.delta for r in rows)


def sign_in(user, today: date | None = None) -> dict:
    """每日签到。返回 {points, streak, balance, capped}。"""
    today = today or timezone.localdate()
    if SignInRecord.objects.filter(user=user, sign_date=today).exists():
        return {"already": True, "points": 0, "balance": get_balance(user)}

    yesterday = today - timedelta(days=1)
    prev = SignInRecord.objects.filter(user=user, sign_date=yesterday).first()
    streak = (prev.streak + 1) if prev else 1

    points = settings.SIGN_IN_POINTS
    if streak % 7 == 0:
        points += settings.SIGN_IN_STREAK_BONUS

    # 月度封顶：本月签到类积分已达上限就不再发
    capped = monthly_earned(user, today) >= settings.MONTHLY_POINT_CAP
    granted = 0 if capped else points

    with transaction.atomic():
        SignInRecord.objects.create(user=user, sign_date=today, points=granted, streak=streak)
        if granted:
            _append_locked(
                user, granted, PointReason.STREAK if granted > settings.SIGN_IN_POINTS else PointReason.SIGN_IN
            )
    return {"already": False, "points": granted, "streak": streak, "balance": get_balance(user), "capped": capped}


def active_subscription(user):
    now = timezone.now()
    return (
        Subscription.objects.filter(user=user, status=Subscription.Status.ACTIVE, expires_at__gt=now)
        .order_by("-expires_at")
        .first()
    )


def sign_in_status(user, today: date | None = None) -> dict:
    """账户页展示用的签到状态（不产生任何写入）。

    连签断了就归零 —— 昨天没签，今天之前的 streak 不再算数。
    """
    today = today or timezone.localdate()
    latest = SignInRecord.objects.filter(user=user).order_by("-sign_date").first()
    signed_today = bool(latest and latest.sign_date == today)
    streak = 0
    if latest and (today - latest.sign_date).days <= 1:
        streak = latest.streak
    earned = monthly_earned(user, today)
    return {
        "signed_today": signed_today,
        "streak": streak,
        "month_earned": earned,
        "month_cap": settings.MONTHLY_POINT_CAP,
    }


# 延迟导入，避免 models 与 services 循环
from .models import Subscription  # noqa: E402

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

from .models import PointLedger, PointLot, PointReason, PointSpend, SignInRecord

GIFT_LIFETIME = timedelta(days=10)
EXPIRING_REASONS = {PointReason.INITIAL, PointReason.SIGN_IN, PointReason.STREAK, PointReason.SHARE}


class InsufficientPoints(Exception):
    """积分不足。"""


def get_balance(user) -> int:
    """同步过期积分后返回余额。"""
    with transaction.atomic():
        _lock_user(user)
        _sync_lots_locked(user)
        _expire_locked(user)
        return _ledger_balance(user)


def _lock_user(user) -> None:
    from apps.accounts.models import User
    User.objects.select_for_update().get(pk=user.pk)


def _ledger_balance(user) -> int:
    last = PointLedger.objects.filter(user=user).order_by("-id").only("balance_after").first()
    return last.balance_after if last else 0


def _sync_lots_locked(user) -> None:
    """首次使用批次系统时，将旧账本余额原样迁入永久批次。"""
    if not PointLot.objects.filter(user=user).exists():
        old_balance = _ledger_balance(user)
        if old_balance:
            PointLot.objects.create(user=user, source="legacy", remaining=old_balance)


def _expire_locked(user) -> None:
    expired = list(PointLot.objects.filter(user=user, remaining__gt=0, expires_at__lte=timezone.now()))
    amount = sum(lot.remaining for lot in expired)
    if not amount:
        return
    for lot in expired:
        lot.remaining = 0
        lot.save(update_fields=["remaining"])
    _append_locked(user, -amount, PointReason.EXPIRE, note="赠送积分到期")


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
    if delta <= 0:
        raise ValueError("发放积分必须为正")
    with transaction.atomic():
        _lock_user(user)
        _sync_lots_locked(user)
        _expire_locked(user)
        ledger = _append_locked(user, delta, reason, ref_id, note)
        PointLot.objects.create(user=user, source=reason, remaining=delta,
                                expires_at=timezone.now() + GIFT_LIFETIME if reason in EXPIRING_REASONS else None)
        return ledger


def consume_points(user, amount: int, task_id: str, note: str = "") -> PointLedger:
    """预扣积分。不足则抛 InsufficientPoints。"""
    if amount <= 0:
        raise ValueError("amount 必须为正")
    with transaction.atomic():
        _lock_user(user)
        _sync_lots_locked(user)
        _expire_locked(user)
        if _ledger_balance(user) < amount:
            raise InsufficientPoints("积分不足")
        remaining = amount
        lots = PointLot.objects.filter(user=user, remaining__gt=0).order_by("expires_at", "created_at", "id")
        # MySQL NULL 排在前面；永久批次必须最后使用。
        lots = sorted(lots, key=lambda lot: (lot.expires_at is None, lot.expires_at or timezone.now(), lot.pk))
        for lot in lots:
            take = min(lot.remaining, remaining)
            if not take:
                continue
            lot.remaining -= take
            lot.save(update_fields=["remaining"])
            PointSpend.objects.create(user=user, task_id=task_id, lot=lot, amount=take)
            remaining -= take
            if remaining == 0:
                break
        if remaining:
            raise InsufficientPoints("积分批次不足")
        return _append_locked(user, -amount, PointReason.CONSUME, ref_id=task_id, note=note)


def refund_points(user, amount: int, task_id: str, note: str = "处理失败退还") -> PointLedger:
    """退还积分（幂等由调用方按 task 状态保证）。"""
    if amount <= 0:
        raise ValueError("amount 必须为正")
    with transaction.atomic():
        _lock_user(user)
        _sync_lots_locked(user)
        _expire_locked(user)
        all_spends = list(PointSpend.objects.select_related("lot").filter(user=user, task_id=task_id))
        spends = [spend for spend in all_spends if not spend.refunded]
        if all_spends and not spends:
            raise ValueError("该任务积分已退还")
        if spends and sum(spend.amount for spend in spends) != amount:
            raise ValueError("退款积分与原扣分不符")
        if spends:
            now = timezone.now()
            for spend in spends:
                expires_at = (max(spend.lot.expires_at, now + GIFT_LIFETIME)
                              if spend.lot.expires_at else None)
                PointLot.objects.create(user=user, source=PointReason.REFUND,
                                        remaining=spend.amount, expires_at=expires_at)
                spend.refunded = True
                spend.save(update_fields=["refunded"])
        else:
            PointLot.objects.create(user=user, source=PointReason.REFUND, remaining=amount)
        return _append_locked(user, amount, PointReason.REFUND, ref_id=task_id, note=note)


def ensure_initial_grant(user) -> None:
    """新人一次性赠送试用额度。重复调用安全。"""
    if PointLedger.objects.filter(user=user, reason=PointReason.INITIAL).exists():
        return
    add_points(
        user,
        settings.INITIAL_GIFT_POINTS,
        PointReason.INITIAL,
        note=f"新用户赠送 {settings.INITIAL_GIFT_POINTS} 个去水印积分",
    )


def points_for_duration(duration_ms: int) -> int:
    """每满 10 秒 1 分；每条至少扣 1 分。"""
    return max(1, duration_ms // 10_000)


def monthly_earned(user, today: date | None = None) -> int:
    """本自然月累计获得的签到类积分，仅用于页面展示。"""
    today = today or timezone.localdate()
    start = today.replace(day=1)
    rows = PointLedger.objects.filter(
        user=user,
        reason__in=[PointReason.SIGN_IN, PointReason.STREAK],
        created_at__date__gte=start,
    )
    return sum(r.delta for r in rows)


def sign_in(user, today: date | None = None) -> dict:
    """每日签到；每连续 7 天奖励 5 分，不设月度封顶。"""
    today = today or timezone.localdate()
    with transaction.atomic():
        _lock_user(user)
        if SignInRecord.objects.filter(user=user, sign_date=today).exists():
            return {"already": True, "points": 0, "balance": get_balance(user)}
        yesterday = today - timedelta(days=1)
        prev = SignInRecord.objects.filter(user=user, sign_date=yesterday).first()
        streak = (prev.streak + 1) if prev else 1
        points = settings.SIGN_IN_POINTS
        if streak % 7 == 0:
            points += settings.SIGN_IN_STREAK_BONUS
        SignInRecord.objects.create(user=user, sign_date=today, points=points, streak=streak)
        add_points(user, points, PointReason.STREAK if points > settings.SIGN_IN_POINTS else PointReason.SIGN_IN)
    return {"already": False, "points": points, "streak": streak, "balance": get_balance(user)}


def active_subscription(user):
    now = timezone.now()
    return (
        Subscription.objects.filter(user=user, status=Subscription.Status.ACTIVE,
                                    started_at__lte=now, expires_at__gt=now)
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
    }


# 延迟导入，避免 models 与 services 循环
from .models import Subscription  # noqa: E402

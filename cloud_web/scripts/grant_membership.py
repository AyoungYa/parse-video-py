"""手动开通会员（订阅）——支付通道未接入前的测试/运营手段。

用法（在 cloud_web/ 下）：
    uv run python scripts/grant_membership.py 21 --days 30            # 按用户 ID
    uv run python scripts/grant_membership.py 18373159240 --days 30   # 按手机号
    uv run python scripts/grant_membership.py 用户9240 --days 30       # 按昵称
    uv run python scripts/grant_membership.py 21 --days 30 --dry-run  # 只看不写

规则：
  · 已有生效订阅 → 从原到期时间起顺延（续费语义），不覆盖旧记录；
  · 无生效订阅   → 新建一条 monthly 订阅，从当前时间起算；
  · minutes_used 重置为 0、minutes_limit 取 settings.MONTHLY_MINUTES_LIMIT。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

import django

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.conf import settings  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.billing.models import Plan, Subscription  # noqa: E402


def resolve_user(key: str) -> User:
    """按 ID / 手机号 / 昵称 / 用户号定位，多个命中时报错要求更精确。"""
    qs = User.objects.none()
    if key.isdigit():
        qs = User.objects.filter(id=int(key))
        if not qs.exists():
            qs = User.objects.filter(phone=key)
    if not qs.exists():
        qs = User.objects.filter(phone=key) | User.objects.filter(nickname=key) | User.objects.filter(user_no=key)
    users = list(qs.distinct())
    if not users:
        raise SystemExit(f"找不到用户：{key}")
    if len(users) > 1:
        detail = "、".join(f"id={u.id}({u.phone}/{u.nickname})" for u in users)
        raise SystemExit(f"「{key}」命中多个用户，请改用用户 ID：{detail}")
    return users[0]


def grant(user: User, days: int, dry_run: bool) -> None:
    now = timezone.now()
    current = (
        Subscription.objects.filter(
            user=user, status=Subscription.Status.ACTIVE, expires_at__gt=now
        )
        .order_by("-expires_at")
        .first()
    )
    base = current.expires_at if current else now
    expires = base + timedelta(days=days)

    print(f"用户：id={user.id} phone={user.phone} nickname={user.nickname!r}")
    if current:
        print(f"现有订阅：{current.subscription_no} 到 {timezone.localtime(current.expires_at):%Y-%m-%d %H:%M}（顺延）")
    else:
        print("现有订阅：无（新建）")
    print(f"开通 {days} 天 → 到期 {timezone.localtime(expires):%Y-%m-%d %H:%M}")

    if dry_run:
        print("dry-run：未写入")
        return

    with transaction.atomic():
        if current:
            current.expires_at = expires
            current.minutes_used = 0
            current.minutes_limit = settings.MONTHLY_MINUTES_LIMIT
            current.save(update_fields=["expires_at", "minutes_used", "minutes_limit"])
            sub = current
        else:
            sub = Subscription.objects.create(
                user=user,
                plan=Plan.MONTHLY,
                status=Subscription.Status.ACTIVE,
                started_at=now,
                expires_at=expires,
                minutes_used=0,
                minutes_limit=settings.MONTHLY_MINUTES_LIMIT,
            )
    print(f"已完成：{sub.subscription_no} active，"
          f"{timezone.localtime(sub.started_at):%Y-%m-%d %H:%M} → "
          f"{timezone.localtime(sub.expires_at):%Y-%m-%d %H:%M}，"
          f"额度 {sub.minutes_limit} 分钟/月")


def main() -> None:
    ap = argparse.ArgumentParser(description="手动开通会员订阅")
    ap.add_argument("user", help="用户 ID / 手机号 / 昵称")
    ap.add_argument("--days", type=int, default=30, help="开通天数，默认 30")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果不写库")
    args = ap.parse_args()
    if args.days <= 0:
        raise SystemExit("--days 必须为正")
    grant(resolve_user(args.user), args.days, args.dry_run)


if __name__ == "__main__":
    main()

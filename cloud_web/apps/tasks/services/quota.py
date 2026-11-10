"""额度计算：单条时长上限、本月已用分钟、积分余额。

放在 services 里而不是视图里，是因为 accounts.me 和 tasks.sign_upload 都要用，
视图互相 import 会形成环。
"""

from __future__ import annotations

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.billing.services import active_subscription, get_balance

# 订阅用户单条上限（秒）；免费用户走 settings.FREE_TRIAL_MAX_SECONDS
MEMBER_MAX_SECONDS = 30 * 60


def fmt_seconds(seconds: int) -> str:
    """把秒数写成人话，用于提示文案。

    ★ 别用 `seconds // 60`：免费额度是 30 **秒**，整除后成了「0 分钟」，
      用户看到「当前上限 0 分钟」会以为额度坏了（2026-11-09 冒烟发现）。
    """
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds % 60 == 0:
        return f"{seconds // 60} 分钟"
    return f"{seconds / 60:.1f} 分钟"


def month_minutes_used(user) -> float:
    """本月已消耗的处理时长（分钟）。只算真正要占算力的状态。"""
    from apps.tasks.models import IN_FLIGHT, Task, TaskStatus

    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (
        Task.objects.filter(
            user=user,
            status__in=[TaskStatus.SUCCEEDED, TaskStatus.PENDING, TaskStatus.AWAITING_CONFIRMATION,
                        TaskStatus.NEEDS_MANUAL, *IN_FLIGHT],
            created_at__gte=start,
        ).aggregate(ms=Sum("duration_ms"))["ms"]
        or 0
    )
    return total / 60000


def user_quota(user) -> dict:
    """前端预检卡需要的一份额度快照。

    `max_seconds` 是单条上限，前端据此在本地就拦住超长片子，
    不用等 500MB 传完才被服务端拒。
    """
    sub = active_subscription(user)
    if sub is None:
        return {
            "is_member": False,
            "points": get_balance(user),
            "max_seconds": settings.FREE_TRIAL_MAX_SECONDS,
            "minutes_used": 0,
            "minutes_limit": 0,
        }
    return {
        "is_member": True,
        "points": get_balance(user),
        "max_seconds": MEMBER_MAX_SECONDS,
        "minutes_used": round(month_minutes_used(user), 1),
        "minutes_limit": sub.minutes_limit,
    }

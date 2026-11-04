"""签到与我的积分。"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import PointLedger
from .services import active_subscription, get_balance, sign_in, sign_in_status


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def do_sign_in(request):
    return Response(sign_in(request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_points(request):
    user = request.user
    entries = PointLedger.objects.filter(user=user)[:50]
    sub = active_subscription(user)
    return Response(
        {
            "balance": get_balance(user),
            "check_in": sign_in_status(user),
            "subscription": (
                {
                    "plan": sub.plan,
                    "expires_at": sub.expires_at,
                    "minutes_used": sub.minutes_used,
                    "minutes_limit": sub.minutes_limit,
                }
                if sub
                else None
            ),
            "entries": [
                {
                    "delta": e.delta,
                    "balance_after": e.balance_after,
                    "reason": e.reason,
                    "note": e.note,
                    "created_at": e.created_at,
                }
                for e in entries
            ],
        }
    )

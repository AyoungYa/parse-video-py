"""签到与我的积分。"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import PaymentOrder, PointLedger
from .services import active_subscription, get_balance, sign_in, sign_in_status
from .wechat_pay import (
    PaymentError, create_pending_order, decrypt_notification, query_order,
    settle_paid_order, verify_wechat_signature,
)


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
            "membership_plan": ({"plan": "monthly", "price_fen": settings.WECHAT_PAY_MONTHLY_PRICE_FEN,
                                 "minutes_limit": settings.MONTHLY_MINUTES_LIMIT}
                                if settings.MEMBERSHIP_PURCHASE_ENABLED else None),
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


def _order_payload(order: PaymentOrder) -> dict:
    return {
        "order_no": order.order_no, "status": order.status, "plan": order.plan,
        "amount_fen": order.amount_fen, "expires_at": order.expires_at,
        "code_url": order.code_url if order.status == PaymentOrder.Status.PENDING else "",
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_membership_order(request):
    if not settings.MEMBERSHIP_PURCHASE_ENABLED:
        return Response({"message": "会员购买暂未开放"}, status=status.HTTP_404_NOT_FOUND)
    try:
        order = create_pending_order(request.user)
    except PaymentError as exc:
        return Response({"message": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response(_order_payload(order), status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def membership_order(request, order_no: str):
    if not settings.MEMBERSHIP_PURCHASE_ENABLED:
        return Response({"message": "会员购买暂未开放"}, status=status.HTTP_404_NOT_FOUND)
    order = PaymentOrder.objects.filter(order_no=order_no, user=request.user).first()
    if order is None:
        return Response({"message": "订单不存在"}, status=status.HTTP_404_NOT_FOUND)
    if order.status == PaymentOrder.Status.PENDING:
        now = timezone.now()
        if order.last_checked_at is None or now - order.last_checked_at >= timedelta(seconds=5):
            PaymentOrder.objects.filter(pk=order.pk).update(last_checked_at=now)
            try:
                result = query_order(order.order_no)
                if result.get("trade_state") == "SUCCESS":
                    settle_paid_order(result)
                elif result.get("trade_state") == "CLOSED":
                    PaymentOrder.objects.filter(pk=order.pk, status=PaymentOrder.Status.PENDING).update(
                        status=PaymentOrder.Status.CLOSED, code_url=""
                    )
            except PaymentError:
                # 查询失败不代表支付失败；微信异步回调仍能正常完成订单。
                pass
            order.refresh_from_db()
    return Response(_order_payload(order))


@api_view(["POST"])
@permission_classes([AllowAny])
def wechat_pay_notify(request):
    try:
        verify_wechat_signature(request.headers, request.body)
        result = decrypt_notification(request.body)
        settle_paid_order(result)
    except PaymentError:
        return Response({"code": "FAIL", "message": "通知校验失败"}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"code": "SUCCESS", "message": "成功"})

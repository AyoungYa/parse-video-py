"""登录：大陆手机号 + 短信验证码。首次登录自动建号。

没有密码、没有注册页——用户只填手机号和验证码，剩下的交给后端。
"""

from __future__ import annotations

import re

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .models import AuthToken, User
from . import sms

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "phone": user.phone,
        "nickname": user.nickname,
        "avatar": user.avatar,
        "is_staff": user.is_staff,
    }


class _SmsSendThrottle(ScopedRateThrottle):
    scope = "sms_send"


def _client_ip(request) -> str | None:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([_SmsSendThrottle])
def sms_send(request):
    """发送验证码。60s 重发间隔 + 每日上限，防止刷短信费。"""
    phone = (request.data.get("phone") or "").strip()
    if not PHONE_RE.match(phone):
        return Response({"code": "invalid_phone", "message": "请输入正确的 11 位手机号"}, status=400)
    try:
        result = sms.send_code(phone, ip=_client_ip(request))
    except sms.SmsError as e:
        return Response({"code": e.code, "message": e.message, **e.extra}, status=e.status)
    return Response(result)


@api_view(["POST"])
@permission_classes([AllowAny])
def sms_login(request):
    """验证码登录。新手机号自动创建账号并发放试用额度。"""
    phone = (request.data.get("phone") or "").strip()
    code = (request.data.get("code") or "").strip()
    if not PHONE_RE.match(phone):
        return Response({"code": "invalid_phone", "message": "请输入正确的 11 位手机号"}, status=400)
    if not re.fullmatch(r"\d{6}", code):
        return Response({"code": "invalid_code", "message": "请输入 6 位验证码"}, status=400)

    try:
        sms.verify_and_consume(phone, code)
        user, is_new = sms.login_or_register(phone)
    except sms.SmsError as e:
        return Response({"code": e.code, "message": e.message, **e.extra}, status=e.status)

    _, raw = AuthToken.issue(user)
    return Response(
        {
            "token": raw,
            "user": _serialize_user(user),
            "is_new": is_new,
        },
        status=status.HTTP_201_CREATED if is_new else status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    token = request.auth
    if token is not None:
        token.delete()
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    from apps.tasks.services.quota import user_quota

    user = request.user
    quota = user_quota(user)
    return Response({**_serialize_user(user), "points": quota["points"], "quota": quota})

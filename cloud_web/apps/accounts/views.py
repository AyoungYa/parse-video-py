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
from . import captcha as captcha_service
from . import sms

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "user_no": user.user_no,
        "phone": user.phone,
        "nickname": user.nickname,
        "avatar": user.avatar,
        "is_staff": user.is_staff,
    }


class _SmsSendThrottle(ScopedRateThrottle):
    scope = "sms_send"


class _CaptchaThrottle(ScopedRateThrottle):
    scope = "captcha"


def _client_ip(request) -> str | None:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _bad_phone():
    return Response({"code": "invalid_phone", "message": "请输入正确的 11 位手机号"}, status=400)


def _sms_error(e: sms.SmsError):
    return Response({"code": e.code, "message": e.message, **e.extra}, status=e.status)


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([_CaptchaThrottle])
def captcha(request):
    """签发一张图形验证码。图片是服务端渲染的 PNG（base64），答案不回传。"""
    item = captcha_service.new_challenge(ip=_client_ip(request))
    return Response(item)


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([_SmsSendThrottle])
def sms_send(request):
    """发送验证码。

    三道闸门，顺序不能乱：
      1. 手机号格式（本地校验，不过就不消耗图形验证码）；
      2. 图形验证码（挡脚本，一条错误答案就换新图）；
      3. 短信侧限流（60s 重发 + 手机/IP 每日上限，挡人心不足）。
    """
    phone = (request.data.get("phone") or "").strip()
    if not PHONE_RE.match(phone):
        return _bad_phone()
    try:
        captcha_service.verify_and_consume(
            request.data.get("captcha_id"), request.data.get("captcha_code")
        )
    except captcha_service.CaptchaError as e:
        return Response({"code": e.code, "message": e.message}, status=e.status)

    try:
        result = sms.send_code(phone, ip=_client_ip(request))
    except sms.SmsError as e:
        return _sms_error(e)
    return Response(result)


@api_view(["POST"])
@permission_classes([AllowAny])
def sms_login(request):
    """验证码登录。新手机号自动创建账号并发放试用额度。"""
    phone = (request.data.get("phone") or "").strip()
    code = (request.data.get("code") or "").strip()
    if not PHONE_RE.match(phone):
        return _bad_phone()
    if not re.fullmatch(r"\d{6}", code):
        return Response({"code": "invalid_code", "message": "请输入 6 位验证码"}, status=400)

    try:
        sms.verify_and_consume(phone, code)
        user, is_new = sms.login_or_register(phone)
    except sms.SmsError as e:
        return _sms_error(e)

    _, raw = AuthToken.issue(user)
    return Response(
        {
            "token": raw,
            "user": _serialize_user(user),
            "is_new": is_new,
        },
        status=status.HTTP_201_CREATED if is_new else status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def site_config(request):
    """站点公开配置（无需登录）。

    前端文案里的可调参数（素材保留天数等）从这里取，改后端配置不用重发前端。
    只暴露无敏感信息、适合公开展示的键。
    """
    from django.conf import settings

    return Response({"retention_days": settings.OSS_RETENTION_DAYS})


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
    return Response(
        {
            **_serialize_user(user),
            "points": quota["points"],
            "quota": quota,
        }
    )

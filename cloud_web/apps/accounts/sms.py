"""短信验证码服务：生成、限流、核销、发送。

provider（settings.SMS_PROVIDER）：
- ``console``（默认）：不真发，只打日志。配合 ``SMS_DEV_ECHO=1`` 把验证码回显在
  响应里，本地联调和内测用——上线前必须关掉。
- ``aliyun``：阿里云短信（需已备案签名 + 模板）。SDK 按需 import，
  未安装/未配置时抛 SmsError，绝不静默降级。

安全参数全部可从环境变量覆盖，见 config/settings/base.py 的 SMS 段。
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.billing.services import ensure_initial_grant

from .models import SmsCode, User

logger = logging.getLogger(__name__)


class SmsError(Exception):
    """带错误码的短信异常，视图层直接转成 4xx 响应。"""

    def __init__(self, code: str, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}


# ---- 参数（集中读取，测试时可 override_settings）----

def _p(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def send_code(phone: str, ip: str | None = None) -> dict:
    """给手机号发一条登录验证码。返回给前端的信息（可能含开发回显码）。"""
    now = timezone.now()
    resend_gap = _p("SMS_RESEND_INTERVAL_SECONDS", 60)
    daily_phone_cap = _p("SMS_DAILY_PHONE_CAP", 10)
    daily_ip_cap = _p("SMS_DAILY_IP_CAP", 30)
    ttl = _p("SMS_CODE_TTL_SECONDS", 300)

    # 限流 1：同手机号 60s 内只能发一次
    last = SmsCode.objects.filter(phone=phone).order_by("-created_at").first()
    if last:
        elapsed = (now - last.created_at).total_seconds()
        if elapsed < resend_gap:
            raise SmsError(
                "sms_too_frequent",
                f"发送太频繁，请 {int(resend_gap - elapsed) + 1} 秒后再试",
                status=429,
                extra={"retry_after": int(resend_gap - elapsed) + 1},
            )

    # 限流 2：单手机 / 单 IP 每日上限（防刷短信费）
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if SmsCode.objects.filter(phone=phone, created_at__gte=day_start).count() >= daily_phone_cap:
        raise SmsError("sms_daily_phone_cap", "该手机号今日发送次数已达上限", status=429)
    if ip and SmsCode.objects.filter(ip=ip, created_at__gte=day_start).count() >= daily_ip_cap:
        raise SmsError("sms_daily_ip_cap", "该网络今日发送次数已达上限", status=429)

    code = f"{secrets.randbelow(1000000):06d}"
    SmsCode.objects.create(
        phone=phone,
        code_hash=SmsCode.hash_code(code),
        purpose="login",
        ip=ip,
        expires_at=now + timedelta(seconds=ttl),
    )
    _deliver(phone, code)

    resp: dict = {"retry_after": resend_gap}
    if getattr(settings, "SMS_DEV_ECHO", False):
        # 开发回显：仅 SMS_PROVIDER=console 且显式开启时
        resp["dev_code"] = code
    return resp


def verify_and_consume(phone: str, code: str) -> None:
    """核销验证码。错误码语义清晰，方便前端提示。"""
    max_attempts = _p("SMS_MAX_ATTEMPTS", 5)
    record = (
        SmsCode.objects.filter(phone=phone, purpose="login", used_at__isnull=True)
        .order_by("-created_at")
        .first()
    )
    if record is None:
        raise SmsError("sms_code_not_found", "请先获取验证码", status=400)
    if timezone.now() > record.expires_at:
        raise SmsError("sms_code_expired", "验证码已过期，请重新获取", status=400)
    if record.attempts >= max_attempts:
        raise SmsError("sms_code_locked", "错误次数过多，请重新获取验证码", status=400)
    if SmsCode.hash_code(code) != record.code_hash:
        record.attempts += 1
        record.save(update_fields=["attempts"])
        raise SmsError("sms_code_wrong", "验证码错误", status=400)
    record.used_at = timezone.now()
    record.save(update_fields=["used_at"])


def login_or_register(phone: str) -> tuple[User, bool]:
    """验证码已核销的前提下：老用户直接登录，新用户自动建号（无密码）。"""
    with transaction.atomic():
        user, created = User.objects.get_or_create(
            phone=phone,
            defaults={
                "username": phone,
                "nickname": f"用户{phone[-4:]}",
            },
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
            ensure_initial_grant(user)
    return user, created


# ---- 发送通道 ----

def _deliver(phone: str, code: str) -> None:
    provider = getattr(settings, "SMS_PROVIDER", "console")
    if provider == "console":
        logger.warning("[SMS:console] 给 %s 发送验证码 %s（未真实发送）", phone, code)
        return
    if provider == "aliyun":
        _deliver_aliyun(phone, code)
        return
    raise SmsError("sms_provider_unknown", f"未知短信通道：{provider}", status=500)


def _deliver_aliyun(phone: str, code: str) -> None:
    """真发短信。阿里云的失败码要翻译成人话，否则前端只能显示「发送失败」。

    ★ 密钥/签名/模板类的配置错必须显式报错（500/502），**绝不静默降级成 console**
      —— 否则线上「看起来正常」但用户永远收不到码，排查成本极高。
    """
    sign = getattr(settings, "SMS_ALIYUN_SIGN_NAME", "")
    template = getattr(settings, "SMS_ALIYUN_TEMPLATE_CODE", "")
    ak = getattr(settings, "SMS_ALIYUN_ACCESS_KEY_ID", "")
    sk = getattr(settings, "SMS_ALIYUN_ACCESS_KEY_SECRET", "")
    if not sign or not template:
        raise SmsError("sms_provider_unconfigured", "阿里云短信未配置签名/模板", status=500)
    if not ak or not sk:
        raise SmsError("sms_provider_unconfigured", "阿里云短信未配置 AccessKey", status=500)

    try:
        from alibabacloud_dysmsapi20170525.client import Client
        from alibabacloud_dysmsapi20170525.models import SendSmsRequest
        from alibabacloud_tea_openapi.models import Config
    except ImportError as e:  # pragma: no cover - 环境问题
        raise SmsError("sms_provider_unavailable", "未安装阿里云短信 SDK", status=500) from e

    config = Config(
        access_key_id=ak,
        access_key_secret=sk,
        endpoint="dysmsapi.aliyuncs.com",
        connect_timeout=3000,  # 毫秒；不设的话网络抖动会把登录页卡住
        read_timeout=5000,
    )
    client = Client(config)

    try:
        resp = client.send_sms(
            SendSmsRequest(
                phone_numbers=phone,
                sign_name=sign,
                template_code=template,
                template_param=f'{{"code":"{code}"}}',
            )
        )
    except Exception as e:  # pragma: no cover - 网络/鉴权异常
        logger.error("[SMS:aliyun] 调用失败 %s: %s", phone, e)
        raise SmsError("sms_send_failed", "短信服务暂时不可用，请稍后重试", status=502) from e

    body = getattr(resp, "body", None)
    result_code = getattr(body, "code", "") or ""
    if result_code == "OK":
        logger.info(
            "[SMS:aliyun] 已向 %s 发送验证码（request_id=%s）",
            phone,
            getattr(body, "request_id", ""),
        )
        return

    message = getattr(body, "message", "")
    request_id = getattr(body, "request_id", "")
    logger.error(
        "[SMS:aliyun] 发送失败 %s: code=%s message=%s request_id=%s",
        phone, result_code, message, request_id,
    )
    code_name, hint, status = _ALIYUN_ERRORS.get(
        result_code, ("sms_send_failed", "短信发送失败，请稍后重试", 502)
    )
    raise SmsError(code_name, hint, status=status)


#: 阿里云短信错误码 → （我们对外暴露的错误码, 给用户看的提示, HTTP 状态）
#: 参考 https://help.aliyun.com/zh/sms/developer-reference/api-error-codes
#: ★ 对外只给「能怎么办」的提示；不把 isv.xxx 原文抛给前端（会被用来探测账号状态）
_ALIYUN_ERRORS: dict[str, tuple[str, str, int]] = {
    "isv.BUSINESS_LIMIT_CONTROL": ("sms_rate_limited", "该号码发送过于频繁，请稍后再试", 429),
    "isv.MOBILE_NUMBER_ILLEGAL": ("sms_phone_illegal", "该手机号无法接收短信", 400),
    "isv.MOBILE_COUNT_OVER_LIMIT": ("sms_phone_illegal", "该手机号接收次数过多", 400),
    "isv.AMOUNT_NOT_ENOUGH": ("sms_send_failed", "短信服务暂时不可用，请稍后重试", 502),
    "isv.OUT_OF_SERVICE": ("sms_send_failed", "短信服务暂时不可用，请稍后重试", 502),
    "isv.PRODUCT_UN_SUBSCRIPT": ("sms_send_failed", "短信服务暂时不可用，请稍后重试", 502),
    "isv.SMS_SIGNATURE_ILLEGAL": ("sms_send_failed", "短信发送失败，请稍后重试", 502),
    "isv.SMS_TEMPLATE_ILLEGAL": ("sms_send_failed", "短信发送失败，请稍后重试", 502),
    "isv.TEMPLATE_MISSING_PARAMETERS": ("sms_send_failed", "短信发送失败，请稍后重试", 502),
    "isv.INVALID_ACCESS_KEY_ID": ("sms_send_failed", "短信服务暂时不可用，请稍后重试", 502),
    "SignatureDoesNotMatch": ("sms_send_failed", "短信服务暂时不可用，请稍后重试", 502),
}

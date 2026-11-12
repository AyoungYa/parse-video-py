"""微信支付 API v3 Native 支付；所有金额和权益由服务端决定。"""

from __future__ import annotations

import base64
import calendar
import json
import logging
import secrets
import time
from datetime import timedelta
from pathlib import Path

import requests
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import PaymentOrder, Plan, Subscription

logger = logging.getLogger(__name__)
API_HOST = "https://api.mch.weixin.qq.com"


class PaymentError(Exception):
    pass


def _one_month_later(value):
    year = value.year + (value.month == 12)
    month = value.month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _configured() -> None:
    required = (
        "WECHAT_PAY_APP_ID", "WECHAT_PAY_MCH_ID", "WECHAT_PAY_MCH_SERIAL_NO",
        "WECHAT_PAY_PRIVATE_KEY_PATH", "WECHAT_PAY_PUBLIC_KEY_ID",
        "WECHAT_PAY_PUBLIC_KEY_PATH", "WECHAT_PAY_API_V3_KEY", "WECHAT_PAY_NOTIFY_URL",
    )
    if any(not getattr(settings, name, "") for name in required):
        raise PaymentError("微信支付尚未配置完成")
    if len(settings.WECHAT_PAY_API_V3_KEY.encode()) != 32:
        raise PaymentError("微信支付 API v3 密钥长度必须为 32 字节")


def _private_key():
    return serialization.load_pem_private_key(
        Path(settings.WECHAT_PAY_PRIVATE_KEY_PATH).read_bytes(), password=None
    )


def _public_key():
    return serialization.load_pem_public_key(Path(settings.WECHAT_PAY_PUBLIC_KEY_PATH).read_bytes())


def verify_wechat_signature(headers, body: bytes) -> None:
    """对原始字节验签，拒绝时间戳重放和未知公钥 ID。"""
    _configured()
    timestamp = headers.get("Wechatpay-Timestamp", "")
    nonce = headers.get("Wechatpay-Nonce", "")
    serial = headers.get("Wechatpay-Serial", "")
    signature = headers.get("Wechatpay-Signature", "")
    try:
        if not timestamp or abs(time.time() - int(timestamp)) > 300:
            raise PaymentError("微信支付签名时间戳无效")
        if not nonce or serial != settings.WECHAT_PAY_PUBLIC_KEY_ID or not signature:
            raise PaymentError("微信支付签名信息不完整")
        message = f"{timestamp}\n{nonce}\n".encode() + body + b"\n"
        _public_key().verify(base64.b64decode(signature, validate=True), message,
                             padding.PKCS1v15(), hashes.SHA256())
    except (ValueError, InvalidSignature, OSError, TypeError) as exc:
        raise PaymentError("微信支付签名验证失败") from exc


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    _configured()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n".encode()
    try:
        signature = base64.b64encode(_private_key().sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()
    except (OSError, ValueError) as exc:
        raise PaymentError("微信支付商户密钥不可用") from exc
    auth = (
        'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{settings.WECHAT_PAY_MCH_ID}",nonce_str="{nonce}",'
        f'signature="{signature}",timestamp="{timestamp}",'
        f'serial_no="{settings.WECHAT_PAY_MCH_SERIAL_NO}"'
    )
    try:
        response = requests.request(
            method, API_HOST + path,
            data=body.encode() if payload is not None else None,
            headers={"Authorization": auth, "Accept": "application/json", "Content-Type": "application/json"},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise PaymentError("微信支付暂时不可用，请稍后重试") from exc
    if response.status_code >= 400:
        logger.warning("微信支付请求失败 status=%s body=%s", response.status_code, response.text[:300])
        raise PaymentError("微信支付暂时不可用，请稍后重试")
    verify_wechat_signature(response.headers, response.content)
    try:
        return response.json()
    except ValueError as exc:
        raise PaymentError("微信支付响应无效") from exc


def create_native_order(order: PaymentOrder) -> str:
    payload = {
        "appid": settings.WECHAT_PAY_APP_ID,
        "mchid": settings.WECHAT_PAY_MCH_ID,
        "description": "无痕匠包月会员",
        "out_trade_no": order.order_no,
        "time_expire": order.expires_at.isoformat(timespec="seconds"),
        "notify_url": settings.WECHAT_PAY_NOTIFY_URL,
        "amount": {"total": order.amount_fen, "currency": "CNY"},
    }
    result = _request("POST", "/v3/pay/transactions/native", payload)
    code_url = result.get("code_url", "")
    if not code_url.startswith("weixin://"):
        raise PaymentError("微信支付未返回有效二维码")
    return code_url


def query_order(order_no: str) -> dict:
    return _request("GET", f"/v3/pay/transactions/out-trade-no/{order_no}?mchid={settings.WECHAT_PAY_MCH_ID}")


def decrypt_notification(body: bytes) -> dict:
    try:
        envelope = json.loads(body)
        if envelope.get("event_type") != "TRANSACTION.SUCCESS":
            raise PaymentError("不是支付成功通知")
        resource = envelope["resource"]
        if resource["algorithm"] != "AEAD_AES_256_GCM":
            raise PaymentError("不支持的通知加密算法")
        ciphertext = base64.b64decode(resource["ciphertext"], validate=True)
        plaintext = AESGCM(settings.WECHAT_PAY_API_V3_KEY.encode()).decrypt(
            resource["nonce"].encode(), ciphertext, resource.get("associated_data", "").encode()
        )
        return json.loads(plaintext)
    except (KeyError, ValueError, UnicodeError, InvalidTag, TypeError) as exc:
        raise PaymentError("支付通知解密失败") from exc


def settle_paid_order(data: dict) -> PaymentOrder:
    """核对商户、金额和交易号后幂等开通会员。"""
    order_no = data.get("out_trade_no", "")
    transaction_id = data.get("transaction_id", "")
    if (data.get("trade_state") != "SUCCESS" or not order_no or not transaction_id
            or data.get("mchid") != settings.WECHAT_PAY_MCH_ID
            or data.get("appid") != settings.WECHAT_PAY_APP_ID):
        raise PaymentError("支付结果信息不匹配")
    with transaction.atomic():
        order = PaymentOrder.objects.select_for_update().filter(order_no=order_no).first()
        if order is None:
            raise PaymentError("支付订单不存在")
        amount = data.get("amount") or {}
        if amount.get("total") != order.amount_fen or amount.get("currency") != "CNY":
            raise PaymentError("支付金额不匹配")
        if order.status == PaymentOrder.Status.PAID:
            if order.transaction_id != transaction_id:
                raise PaymentError("支付交易号不匹配")
            return order
        if order.status != PaymentOrder.Status.PENDING:
            raise PaymentError("支付订单已关闭")
        # 串行化同一用户并发支付，续期从已有订阅的最晚到期时间开始。
        from apps.accounts.models import User
        User.objects.select_for_update().get(pk=order.user_id)
        latest = Subscription.objects.filter(
            user_id=order.user_id, status=Subscription.Status.ACTIVE
        ).order_by("-expires_at").first()
        start = max(timezone.now(), latest.expires_at) if latest else timezone.now()
        subscription = Subscription.objects.create(
            user_id=order.user_id, plan=Plan.MONTHLY, status=Subscription.Status.ACTIVE,
            started_at=start, expires_at=_one_month_later(start),
        )
        order.subscription = subscription
        order.transaction_id = transaction_id
        order.status = PaymentOrder.Status.PAID
        order.paid_at = timezone.now()
        order.code_url = ""
        order.save(update_fields=["subscription", "transaction_id", "status", "paid_at", "code_url"])
        return order


def create_pending_order(user) -> PaymentOrder:
    _configured()
    now = timezone.now()
    existing = PaymentOrder.objects.filter(
        user=user, status=PaymentOrder.Status.PENDING, expires_at__gt=now,
    ).exclude(code_url="").order_by("-created_at").first()
    if existing:
        return existing
    order = PaymentOrder.objects.create(
        user=user, plan=Plan.MONTHLY, amount_fen=settings.WECHAT_PAY_MONTHLY_PRICE_FEN,
        expires_at=now + timedelta(minutes=15),
    )
    order.code_url = create_native_order(order)
    order.save(update_fields=["code_url"])
    return order

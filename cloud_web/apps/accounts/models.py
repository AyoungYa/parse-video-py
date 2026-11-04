"""用户与访问令牌。

为什么不用 DRF 自带的 TokenAuthentication：那张表不支持过期时间，
而我们的用户 token 需要 7 天过期（浏览器 localStorage 持有，丢失风险更高）。
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """手机号为主登录方式；微信 openid 预留。"""

    phone = models.CharField("手机号", max_length=20, unique=True, null=True, blank=True)
    wx_openid = models.CharField("微信 openid", max_length=64, unique=True, null=True, blank=True)
    nickname = models.CharField("昵称", max_length=64, blank=True, default="")
    avatar = models.URLField("头像", blank=True, default="")

    class Meta:
        db_table = "user"
        verbose_name = "用户"
        verbose_name_plural = "用户"

    def __str__(self) -> str:
        return self.nickname or self.phone or self.username


class SmsCode(models.Model):
    """短信验证码。

    只存哈希（同 AuthToken 风格）；5 分钟有效、单次核销、错 5 次作废。
    发送限流（60s 重发间隔 / 每手机每日上限）也在查这张表。
    """

    phone = models.CharField("手机号", max_length=20, db_index=True)
    code_hash = models.CharField("验证码哈希", max_length=64)
    purpose = models.CharField("用途", max_length=20, default="login")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField("已尝试次数", default=0)
    ip = models.GenericIPAddressField("请求 IP", null=True, blank=True)

    class Meta:
        db_table = "sms_code"
        verbose_name = "短信验证码"
        verbose_name_plural = "短信验证码"

    @staticmethod
    def hash_code(code: str) -> str:
        import hashlib

        return hashlib.sha256(code.encode()).hexdigest()


class AuthToken(models.Model):
    """Bearer token。只存哈希，明文只在签发那一刻返回一次。"""

    key_hash = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "auth_token"
        verbose_name = "访问令牌"

    @classmethod
    def issue(cls, user: User, ttl_days: int = 7) -> tuple["AuthToken", str]:
        """签发一个 token，返回 (实例, 明文)。明文不落库。"""
        raw = "utk_" + secrets.token_urlsafe(32)
        token = cls.objects.create(
            key_hash=cls.hash_key(raw),
            user=user,
            expires_at=timezone.now() + timedelta(days=ttl_days),
        )
        return token, raw

    @staticmethod
    def hash_key(raw: str) -> str:
        import hashlib

        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def is_valid(self) -> bool:
        return self.expires_at > timezone.now()

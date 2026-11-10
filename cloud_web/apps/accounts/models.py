"""用户与访问令牌。

为什么不用 DRF 自带的 TokenAuthentication：那张表不支持过期时间，
而我们的用户 token 需要 7 天过期（浏览器 localStorage 持有，丢失风险更高）。

数据库规范（全项目统一，见 cloud_web/deploy/init_mysql.sql）：
  · 不建数据库外键（db_constraint=False），级联由 Django ORM 在应用层模拟；
  · 每个字段写 db_comment、每张表写 db_table_comment，DDL 自带中文说明；
  · 普通索引 idx_ 开头、唯一索引 uniq_ 开头，全部显式命名；
  · 每张业务表一个对外唯一号 *_no，用户端只用它查询/读写。
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

from apps.bizno import (
    NO_MAX_LENGTH,
    gen_auth_token_no,
    gen_captcha_no,
    gen_sms_code_no,
    gen_user_no,
)


class User(AbstractUser):
    """手机号为主登录方式；微信 openid 预留。"""

    user_no = models.CharField(
        "用户号", max_length=NO_MAX_LENGTH, default=gen_user_no, editable=False,
        db_comment="对外用户号 U+日期+随机，API 查询用此值，不暴露自增 ID",
    )
    phone = models.CharField(
        "手机号", max_length=20, null=True, blank=True,
        db_comment="登录手机号，E.164 或国内 11 位；未绑定为 NULL",
    )
    wx_openid = models.CharField(
        "微信 openid", max_length=64, null=True, blank=True,
        db_comment="微信 openid（预留），未绑定为 NULL",
    )
    nickname = models.CharField(
        "昵称", max_length=64, blank=True, default="",
        db_comment="用户昵称，可空",
    )
    avatar = models.URLField(
        "头像", blank=True, default="",
        db_comment="头像 URL，可空",
    )

    class Meta:
        db_table = "user"
        db_table_comment = "用户账户：手机号登录，承载积分、任务、订阅的归属主体"
        verbose_name = "用户"
        verbose_name_plural = "用户"
        constraints = [
            models.UniqueConstraint(fields=["user_no"], name="uniq_user_no"),
            models.UniqueConstraint(fields=["phone"], name="uniq_user_phone"),
            models.UniqueConstraint(fields=["wx_openid"], name="uniq_user_wx_openid"),
        ]

    def __str__(self) -> str:
        return self.nickname or self.phone or self.username


class SmsCode(models.Model):
    """短信验证码。

    只存哈希（同 AuthToken 风格）；5 分钟有效、单次核销、错 5 次作废。
    发送限流（60s 重发间隔 / 每手机每日上限）也在查这张表。
    """

    sms_no = models.CharField(
        "验证码记录号", max_length=NO_MAX_LENGTH, default=gen_sms_code_no, editable=False,
        db_comment="对外记录号 C+日期+随机",
    )
    phone = models.CharField(
        "手机号", max_length=20,
        db_comment="接收验证码的手机号",
    )
    code_hash = models.CharField(
        "验证码哈希", max_length=64,
        db_comment="验证码 SHA-256，明文不落库",
    )
    purpose = models.CharField(
        "用途", max_length=20, default="login",
        db_comment="用途：login / reset 等",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_comment="签发时间",
    )
    expires_at = models.DateTimeField(
        db_comment="过期时间（签发后 5 分钟）",
    )
    used_at = models.DateTimeField(
        null=True, blank=True,
        db_comment="核销时间；非空表示已使用",
    )
    attempts = models.PositiveSmallIntegerField(
        "已尝试次数", default=0,
        db_comment="校验尝试次数，达 5 次作废",
    )
    ip = models.GenericIPAddressField(
        "请求 IP", null=True, blank=True,
        db_comment="请求来源 IP，用于限流与审计",
    )

    class Meta:
        db_table = "sms_code"
        db_table_comment = "短信验证码：只存哈希，5 分钟有效、单次核销、错 5 次作废"
        verbose_name = "短信验证码"
        verbose_name_plural = "短信验证码"
        constraints = [
            models.UniqueConstraint(fields=["sms_no"], name="uniq_sms_code_no"),
        ]
        indexes = [
            models.Index(fields=["phone", "-created_at"], name="idx_sms_code_phone_created"),
            models.Index(fields=["expires_at"], name="idx_sms_code_expires_at"),
        ]

    @staticmethod
    def hash_code(code: str) -> str:
        import hashlib

        return hashlib.sha256(code.encode()).hexdigest()


class CaptchaChallenge(models.Model):
    """图形验证码挑战。

    为什么落库而不是用 cache：Django 默认 locmem 缓存是**每进程一份**，
    线上 gunicorn 多 worker 时 A 进程签发的挑战 B 进程查不到 → 随机失败。
    表只有几百字节一行，签发时顺手清过期行，成本可以忽略。

    只存哈希；2 分钟有效、核销即作废、错 3 次作废。
    """

    captcha_no = models.CharField(
        "挑战号", max_length=NO_MAX_LENGTH, default=gen_captcha_no, editable=False,
        db_comment="对外挑战号 P+日期+随机，前端凭它回传答案",
    )
    code_hash = models.CharField(
        "答案哈希", max_length=64,
        db_comment="答案 SHA-256（带挑战号加盐），明文不落库",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_comment="签发时间",
    )
    expires_at = models.DateTimeField(
        db_comment="过期时间（默认签发后 2 分钟）",
    )
    used_at = models.DateTimeField(
        null=True, blank=True,
        db_comment="核销时间；非空表示已使用",
    )
    attempts = models.PositiveSmallIntegerField(
        "已尝试次数", default=0,
        db_comment="校验尝试次数，达 3 次作废",
    )
    ip = models.GenericIPAddressField(
        "请求 IP", null=True, blank=True,
        db_comment="请求来源 IP，用于限流与审计",
    )

    class Meta:
        # ★ 表名用短名 captcha：MySQL 索引名上限 30 字符，
        #   idx_captcha_challenge_expires_at 会超长（E034），别改回去
        db_table = "captcha"
        db_table_comment = "图形验证码挑战：只存哈希，2 分钟有效、单次核销、错 3 次作废"
        verbose_name = "图形验证码"
        verbose_name_plural = "图形验证码"
        constraints = [
            models.UniqueConstraint(fields=["captcha_no"], name="uniq_captcha_no"),
        ]
        indexes = [
            models.Index(fields=["expires_at"], name="idx_captcha_expires_at"),
        ]

    @staticmethod
    def hash_code(captcha_no: str, answer: str) -> str:
        """答案按「挑战号 + 大写答案」哈希：同一答案在不同挑战里哈希不同。"""
        import hashlib

        return hashlib.sha256(f"{captcha_no}:{answer.upper()}".encode()).hexdigest()


class AuthToken(models.Model):
    """Bearer token。只存哈希，明文只在签发那一刻返回一次。"""

    token_no = models.CharField(
        "令牌记录号", max_length=NO_MAX_LENGTH, default=gen_auth_token_no, editable=False,
        db_comment="对外记录号 K+日期+随机",
    )
    key_hash = models.CharField(
        max_length=64,
        db_comment="令牌 SHA-256；明文只在签发时返回一次",
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="tokens",
        db_constraint=False, db_index=False,
        db_comment="所属用户 ID（逻辑外键，不建 DB 约束）",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_comment="签发时间",
    )
    expires_at = models.DateTimeField(
        db_comment="过期时间（默认 7 天）",
    )
    last_used_at = models.DateTimeField(
        null=True, blank=True,
        db_comment="最近一次使用时间",
    )

    class Meta:
        db_table = "auth_token"
        db_table_comment = "访问令牌：只存哈希，默认 7 天过期"
        verbose_name = "访问令牌"
        constraints = [
            models.UniqueConstraint(fields=["token_no"], name="uniq_auth_token_no"),
            models.UniqueConstraint(fields=["key_hash"], name="uniq_auth_token_key_hash"),
        ]
        indexes = [
            models.Index(fields=["user"], name="idx_auth_token_user"),
            models.Index(fields=["expires_at"], name="idx_auth_token_expires_at"),
        ]

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

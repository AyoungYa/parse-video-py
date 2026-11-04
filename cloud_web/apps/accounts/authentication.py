"""Bearer token 鉴权。

前端只带 `Authorization: Bearer utk_xxx`，不依赖 Cookie，天然免疫 CSRF。
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import authentication, exceptions

from .models import AuthToken


class BearerTokenAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None  # 交给下一个认证器（返回 None 而不是抛错，允许匿名端点）
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Authorization 头格式应为 'Bearer <token>'")

        raw = header[1].decode()
        try:
            token = AuthToken.objects.select_related("user").get(key_hash=AuthToken.hash_key(raw))
        except AuthToken.DoesNotExist:
            raise exceptions.AuthenticationFailed("令牌无效")

        if not token.is_valid:
            token.delete()
            raise exceptions.AuthenticationFailed("令牌已过期，请重新登录")

        if token.last_used_at is None or (timezone.now() - token.last_used_at).total_seconds() > 300:
            AuthToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("账号已被禁用")

        return token.user, token

    def authenticate_header(self, request) -> str:
        return "Bearer"

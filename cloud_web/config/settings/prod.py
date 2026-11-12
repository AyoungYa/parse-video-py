"""生产配置：强制 HTTPS 相关安全项。"""

import os

from .base import *  # noqa: F403

DEBUG = False

# 备案域名填进 ALLOWED_HOSTS，例如 wuhenjiang.com,www.wuhenjiang.com
ALLOWED_HOSTS = [
    h.strip() for h in os.getenv("ALLOWED_HOSTS", "wuhenjiang.com").split(",") if h.strip()
]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "1") == "1"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# 静态文件由 Nginx 直接服务；Django 只负责 collectstatic 产出
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"

# 生产绝不回显验证码；也用不了 console 通道（否则用户永远收不到码）
SMS_DEV_ECHO = False
if SMS_PROVIDER == "console":  # noqa: F405
    import warnings

    warnings.warn(
        "SMS_PROVIDER=console：生产环境不会真实发送短信，用户将无法登录。"
        "请配置阿里云短信认证（SMS_PROVIDER=dypnsapi）。",
        RuntimeWarning,
        stacklevel=1,
    )

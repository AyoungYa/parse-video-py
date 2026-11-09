"""本地单测专用配置。

为什么单独一个 settings 模块：
  · config.settings.dev 会强制校验 MYSQL_PASSWORD 非空（为了线上强制配密码），
    而本地 3307 实例的 root 是免密的 → 直接用 dev 跑不起来。
  · 这里直接继承 base，绕开那个「必须配密码」的校验，
    并且**不把任何密码写进文件**（本地 root 的密码本来就是空串）。

用法（推荐走 scripts/run_tests_local.py，它会设好环境变量）：
    MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 MYSQL_NAME=wuhenjiang \
    MYSQL_USER=root MYSQL_PASSWORD= \
    DJANGO_SETTINGS_MODULE=config.settings.test_local \
        python manage.py test tests --noinput
"""

import os

from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

# 本地 root 免密：密码留空，不落盘任何凭据。
DATABASES["default"]["PASSWORD"] = os.getenv("MYSQL_PASSWORD", "")  # noqa: F405

# 测试库用 utf8mb4，跟线上一致（否则中文/emoji 用例会假失败）。
DATABASES["default"].setdefault(  # noqa: F405
    "TEST",
    {"CHARSET": "utf8mb4", "COLLATION": "utf8mb4_unicode_ci"},
)

# 本地不真发短信，验证码直接回显。
SMS_PROVIDER = "console"
SMS_DEV_ECHO = True

"""本地开发配置：DEBUG 打开，允许所有 CORS。

数据库与线上一致，走 .env 里的远程 MySQL —— 本地不提供 SQLite 回退，
避免「本地过、线上炸」。凭据填 cloud_web/.env（模板见 .env.example）。
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

# 本项目不提供 SQLite 回退，本地也必须连远程 MySQL。
# 缺配置时直接给中文提示 —— 否则只会看到一个没头没脑的 OperationalError(2003)。
_required_db_env = ("MYSQL_HOST", "MYSQL_NAME", "MYSQL_USER", "MYSQL_PASSWORD")
_missing = [k for k in _required_db_env if not os.getenv(k)]
if _missing:
    raise ImproperlyConfigured(
        "数据库配置缺失：" + "、".join(_missing) + "\n"
        "  请填 cloud_web/.env（模板见 .env.example），填完先自检：\n"
        "      uv run python scripts/check_db.py\n"
        "  本项目不使用 SQLite，本地开发同样连远程 MySQL。"
    )

# 本地默认不真发短信，把验证码直接回显给前端，方便联调。
# 生产（prod.py）不会走到这里；如需本地演练真发，设 SMS_PROVIDER=dypnsapi 并关掉 SMS_DEV_ECHO。
if os.getenv("SMS_PROVIDER", "console") == "console":
    SMS_DEV_ECHO = os.getenv("SMS_DEV_ECHO", "1") == "1"  # noqa: F405

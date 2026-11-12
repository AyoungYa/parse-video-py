"""公共配置。敏感项一律从环境变量读，见 .env.example。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 本地开发直接读 cloud_web/.env（里面放远程 MySQL 的地址与凭据），免得每次 export。
# override=False：已经存在的环境变量优先，所以 Docker 的 env_file / 平台注入不会被文件覆盖。
load_dotenv(BASE_DIR / ".env", override=False)

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = os.getenv("DEBUG", "0") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "apps.accounts",
    "apps.tasks",
    "apps.billing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---- 数据库：MySQL 8（远程实例，地址/凭据全部来自 .env）----
# 不提供 SQLite 回退：本地与线上必须用同一种数据库，否则 JSON 字段、事务隔离、
# 唯一约束行为都会出现「本地测得过、线上炸」的差异。
_db_options: dict[str, object] = {
    "charset": "utf8mb4",
    "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
    # 远程实例必须设超时，否则网络抖动会让请求线程一直挂着（长轮询下会迅速吃满 worker）。
    "connect_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
    "read_timeout": int(os.getenv("MYSQL_READ_TIMEOUT", "30")),
    "write_timeout": int(os.getenv("MYSQL_WRITE_TIMEOUT", "30")),
}

# 公网连 MySQL 一律建议开 TLS：MYSQL_SSL=1 开启加密；
# 再给 MYSQL_SSL_CA 就校验证书，不给则只加密不校验（自签证书场景）。
if os.getenv("MYSQL_SSL") == "1":
    _ssl_options: dict[str, str] = {}
    if os.getenv("MYSQL_SSL_CA"):
        _ssl_options["ca"] = os.environ["MYSQL_SSL_CA"]
    _db_options["ssl"] = _ssl_options

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.getenv("MYSQL_NAME", "wuhenjiang"),
        "USER": os.getenv("MYSQL_USER", "wuhenjiang"),
        "PASSWORD": os.getenv("MYSQL_PASSWORD", ""),
        "HOST": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "PORT": os.getenv("MYSQL_PORT", "3306"),
        "OPTIONS": _db_options,
        "CONN_MAX_AGE": int(os.getenv("MYSQL_CONN_MAX_AGE", "60")),
    }
}

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---- DRF ----
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.BearerTokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "upload_sign": os.getenv("THROTTLE_UPLOAD_SIGN", "30/hour"),
        "task_create": os.getenv("THROTTLE_TASK_CREATE", "60/hour"),
        "worker": os.getenv("THROTTLE_WORKER", "600/minute"),
        "sms_send": os.getenv("THROTTLE_SMS_SEND", "12/hour"),
        "captcha": os.getenv("THROTTLE_CAPTCHA", "60/hour"),
    },
    "EXCEPTION_HANDLER": "apps.tasks.exceptions.api_exception_handler",
}

CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
CORS_ALLOW_CREDENTIALS = True

# ---- 阿里云 OSS（AK/SK 只存在于云端，绝不外发给算力机）----
OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "oss-cn-shenzhen.aliyuncs.com")
OSS_BUCKET = os.getenv("OSS_BUCKET", "")
OSS_ACCESS_KEY_ID = os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("OSS_ACCESS_KEY_SECRET", "")
OSS_UPLOAD_PREFIX = os.getenv("OSS_UPLOAD_PREFIX", "uploads/")
OSS_RESULT_PREFIX = os.getenv("OSS_RESULT_PREFIX", "results/")
OSS_SIGN_EXPIRE_SECONDS = int(os.getenv("OSS_SIGN_EXPIRE_SECONDS", "900"))
OSS_MAX_UPLOAD_BYTES = int(os.getenv("OSS_MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))
OSS_ALLOWED_CONTENT_TYPES = [
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
    "application/octet-stream",
]

# ---- 任务与租约（与 gpu_worker 必须一致）----
WORKER_CLAIM_TIMEOUT = int(os.getenv("WORKER_CLAIM_TIMEOUT", "25"))
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "90"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "15"))
MAX_ATTEMPT = int(os.getenv("MAX_ATTEMPT", "3"))
STALE_UPLOAD_MINUTES = int(os.getenv("STALE_UPLOAD_MINUTES", "30"))
# 素材（原片/成片/报告）保留天数：任务结束后超过该天数自动从 OSS 清理。
# 单位为天，最小 1 天（写 0 或负数按 1 天算，避免清理窗口落到「未来」误删在途任务）。
OSS_RETENTION_DAYS = max(1, int(os.getenv("OSS_RETENTION_DAYS", "3")))

# ---- 付费规则（成本模型校准值，见 docs/product/成本与选型.md）----
INITIAL_GIFT_POINTS = int(os.getenv("INITIAL_GIFT_POINTS", "100"))
FREE_TRIAL_MAX_SECONDS = int(os.getenv("FREE_TRIAL_MAX_SECONDS", "30"))
SIGN_IN_POINTS = int(os.getenv("SIGN_IN_POINTS", "1"))
SIGN_IN_STREAK_BONUS = int(os.getenv("SIGN_IN_STREAK_BONUS", "5"))
MONTHLY_MINUTES_LIMIT = int(os.getenv("MONTHLY_MINUTES_LIMIT", "100"))

# 微信支付 Native 扫码：密钥放在部署机器上，路径不提交到仓库。
WECHAT_PAY_APP_ID = os.getenv("WECHAT_PAY_APP_ID", "")
WECHAT_PAY_MCH_ID = os.getenv("WECHAT_PAY_MCH_ID", "")
WECHAT_PAY_MCH_SERIAL_NO = os.getenv("WECHAT_PAY_MCH_SERIAL_NO", "")
WECHAT_PAY_PRIVATE_KEY_PATH = os.getenv("WECHAT_PAY_PRIVATE_KEY_PATH", "")
WECHAT_PAY_PUBLIC_KEY_ID = os.getenv("WECHAT_PAY_PUBLIC_KEY_ID", "")
WECHAT_PAY_PUBLIC_KEY_PATH = os.getenv("WECHAT_PAY_PUBLIC_KEY_PATH", "")
WECHAT_PAY_API_V3_KEY = os.getenv("WECHAT_PAY_API_V3_KEY", "")
WECHAT_PAY_NOTIFY_URL = os.getenv("WECHAT_PAY_NOTIFY_URL", "")
WECHAT_PAY_MONTHLY_PRICE_FEN = int(os.getenv("WECHAT_PAY_MONTHLY_PRICE_FEN", "4900"))
MEMBERSHIP_PURCHASE_ENABLED = os.getenv("MEMBERSHIP_PURCHASE_ENABLED", "0") == "1"

# ---- 短信验证码登录（provider=console 时不真发，仅打日志）----
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "console")  # console | aliyun
SMS_DEV_ECHO = os.getenv("SMS_DEV_ECHO", "0") == "1"  # 仅开发：把验证码回显给前端
SMS_CODE_TTL_SECONDS = int(os.getenv("SMS_CODE_TTL_SECONDS", "300"))
SMS_RESEND_INTERVAL_SECONDS = int(os.getenv("SMS_RESEND_INTERVAL_SECONDS", "60"))
SMS_DAILY_PHONE_CAP = int(os.getenv("SMS_DAILY_PHONE_CAP", "10"))
SMS_DAILY_IP_CAP = int(os.getenv("SMS_DAILY_IP_CAP", "30"))
SMS_MAX_ATTEMPTS = int(os.getenv("SMS_MAX_ATTEMPTS", "5"))
SMS_ALIYUN_ACCESS_KEY_ID = os.getenv("SMS_ALIYUN_ACCESS_KEY_ID", "")
SMS_ALIYUN_ACCESS_KEY_SECRET = os.getenv("SMS_ALIYUN_ACCESS_KEY_SECRET", "")
SMS_ALIYUN_SIGN_NAME = os.getenv("SMS_ALIYUN_SIGN_NAME", "")
SMS_ALIYUN_TEMPLATE_CODE = os.getenv("SMS_ALIYUN_TEMPLATE_CODE", "")

# ---- 图形验证码（发短信前的人机校验，防脚本刷短信费）----
CAPTCHA_TTL_SECONDS = int(os.getenv("CAPTCHA_TTL_SECONDS", "120"))
CAPTCHA_MAX_ATTEMPTS = int(os.getenv("CAPTCHA_MAX_ATTEMPTS", "3"))
CAPTCHA_DEV_ECHO = os.getenv("CAPTCHA_DEV_ECHO", "0") == "1"  # 仅开发：回显答案给自动化脚本

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"}
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}

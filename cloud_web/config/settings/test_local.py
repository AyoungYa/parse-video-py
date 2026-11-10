"""本地单测专用配置。

为什么单独一个 settings 模块：
  · config.settings.dev 会强制校验 MYSQL_PASSWORD 非空（为了线上强制配密码），
    而本地 3307 实例的 root 是免密的 → 直接用 dev 跑不起来。
  · 这里直接继承 base，绕开那个「必须配密码」的校验。

★★ 为什么密码写死成空串、而不是 os.getenv("MYSQL_PASSWORD")：
  base.py 里 load_dotenv(BASE_DIR/".env") 会把 **远程调试库的密码**注入
  MYSQL_PASSWORD 环境变量。若这里跟着读，就会拿远程密码去连本地 3307 的 root
  → (1045) Access denied for user 'root'@'localhost'。
  本地 root 本来就是空密码，**必须显式覆盖掉继承来的远程凭据**。
  同时这也意味着：本地单测永远不会误连远程库。

用法（推荐走 scripts/run_tests_local.py，它会设好环境变量）：
    DJANGO_SETTINGS_MODULE=config.settings.test_local \
        python manage.py test tests --noinput
"""

from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

# 本地 3307 实例：root 免密（--initialize-insecure 初始化）。
# ★ 刻意忽略环境变量里的 MYSQL_PASSWORD —— 那个值是远程调试库的，用在这里必 1045。
DATABASES["default"]["PASSWORD"] = ""  # noqa: F405

# ★ HOST/USER 也必须**忽略** .env 注入的值（load_dotenv 已把远程调试库
#   120.25.1.236 / test_db_rw 塞进环境变量）。本地单测要连本机 3307，
#   所以这里不读 os.getenv，直接写死本地默认；需要连别处时在调用处改。
#   → 副作用是好的：本地单测永远不会误连远程库。
DATABASES["default"]["HOST"] = "127.0.0.1"  # noqa: F405
DATABASES["default"]["PORT"] = 3307  # noqa: F405
DATABASES["default"]["USER"] = "root"  # noqa: F405
DATABASES["default"]["NAME"] = "wuhenjiang"  # noqa: F405

# 测试库用 utf8mb4，跟线上一致（否则中文/emoji 用例会假失败）。
DATABASES["default"].setdefault(  # noqa: F405
    "TEST",
    {"CHARSET": "utf8mb4", "COLLATION": "utf8mb4_unicode_ci"},
)

# 本地不真发短信，验证码直接回显。
SMS_PROVIDER = "console"
SMS_DEV_ECHO = True

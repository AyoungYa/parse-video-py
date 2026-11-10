"""云端 MySQL 专用测试配置；只读取 TEST_MYSQL_*，不复用业务库账号。"""

import os

from .base import *  # noqa: F403


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"云端测试缺少配置：{name}")
    return value


test_name = required("TEST_MYSQL_NAME")
if not test_name.startswith("test_") or test_name == "test_":
    raise RuntimeError("TEST_MYSQL_NAME 必须是独立的 test_ 前缀数据库")

DATABASES["default"].update(  # noqa: F405
    NAME=test_name,
    HOST=required("TEST_MYSQL_HOST"),
    PORT=required("TEST_MYSQL_PORT"),
    USER=required("TEST_MYSQL_USER"),
    PASSWORD=required("TEST_MYSQL_PASSWORD"),
    CONN_MAX_AGE=0,
    TEST={
        "NAME": test_name,
        "CHARSET": "utf8mb4",
        "COLLATION": "utf8mb4_unicode_ci",
    },
)

# 测试连接的 TLS 与业务连接分开配置。
options = dict(DATABASES["default"].get("OPTIONS", {}))  # noqa: F405
options.pop("ssl", None)
if os.getenv("TEST_MYSQL_SSL", "1") == "1":
    options["ssl"] = {"ca": os.environ["TEST_MYSQL_SSL_CA"]} if os.getenv("TEST_MYSQL_SSL_CA") else {}
DATABASES["default"]["OPTIONS"] = options  # noqa: F405

SMS_PROVIDER = "console"
SMS_DEV_ECHO = True

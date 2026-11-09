#!/usr/bin/env python
"""本地 MySQL 跑 Django 单测（绕开远程账号缺 CREATE DATABASE 的限制）。

背景：
  调试库的账号只有库级授权，没有全局 CREATE DATABASE，
  `manage.py test` 需要建 test_xxx 临时库，会直接失败。
  本地 3307 实例的 root 是免密的，专门用来跑单测。

用法：
    uv run python scripts/run_tests_local.py            # 跑全部
    uv run python scripts/run_tests_local.py tests.test_manual_bbox

前置：本地 MySQL 已启动
    /usr/local/mysql/bin/mysqld --datadir=/tmp/pvp-mysql/data --port=3307 \
        --socket=/tmp/pvp-mysql/mysql.sock --pid-file=/tmp/pvp-mysql/mysqld.pid \
        --bind-address=127.0.0.1 --log-error=/tmp/pvp-mysql/error.log
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# 本地实例：root 免密、库名与线上一致。
# 注意用 os.environ 覆盖（.env 的 load_dotenv override=False 不会覆盖已有环境变量），
# 所以这里设置的优先级最高。
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test_local"
os.environ["MYSQL_HOST"] = "127.0.0.1"
os.environ["MYSQL_PORT"] = "3307"
os.environ["MYSQL_NAME"] = "wuhenjiang"
os.environ["MYSQL_USER"] = "root"
os.environ["MYSQL_PASSWORD"] = ""  # 本地 root 免密，刻意留空
os.environ["MYSQL_SSL"] = "0"
os.environ.setdefault("SMS_PROVIDER", "console")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.test.utils import get_runner  # noqa: E402


def main() -> int:
    # runner.run_tests() 内部会自己调 setup_test_environment()，这里不要重复调。
    runner = get_runner(settings)(verbosity=2, interactive=False)
    labels = sys.argv[1:] or ["tests"]
    print(
        f"[本地单测] 数据库={settings.DATABASES['default']['HOST']}:"
        f"{settings.DATABASES['default']['PORT']} 目标={labels}"
    )
    return runner.run_tests(labels)


if __name__ == "__main__":
    sys.exit(bool(main()))

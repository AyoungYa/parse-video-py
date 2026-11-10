#!/usr/bin/env python
"""本地 MySQL 跑 Django 单测（绕开远程账号缺 CREATE DATABASE 的限制）。

背景：
  调试库的账号只有库级授权，没有全局 CREATE DATABASE，
  `manage.py test` 需要建 test_xxx 临时库，会直接失败。
  本地 3307 实例的 root 是免密的，专门用来跑单测。

用法：
    uv run python scripts/run_tests_local.py            # 跑全部
    uv run python scripts/run_tests_local.py tests.test_manual_bbox

★ 前置：本地 MySQL 3307 必须已启动。
  数据目录在仓库内 `.local/mysql3307/data`（持久、已 gitignore）：
    W=.local/mysql3307
    /usr/local/mysql/bin/mysqld --datadir="$W/data" --port=3307 \\
        --socket="$W/mysql.sock" --pid-file="$W/mysqld.pid" \\
        --bind-address=127.0.0.1 --log-error="$W/error.log" \\
        --mysqlx=OFF --skip-log-bin
  首次初始化（--initialize-insecure，root 空密码）：
    /usr/local/mysql/bin/mysqld --initialize-insecure \\
        --datadir="$W/data" --basedir=/usr/local/mysql
  建库：
    /usr/local/mysql/bin/mysql -h 127.0.0.1 -P 3307 -u root \\
        -e "CREATE DATABASE IF NOT EXISTS wuhenjiang CHARACTER SET utf8mb4;"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# 只切 settings 模块；数据库连接参数由 config/settings/test_local.py 写死为本机 3307。
# ★ 不要在这里设 MYSQL_* 环境变量 —— base.py 的 load_dotenv 会把 .env 里
#   **远程调试库**的地址/账号/密码注入进来，settings 层必须主动忽略它们。
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test_local"
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

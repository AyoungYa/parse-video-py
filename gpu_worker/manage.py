#!/usr/bin/env python
"""无痕匠 · 算力机服务的 Django 管理入口。

本机只跑两件事：常驻的 worker 主循环（manage.py run_worker）和本地状态面板。
不对外提供任何服务，也不需要公网 IP。
"""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError("无法导入 Django，请先安装 requirements.txt") from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()

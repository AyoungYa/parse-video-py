#!/usr/bin/env python
"""无痕匠 · 云端网页服务的 Django 管理入口。"""

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.exceptions import ImproperlyConfigured
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "无法导入 Django，请在仓库根目录执行 uv sync --extra services"
        ) from exc

    try:
        execute_from_command_line(sys.argv)
    except ImproperlyConfigured as exc:
        # 配置缺失/写错时不要甩一屏 traceback，直接说清楚哪里不对
        sys.stderr.write(f"\n[配置有误，已中止]\n{exc}\n\n")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()

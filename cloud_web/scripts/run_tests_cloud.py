#!/usr/bin/env python
"""在独立云端 MySQL 测试库运行 Django 测试。"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.test_cloud"

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.test.utils import get_runner  # noqa: E402


if __name__ == "__main__":
    labels = sys.argv[1:] or ["tests"]
    db = settings.DATABASES["default"]
    print(f"[云端 MySQL 测试] {db['HOST']}:{db['PORT']} / {db['TEST']['NAME']}，目标={labels}")
    runner = get_runner(settings)(verbosity=2, interactive=False, keepdb=True)
    raise SystemExit(bool(runner.run_tests(labels)))

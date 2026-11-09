"""算力机服务配置。

设计取向：**配置全部来自环境变量，代码里不写死任何路径**——因为这台机器换一台就全变了。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("SECRET_KEY", "worker-local-only")
DEBUG = os.getenv("DEBUG", "0") == "1"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

# 本地 SQLite：只存任务镜像与心跳日志，丢了不影响云端状态
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(os.getenv("WORKER_DB_PATH", BASE_DIR / "worker.sqlite3")),
    }
}

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"

# ---------------- 与云端联动（必须和 cloud_web 的 settings 对齐）----------------
CLOUD_BASE_URL = os.getenv("CLOUD_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WORKER_ID = os.getenv("WORKER_ID", "pc-01")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")
WORKER_NAME = os.getenv("WORKER_NAME", WORKER_ID)

CLAIM_TIMEOUT = int(os.getenv("CLAIM_TIMEOUT", "30"))  # 略大于云端 25s
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "15"))
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "90"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "60"))

# ---------------- 本机算力 ----------------
TORCH_DEVICE = os.getenv("TORCH_DEVICE", "cuda")  # cuda / mps / cpu
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "1"))
UPLOAD_CONCURRENCY = int(os.getenv("UPLOAD_CONCURRENCY", "1"))  # 家宽上行，别调大

# 修复脚本目录（skill 的 scripts/ 或本仓库 src/skill/）
WM_SCRIPTS_DIR = Path(os.getenv("WM_SCRIPTS_DIR", BASE_DIR / "scripts"))
# 临时工作区：原片、mask、成片、报告都落在这里，任务结束后删除
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", BASE_DIR / "workspace"))
# 用哪个 python 解释器跑脚本（默认同当前解释器）
WM_PYTHON = os.getenv("WM_PYTHON", sys.executable)

# ---------------- 处理阶段（命令模板）----------------
# ★ 这里刻意做成「可配置的命令模板」而不是硬编码：
#   不同机器的脚本参数可能不同，改 .env 就能适配，不用改代码。
#   可用占位符：{python} {scripts} {work} {input} {mask} {out} {out_fixed}
#               {report} {bbox} {pad} {device}
#   判定成功只看**退出码 0**（踩坑手册铁律 2：日志里出现"完成"不代表成功）。
#   ★ build_mask 退出码 3 = 定位置信度不足 → 任务转人工，不重试。
#   ★ verify 后 pipeline 按 report.json 的 diagnosis 决定是否追加修复轮（见
#     WM_REPAIR_STAGES），决策树阈值固化在 verify.py（坑 44）。
WM_STAGES_TEMPLATE = [
    {
        "name": "build_mask",
        "enabled_when": "no_mask",  # 仅当云端没给 mask 时执行
        "cmd": [
            "{python}",
            "{scripts}/build_mask.py",
            "--input", "{input}",
            "--out", "{mask}",
            "--bbox", "{bbox}",
            "--device", "{device}",
        ],
    },
    {
        "name": "lama_apply",
        "required": True,
        "cmd": [
            "{python}",
            "{scripts}/lama_apply.py",
            "--input", "{input}",
            "--mask", "{mask}",
            "--out", "{out}",
            "--bbox", "{bbox}",
            "--pad", "{pad}",
            "--device", "{device}",
            "--keyframe-every", "1",
        ],
    },
    {
        "name": "verify",
        "required": False,
        "cmd": [
            "{python}",
            "{scripts}/verify.py",
            "--original", "{input}",
            "--processed", "{out}",
            "--mask", "{mask}",
            "--bbox", "{bbox}",
            "--out", "{report}",
        ],
    },
]

# 决策树修复轮（verify 的 diagnosis → 修复命令）。输入是第一轮成片 {out}，
# 输出到 {out_fixed}；修复后 pipeline 自动再跑一次 verify（针对 out_fixed）。
# 两轮都不绿 → 交付较优的那份并保留 diagnosis 供云端转人工。
WM_REPAIR_STAGES = {
    # A 类·过平滑（σ 比 < 0.8）：补周边真实纹理（固定 strength 1.3，实测最优）
    "needs_texture": [
        "{python}",
        "{scripts}/texture_fix.py",
        "--input", "{out}",
        "--mask", "{mask}",
        "--bbox", "{bbox}",
        "--strength", "1.3",
        "--audio-from", "{input}",
        "--out", "{out_fixed}",
    ],
    # B 类·伪噪点/条带断裂（Laplacian 能量比 > 1.5×）：低频行插值 + 高频保留
    "needs_rowinterp": [
        "{python}",
        "{scripts}/texture_fix.py",
        "--input", "{out}",
        "--mask", "{mask}",
        "--bbox", "{bbox}",
        "--lf-mode", "rowinterp",
        "--strength", "1.3",
        "--audio-from", "{input}",
        "--out", "{out_fixed}",
    ],
}

# 修复轮之后重新验收的命令（--processed 指向 out_fixed）
WM_REVERIFY_CMD = [
    "{python}",
    "{scripts}/verify.py",
    "--original", "{input}",
    "--processed", "{out_fixed}",
    "--mask", "{mask}",
    "--bbox", "{bbox}",
    "--out", "{report}",
]

DEFAULT_PAD = int(os.getenv("DEFAULT_PAD", "10"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"}
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "verbose"}},
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", "INFO")},
}

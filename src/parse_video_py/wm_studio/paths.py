# -*- coding: utf-8 -*-
"""skill 脚本定位 + 工作区目录 + 二进制探测。

skill 脚本一律**当子进程/模块调用**，不复制到项目里——这样 skill 升级
（踩坑手册里的修复）能立刻被调参台吃到，不会出现"项目里是旧版脚本"。
"""

import os
import shutil
import subprocess
from pathlib import Path

# 允许用环境变量覆盖（skill 装在别处时）
_ENV_SKILL = "WM_SKILL_DIR"
_ENV_WORKSPACE = "WM_WORKSPACE"


def skill_scripts_dir() -> Path:
    """video-watermark-removal skill 的 scripts 目录。"""
    env = os.environ.get(_ENV_SKILL)
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    p = Path.home() / ".workbuddy/skills/video-watermark-removal/scripts"
    if p.is_dir():
        return p
    raise FileNotFoundError(
        "找不到 video-watermark-removal 脚本目录。"
        f"请先安装 skill，或设置环境变量 {_ENV_SKILL} 指向其 scripts 目录。"
    )


def workspace_root() -> Path:
    """调参台工作区：每部片一个子目录，存 mask / 预览 / 成片 / 日志。"""
    env = os.environ.get(_ENV_WORKSPACE)
    if env:
        p = Path(env).expanduser().resolve()
    else:
        p = Path(__file__).resolve().parents[3] / "wm_studio_workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def script(name: str) -> Path:
    """取 skill 下某个脚本路径，不存在则报错。"""
    p = skill_scripts_dir() / name
    if not p.is_file():
        raise FileNotFoundError(f"skill 脚本缺失: {p}")
    return p


FFMPEG_CANDIDATES = (
    os.environ.get("WM_FFMPEG"),
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "ffmpeg",
)

FFPROBE_CANDIDATES = (
    os.environ.get("WM_FFPROBE"),
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
    "ffprobe",
)


def _first_existing(cands) -> str:
    for c in cands:
        if not c:
            continue
        found = (
            shutil.which(c)
            if not os.path.isabs(c)
            else (c if os.path.exists(c) else None)
        )
        if found:
            return found
    return "ffmpeg"


FFMPEG = _first_existing(FFMPEG_CANDIDATES)
FFPROBE = _first_existing(FFPROBE_CANDIDATES)


def run_capture(cmd, **kw):
    """跑命令并返回 CompletedProcess（不抛异常）。"""
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)

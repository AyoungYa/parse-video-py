# -*- coding: utf-8 -*-
"""产品版管线脚本的公共工具。

设计约定（所有脚本一致，pipeline.py 依赖这些约定）：
  * **mask 一律是 bbox 局部尺寸**（h×w，白=255 修复区），bbox 是全图坐标
    "x y w h" 四个整数 —— 与 wm_studio / skill 的既有契约完全兼容。
  * bbox 由 build_mask.py 负责：云端给了就用云端的，没给就自动定位，
    并把**最终 bbox** 打印成一行 `BBOX_RESULT: x y w h`（pipeline 解析回填）。
  * 退出码：0=成功；3=定位置信度不足（转人工，不重试）；其他=失败。
  * 进度行格式 `n/total`（如 `1843/4428 frames`），pipeline 靠它上报进度。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def find_binary(name: str, extra_candidates: tuple[str, ...] = ()) -> str:
    """按候选顺序找二进制。跨平台部署（Mac/Linux/Win）不能写死 homebrew 路径。"""
    for cand in (os_env_override(name), *extra_candidates, name):
        if not cand:
            continue
        found = shutil.which(cand)
        if found:
            return found
    return name


def os_env_override(name: str) -> str | None:
    return os.environ.get(f"WM_{name.upper()}")


FFMPEG = find_binary("ffmpeg", ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"))
FFPROBE = find_binary("ffprobe", ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"))

BBOX_RESULT_RE = re.compile(r"^BBOX_RESULT:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", re.M)


def parse_bbox(s: str | None) -> tuple[int, int, int, int] | None:
    """解析 "x y w h"。空/格式错返回 None（= 自动定位）。"""
    if not s or not s.strip():
        return None
    parts = s.replace(",", " ").split()
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (int(float(p)) for p in parts)
    except ValueError:
        return None
    if w <= 0 or h <= 0 or x < 0 or y < 0:
        return None
    return x, y, w, h


def bbox_str(b: tuple[int, int, int, int]) -> str:
    return "{} {} {} {}".format(*b)


def emit_bbox(b: tuple[int, int, int, int]) -> None:
    """把最终 bbox 打给 pipeline（必须是独立一行，格式固定）。"""
    print("BBOX_RESULT: " + bbox_str(b), flush=True)


def probe(path: str | Path, entries: str) -> str:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={entries}", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    )
    return (r.stdout or "").strip()


def probe_video_info(path: str | Path) -> dict:
    """分辨率 / 帧率 / 总帧数 / 时长（一次探测，dict 返回）。"""
    w = probe(path, "width")
    h = probe(path, "height")
    rate = probe(path, "r_frame_rate")
    nb = probe(path, "nb_frames")
    du = probe(path, "duration")
    num, _, den = (rate or "25/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 25.0
    frames = int(nb) if nb.isdigit() else 0
    duration = float(du) if du else 0.0
    if not frames and duration and fps:
        frames = int(round(duration * fps))
    return {"w": int(w), "h": int(h), "fps": fps, "frames": frames, "duration": duration}


def die(code: int, message: str) -> "NoReturn":  # noqa: F821
    print(f"ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def ok(message: str) -> None:
    print(message, flush=True)


# 退出码约定（pipeline.py 侧有对应分支，改这里必须同步改那边）
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_LOCATE_LOW_CONFIDENCE = 3

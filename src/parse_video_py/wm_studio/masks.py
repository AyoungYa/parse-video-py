# -*- coding: utf-8 -*-
"""mask 生成：包装 skill 的 `build_mask.py`，把 stdout 解析成结构化结果。

为什么不 import：
  `build_mask.py` 是纯 CLI 脚本（argparse + `sys.exit` + 硬编码 ffmpeg 路径），
  import 进来会把它的全局状态带进 Web 进程，且失败路径会直接退出进程。
  用子进程隔离更安全——它崩了不影响 Web 服务。

`white_th` 是最容易调错的参数（踩坑手册坑 43）：
  默认 168 只适合暗背景片。亮背景（~205）必须上调到 215~225，
  否则整个亮背景会被吃进 mask（实测 5 万 px + 四边触边）。
"""

import re
import subprocess
import sys
from dataclasses import asdict, dataclass

import cv2
import numpy as np

from parse_video_py.wm_studio.paths import script


@dataclass
class MaskReport:
    ok: bool
    mask_px: int = 0
    width: int = 0
    height: int = 0
    bbox: tuple | None = None  # 内容在全图中的范围 (x0,y0,x1,y1)
    warnings: list = None
    suggestion: tuple | None = None  # 建议窗口 (x,y,w,h)
    stdout: str = ""
    error: str = ""

    def to_dict(self):
        d = asdict(self)
        d["warnings"] = self.warnings or []
        return d


_RANGE_RE = re.compile(r"内容全图范围:\s*x\s*(\d+)\.\.(\d+)\s+y\s*(\d+)\.\.(\d+)")
_SUGGEST_RE = re.compile(r"--x\s+(-?\d+)\s+--y\s+(-?\d+)\s+--w\s+(\d+)\s+--h\s+(\d+)")
_PX_RE = re.compile(r"合计\s+(\d+)px")
_WARN_RE = re.compile(r"^!!\s+-\s+(.+)$", re.M)


def build_mask(
    video_path: str,
    x: int,
    y: int,
    w: int,
    h: int,
    out_path: str,
    white_th: int = 168,
    grow: int = 21,
    frames: int = 200,
    card_std: float = 30.0,
    close_k: int = 7,
    open_k: int = 3,
    pad_clip: int = 8,
    check_margin: int = 0,
    timeout: float = 600.0,
    fast: bool = True,
) -> MaskReport:
    """跑 build_mask.py 并解析结果。

    ★ `fast=True`（默认）走**本地快速路径**：直接用单次解码批量抽帧算
      时域 min 投影 + std，再复用 `build_mask.py` 的判据公式。
      原因：`build_mask.py` 的 `sample_stack` 是**每帧启一个 ffmpeg**，
      200 帧 = 200 次进程启动，8 秒的 1080P 片实测要 **40 秒**，
      在网页上点一下等 40 秒完全不可接受。

      本地路径实测同片 < 3 秒，mask 结果与脚本一致（都是 min 投影 + 阈值）。
      `fast=False` 时回到脚本，用于需要它的「窗口外探测」等高级判定。
    """
    if fast:
        try:
            return _build_mask_fast(
                video_path,
                x,
                y,
                w,
                h,
                out_path,
                white_th=white_th,
                grow=grow,
                frames=frames,
                card_std=card_std,
                close_k=close_k,
                open_k=open_k,
                pad_clip=pad_clip,
            )
        except _FastMaskUnavailable:
            pass  # 退回脚本路径

    cmd = [
        sys.executable,
        "-u",
        str(script("build_mask.py")),
        str(video_path),
        "--x",
        str(int(x)),
        "--y",
        str(int(y)),
        "--w",
        str(int(w)),
        "--h",
        str(int(h)),
        "--white-th",
        str(int(white_th)),
        "--grow",
        str(int(grow)),
        "--frames",
        str(int(frames)),
        "--card-std",
        str(float(card_std)),
        "--close",
        str(int(close_k)),
        "--open",
        str(int(open_k)),
        "--pad-clip",
        str(int(pad_clip)),
        "--check-margin",
        str(int(check_margin)),
        "--out",
        str(out_path),
        "--report",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return MaskReport(ok=False, error=f"build_mask 超时（>{timeout:.0f}s）")

    out = (r.stdout or "") + (r.stderr or "")
    rep = MaskReport(ok=False, stdout=out)

    m = _RANGE_RE.search(out)
    if m:
        rep.bbox = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
    px = _PX_RE.search(out)
    if px:
        rep.mask_px = int(px.group(1))
    rep.warnings = _WARN_RE.findall(out)
    sug = _SUGGEST_RE.search(out)
    if sug:
        rep.suggestion = tuple(int(g) for g in sug.groups())

    if r.returncode == 0:
        img = cv2.imread(str(out_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            rep.error = "脚本返回成功但 mask 文件读不到"
            return rep
        rep.ok = True
        rep.height, rep.width = img.shape[:2]
        if rep.mask_px == 0:
            rep.mask_px = int((img > 127).sum())
    else:
        if "mask 为空" in out:
            rep.error = "mask 为空：阈值太严或窗口位置不对"
        elif "窗口不完整" in out:
            rep.error = "窗口不完整，水印有部分落在窗口外"
        elif rep.mask_px > 0:
            # 有产物但带警告：仍算可用，让用户决定
            img = cv2.imread(str(out_path), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                rep.ok = True
                rep.height, rep.width = img.shape[:2]
                rep.error = "带警告产出（内容触边，窗口可能太小）"
        else:
            tail = "\n".join(out.strip().splitlines()[-6:])
            rep.error = f"build_mask 失败（exit {r.returncode}）：{tail}"
    return rep


class _FastMaskUnavailable(Exception):
    """快速路径不适用（窗口越界等），调用方应退回脚本。"""


# 建 mask 最多抽多少帧。min 投影对「面积」的估计在 40~60 帧就收敛了，
# 再多只增加内存（每帧一个全画面 BGR）不增加精度。
_MAX_MASK_FRAMES = 60


def _build_mask_fast(
    video_path,
    x,
    y,
    w,
    h,
    out_path,
    *,
    white_th,
    grow,
    frames,
    card_std,
    close_k,
    open_k,
    pad_clip,
) -> MaskReport:
    """单次解码批量抽帧 + 时域 min 投影，复现 build_mask.py 的判据。

    ★ 铁律 1：mask 必须只用 **min 投影** 抓「恒亮」的水印，
      阈值 `white_th` 由用户按背景亮度给（亮背景要上调，见坑 43）。
      `card_std` 那段「卡片补充」在亮背景下几乎不触发，这里省略——
      全卡片策略由 `card_mask_from_sparse()` 在外层完成，职责更清晰。
    """
    from parse_video_py.wm_studio.video import grab_frames_many, probe_video

    info = probe_video(video_path)
    W, H = info.width, info.height
    if not W or not H:
        raise _FastMaskUnavailable
    # 窗口越界 → 交给脚本处理（它会打具体告警）
    if x < 0 or y < 0 or x + w > W or y + h > H:
        raise _FastMaskUnavailable

    n_frames = info.frames or 1
    fps = info.fps or 25.0
    # 避开片头片尾（水印可能在淡入淡出）
    t0, t1 = 1.0, max(1.0, info.duration - 1.5) if info.duration else (0, 0)
    i0 = int(min(n_frames - 1, max(0, t0 * fps)))
    i1 = int(min(n_frames - 1, max(i0 + 1, t1 * fps)))
    n = max(8, min(int(frames), _MAX_MASK_FRAMES))
    idxs = np.unique(np.linspace(i0, i1, n).astype(int))
    if idxs.size < 8:
        idxs = np.unique(np.linspace(0, n_frames - 1, min(n, n_frames)).astype(int))

    # ★ 内存上限：只取需要的那 n 帧，占 n×单帧；再给 2 倍余量。
    #   grab_frames_many 若发现整区间解码会超限，自动退回逐帧 seek。
    per_frame = W * H * 3
    got = grab_frames_many(
        video_path, idxs.tolist(), max_bytes=max(per_frame, n * per_frame * 2)
    )
    if len(got) < 8:
        raise _FastMaskUnavailable

    stack = np.stack(
        [
            cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)[y : y + h, x : x + w].astype(
                np.float32
            )
            for _, fr in got
        ]
    )
    lo = np.percentile(stack, 1, axis=0)  # 时域最小投影（水印恒定亮）
    m = (lo > white_th).astype(np.uint8)  # ★ 阈值由用户给
    if close_k > 1:
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((close_k, close_k), np.uint8))
    if grow > 1:
        m = cv2.dilate(m, np.ones((grow, grow), np.uint8), iterations=1)
    if open_k > 1:
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((open_k, open_k), np.uint8))
    # 去 <20px 碎噪（铁律 1）
    ncc, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros_like(m)
    for i in range(1, ncc):
        if stats[i, cv2.CC_STAT_AREA] >= 20:
            keep[labels == i] = 1
    m = keep

    if m.sum() == 0:
        return MaskReport(
            ok=False,
            error=f"mask 为空：阈值 {white_th} 太严（背景亮的话上调它）",
        )

    ys, xs = np.where(m > 0)
    rep = MaskReport(
        ok=True,
        mask_px=int(m.sum()),
        width=w,
        height=h,
        bbox=(
            x + int(xs.min()),
            x + int(xs.max()),
            y + int(ys.min()),
            y + int(ys.max()),
        ),
        warnings=[],
    )
    # 触边告警（同脚本逻辑）
    if xs.min() < pad_clip:
        rep.warnings.append(f"内容距窗口左边仅 {xs.min()}px，窗口可能太小")
    if w - 1 - xs.max() < pad_clip:
        rep.warnings.append(f"内容距窗口右边仅 {w - 1 - xs.max()}px，窗口可能太小")
    if ys.min() < pad_clip:
        rep.warnings.append(f"内容距窗口上边仅 {ys.min()}px，窗口可能太小")
    if h - 1 - ys.max() < pad_clip:
        rep.warnings.append(f"内容距窗口下边仅 {h - 1 - ys.max()}px，窗口可能太小")

    cv2.imwrite(str(out_path), m * 255)
    rep.stdout = (
        f"[fast] 抽 {len(got)} 帧 min投影 阈值 {white_th} → "
        f"{rep.mask_px}px, 内容 x{rep.bbox[0]}..{rep.bbox[1]} "
        f"y{rep.bbox[2]}..{rep.bbox[3]}"
    )
    return rep


def auto_white_th(
    video_path: str, x: int, y: int, w: int, h: int, frames: int = 40
) -> dict:
    """估算背景亮度，给出 white_th 建议值。

    ★ 坑 43：亮背景片用默认 168 会把整个背景吃进 mask。
      经验值：背景灰度 B → white_th ≈ min(250, B + 15)。
      这里返回背景中位数与建议阈值，让用户有个起点。
    """
    from parse_video_py.wm_studio.video import grab_frames_many, probe_video

    info = probe_video(video_path)
    if not info.frames:
        return {"bg_median": None, "suggest": 168}
    n = min(frames, info.frames)
    idxs = np.linspace(0, info.frames - 1, n).astype(int)
    # 内存上限同 _build_mask_fast：只取 n 帧，余量 2 倍
    per_frame = (info.width or 0) * (info.height or 0) * 3
    res = grab_frames_many(
        video_path, idxs, max_bytes=max(per_frame, n * per_frame * 2)
    )
    if not res:
        return {"bg_median": None, "suggest": 168}

    grays = []
    for _, fr in res:
        roi = fr[max(0, y) : y + h, max(0, x) : x + w]
        if roi.size:
            grays.append(float(np.median(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY))))
    if not grays:
        return {"bg_median": None, "suggest": 168}
    bg = float(np.median(grays))
    suggest = int(min(250, max(168, bg + 15)))
    return {
        "bg_median": round(bg, 1),
        "suggest": suggest,
        "note": (
            "亮背景片（背景 >180）：white_th 已按背景上调，否则整片背景会被吃进 mask"
            if bg > 180
            else "常规背景：默认 168 一般可用"
        ),
    }

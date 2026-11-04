# -*- coding: utf-8 -*-
"""视频探测与精确抽帧。

抽帧**必须按绝对帧号**（`select=eq(n,K)`），不能用 `-ss` 搜索——
`-ss` 会漂到相邻帧，导致预览看到的是"隔壁那一帧"，调参就白调了。
（对应踩坑手册的帧对齐坑。）
"""

import os
import subprocess
from dataclasses import asdict, dataclass

import cv2
import numpy as np

from parse_video_py.wm_studio.paths import FFMPEG, FFPROBE, run_capture


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    frames: int
    duration: float
    has_audio: bool

    def to_dict(self):
        return asdict(self)


def _probe_one(entries: str, path: str) -> str:
    r = run_capture(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            f"stream={entries}",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ]
    )
    return (r.stdout or "").strip()


def probe_video(path: str) -> VideoInfo:
    """探测分辨率 / 帧率 / 总帧数 / 时长 / 是否有音轨（带 mtime 缓存）。"""
    path = str(path)
    # 缓存键带 mtime+size：文件被替换（重新渲染覆盖）时自动失效
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, 0, 0)
    hit = _INFO_CACHE.get(key)
    if hit is not None:
        return hit
    info = _probe_video_uncached(path)
    _INFO_CACHE[key] = info
    return info


_INFO_CACHE: dict = {}


def _probe_video_uncached(path: str) -> VideoInfo:
    w = _probe_one("width", path)
    h = _probe_one("height", path)
    rate = _probe_one("r_frame_rate", path)
    nb = _probe_one("nb_frames", path)

    num, _, den = (rate or "25/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 25.0

    width = int(w) if w.isdigit() else 0
    height = int(h) if h.isdigit() else 0

    frames = int(nb) if nb.isdigit() else 0
    duration = 0.0
    if frames and fps > 0:
        duration = frames / fps
    if not frames or not duration:
        # mkv/webm 常无 nb_frames，退回容器时长
        d = _probe_one("duration", path)
        try:
            duration = float(d)
        except ValueError:
            duration = 0.0
        if duration and fps > 0:
            frames = int(round(duration * fps))

    r = run_capture(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=nw=1:nk=1",
            path,
        ]
    )
    has_audio = bool((r.stdout or "").strip())

    return VideoInfo(
        path=path,
        width=width,
        height=height,
        fps=fps,
        frames=frames,
        duration=duration,
        has_audio=has_audio,
    )


def grab_frame_bgr(path: str, frame_idx: int) -> np.ndarray | None:
    """按**绝对帧号**精确取一帧 BGR。帧号非法/解码失败返回 None。

    性能要点（4K 长片实测踩过）：
      - `-ss` 放在 `-i` **之前** = 输入级 seek（跳到最近关键帧，毫秒级）；
        放在 `-i` 之后 = 先解码再丢弃前面所有帧，7239 帧的 4K 片要 30 秒。
      - 输入级 seek 后 ffmpeg 会重置时间戳，`select=eq(n,K)` 的 `n` 从 0 数，
        所以要按「seek 到 t0 后还需前进多少帧」= `idx - round(t0*fps)` 来挑。
      - t0 选在目标前 `preroll` 秒；关键帧间隔通常 < 2s，故 2s 足够。
      - 挑不到目标帧（容器时间戳不规整 / 关键帧过稀）时，自动**放大 preroll
        重试一次**，最后才退回全片精确挑帧（慢但稳）。
    """
    info = probe_video(path)
    if frame_idx < 0 or (info.frames and frame_idx >= info.frames):
        return None
    if not info.width or not info.height:
        return None

    fps = info.fps or 25.0
    idx = int(frame_idx)
    need = info.width * info.height * 3

    for preroll in (2.0, 6.0):
        t0 = idx / fps - preroll
        if t0 <= 0:
            continue
        rel = idx - int(round(t0 * fps))
        if rel < 0:
            continue
        cmd = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{t0:.4f}",
            "-i",
            str(path),
            "-vf",
            f"select=eq(n\\,{rel})",
            "-frames:v",
            "1",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-",
        ]
        r = subprocess.run(cmd, capture_output=True)
        buf = np.frombuffer(r.stdout, np.uint8) if r.stdout else np.empty(0, np.uint8)
        if buf.size >= need:
            return buf[:need].reshape(info.height, info.width, 3).copy()

    # 兜底：全片精确挑帧（慢，仅在 seek 路径失效时走）
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        f"select=eq(n\\,{idx})",
        "-frames:v",
        "1",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-",
    ]
    r = subprocess.run(cmd, capture_output=True)
    buf = np.frombuffer(r.stdout, np.uint8) if r.stdout else np.empty(0, np.uint8)
    if buf.size < need:
        return None
    return buf[:need].reshape(info.height, info.width, 3).copy()


def _grab_at_ts(path: str, t: float, info: VideoInfo) -> np.ndarray | None:
    """按**时间戳**取一帧 BGR（输入级 seek，关键帧跳转，毫秒级）。

    这是批量抽帧的**低内存**路径：每次只让 ffmpeg 吐一帧，
    内存占用 = 单帧 BGR（4K 也才 24MB），与视频时长完全无关。
    """
    need = info.width * info.height * 3
    r = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, t):.4f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-",
        ],
        capture_output=True,
    )
    buf = np.frombuffer(r.stdout, np.uint8) if r.stdout else np.empty(0, np.uint8)
    if buf.size < need:
        return None
    return buf[:need].reshape(info.height, info.width, 3).copy()


# 批量抽帧的「整区间解码」内存上限（超过就逐帧 seek）
# 为什么要有这个开关：整区间解码快（单次解码），但要把**区间内所有帧**的
# rawvideo 全读进内存。8 秒 1080P 片还好（1GB），150 秒 1440P 片要 **29GB**，
# 4K 长片更是 70GB+ → 直接把机器打爆。超过上限时退回逐帧 seek。
BATCH_DECODE_LIMIT = 1 << 30  # 1 GiB


def grab_frames_many(
    path: str, indices, max_bytes: int | None = None
) -> list[tuple[int, np.ndarray]]:
    """按帧号列表批量取帧。返回 [(帧号, BGR), ...]。

    两条路径，按**内存**自动选：

    1. **整区间解码**（快，内存高）：一次 `select='between(n,A,B)'` 解码整个
       `[min,max]` 区间，再按帧号挑。耗时 ≈ 一次解码，内存 ≈ 区间帧数 × 单帧。
       只适合短区间（首尾跨度 × 单帧 ≤ `max_bytes`）。
    2. **逐帧 seek**（慢一点，内存恒定）：对每个帧号算时间戳，
       用**输入级 `-ss`**（在 `-i` 之前，关键帧跳转）各取一帧。
       内存 = 单帧（4K 24MB），与片长无关。

    ★ 为什么必须分流：老实现一律走路径 1，150 秒的 1440P 片建 mask 时要
      一次性读 **29GB** rawvideo（`demo/weibo-1027.mp4`，实测推算），
      4K 289 秒片要 **77GB**。这个内存在普通机器上必 OOM，
      而且真正需要的只有 120 帧 ≈ 1GB —— 浪费 30~60 倍。
    """
    idxs = sorted({int(i) for i in indices})
    if not idxs:
        return []
    info = probe_video(path)
    if not info.width or not info.height or not info.frames:
        return []

    lo, hi = max(0, idxs[0]), min(info.frames - 1, idxs[-1])
    if hi < lo:
        return []

    fps = info.fps or 25.0
    fsz = info.width * info.height * 3
    limit = BATCH_DECODE_LIMIT if max_bytes is None else int(max_bytes)
    span_bytes = (hi - lo + 1) * fsz

    if span_bytes > limit:
        # 路径 2：逐帧 seek。内存恒定 = 单帧。
        out = []
        for n in idxs:
            fr = _grab_at_ts(path, n / fps, info)
            if fr is not None:
                out.append((n, fr))
        return out

    # 路径 1：整区间解码
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        f"select='between(n\\,{lo}\\,{hi})'",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if not r.stdout:
        return []

    raw = np.frombuffer(r.stdout, np.uint8)
    count = raw.size // fsz
    wanted = set(idxs)
    out = []
    for k in range(count):
        n = lo + k
        if n in wanted:
            out.append(
                (
                    n,
                    raw[k * fsz : (k + 1) * fsz]
                    .reshape(info.height, info.width, 3)
                    .copy(),
                )
            )
    return out


def frame_to_jpeg_bytes(frame_bgr: np.ndarray, quality: int = 88) -> bytes:
    ok, enc = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return enc.tobytes()


def crop_roi(frame_bgr: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    """夹紧后裁剪 ROI（越界自动缩到画面内）。"""
    H, W = frame_bgr.shape[:2]
    x0 = max(0, min(int(x), W - 1))
    y0 = max(0, min(int(y), H - 1))
    x1 = max(x0 + 1, min(int(x + w), W))
    y1 = max(y0 + 1, min(int(y + h), H))
    return frame_bgr[y0:y1, x0:x1]

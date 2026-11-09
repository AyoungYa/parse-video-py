# -*- coding: utf-8 -*-
"""产品版 LaMa 逐帧修复（流式管道，零临时帧文件）。

移植自 demo/wm_wcj/lama_apply_maskonly.py（该版本已在真实交付片上验证），
仅做三处产品化改造：
  1. CLI 从位置参数改为 --input/--mask/--out/--bbox 契约（pipeline.py 调用）
  2. ffmpeg/ffprobe 从 homebrew 硬编码改为 PATH 探测（跨平台部署）
  3. 进度行输出 `n/total`，pipeline 靠它上报渲染进度

保留下来的铁律（全部来自踩坑手册实测教训）：
  * mask alpha 合成：只回贴 mask 内像素，mask 外真实像素一个不动（铁律 2）
  * 逐帧独立 LaMa（--keyframe-every 1），静态贴回会留印记（坑 12/13/16）
  * x264 必须 -threads 1，否则抢 CPU 逐帧 2.2s→4.5s
  * MPS/CUDA 异步，计时前同步
  * 音轨兜底三连（copy → aac → 无声版）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage as ndimage_module

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wm_common import FFMPEG, FFPROBE, die, parse_bbox  # noqa: E402


def probe(video: str, entries: str) -> str:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={entries}", "-of", "csv=p=0", video],
        capture_output=True, text=True,
    )
    out = (r.stdout or "").strip()
    return out.splitlines()[0] if out else ""


def probe_fps(video: str) -> float:
    s = probe(video, "r_frame_rate") or "25/1"
    num, _, den = s.partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 25.0


def probe_size(video: str) -> tuple[int, int]:
    wh = probe(video, "width,height")
    w, h = wh.split(",")[:2]
    return int(w), int(h)


def probe_total_frames(video: str) -> int:
    nb = probe(video, "nb_frames")
    if nb.isdigit():
        return int(nb)
    try:
        return int(round(float(probe(video, "duration")) * probe_fps(video)))
    except ValueError:
        return 0


def _sync_accel() -> None:
    """MPS/CUDA 异步执行；落盘与计时前需同步，否则读数虚低。"""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
        elif torch.backends.mps.is_available():
            torch.mps.synchronize()
    except Exception:  # noqa: BLE001
        pass


def inpaint_region(lama, frame_bgr, bbox, pad, precise_mask=None):
    """裁剪 bbox 周边区域送 LaMa，修复后**只按 mask alpha 贴回**（铁律 2）。"""
    x, y, w, h = bbox
    H, W = frame_bgr.shape[:2]
    x0, y0 = max(0, x - pad * 3), max(0, y - pad * 3)
    x1, y1 = min(W, x + w + pad * 3), min(H, y + h + pad * 3)

    crop = frame_bgr[y0:y1, x0:x1]
    pil = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

    mask = np.zeros(crop.shape[:2], dtype=np.uint8)
    if precise_mask is not None:
        # 精确形状 + 小膨胀余量（把文字抗锯齿边缘盖全）
        pm = ndimage_module.binary_dilation(
            precise_mask.astype(np.uint8),
            structure=np.ones((3, 3)), iterations=max(2, pad // 3))
        mask[y - y0:y - y0 + h, x - x0:x - x0 + w] = pm * 255
    else:
        mask[max(0, y - y0 - pad):max(0, y - y0) + h + pad,
             max(0, x - x0 - pad):max(0, x - x0) + w + pad] = 255

    out = lama(pil, Image.fromarray(mask))
    _sync_accel()
    out_arr = np.array(out)
    # LaMa 输出可能被 pad 到 8 的倍数，裁回 crop 原尺寸
    ch, cw = crop.shape[:2]
    out_arr = out_arr[:ch, :cw]
    if out_arr.shape[:2] != (ch, cw):
        out_arr = cv2.resize(out_arr, (cw, ch))

    result = frame_bgr.copy()
    inp = cv2.cvtColor(out_arr, cv2.COLOR_RGB2BGR)
    # ★ 只回贴 mask 内像素：mask 外的真实画面一个像素都不动。
    a = (mask > 127).astype(np.float32)
    a = cv2.GaussianBlur(a, (0, 0), sigmaX=1.0)     # 3px 内羽化，避免合成接缝
    a = np.clip(a, 0.0, 1.0)[..., None]
    base = frame_bgr[y0:y1, x0:x1].astype(np.float32)
    result[y0:y1, x0:x1] = (base * (1.0 - a) + inp.astype(np.float32) * a).astype(np.uint8)
    return result


def render(lama, video, out_silent, bbox, pad, precise_mask, crf=18,
           progress_every=50) -> int:
    """流式：解码 → 逐帧 LaMa → 编码，不落临时帧。"""
    W, H = probe_size(video)
    fsz = W * H * 3
    total = probe_total_frames(video) or 1

    dec_cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-i", video, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    enc_cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
               "-r", str(probe_fps(video)),
               "-i", "-",
               "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
               "-threads", "1",            # ★ 必须限 1：x264 抢 CPU 实测 2.2s→4.5s/帧
               "-pix_fmt", "yuv420p", "-an", out_silent]

    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE)
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)

    n, t0 = 0, time.time()
    while True:
        buf = dec.stdout.read(fsz)
        if len(buf) < fsz:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
        out = inpaint_region(lama, frame, bbox, pad, precise_mask)
        enc.stdin.write(out.tobytes())
        n += 1
        if n % progress_every == 0:
            _sync_accel()
            el = time.time() - t0
            # ★ 进度行格式 n/total —— pipeline 靠正则抠它上报进度
            print(f"{n}/{total} frames  {el:.0f}s  {el / n:.2f}s/frame", flush=True)

    enc.stdin.close()
    enc.wait()
    dec.stdout.close()
    dec.wait()
    if enc.returncode != 0:
        die(1, f"编码器退出码 {enc.returncode}")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="产品版 LaMa 逐帧去水印（mask alpha 合成）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--mask", required=True, help="bbox 局部尺寸 mask PNG（白=修复区）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--bbox", required=True, help='"x y w h" 全图坐标')
    ap.add_argument("--pad", type=int, default=10)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "mps", "cuda"])
    ap.add_argument("--keyframe-every", type=int, default=1,
                    help="必须 1（逐帧独立修复）；>1 是省算力档，会留印记（坑 16）")
    ap.add_argument("--crf", type=int, default=18)
    args = ap.parse_args()

    bbox = parse_bbox(args.bbox)
    if bbox is None:
        die(1, f"--bbox 格式错误: {args.bbox!r}")

    precise_mask = None
    m = cv2.imread(args.mask, cv2.IMREAD_GRAYSCALE)
    if m is None:
        die(1, f"mask 文件读取失败: {args.mask}")
    x, y, w, h = bbox
    if m.shape != (h, w):
        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
    precise_mask = m > 127
    if not precise_mask.any():
        die(1, f"mask 全空: {args.mask}")
    print(f"[mask] 精确模式: {int(precise_mask.sum())}px "
          f"({precise_mask.sum() / precise_mask.size * 100:.0f}% of bbox)", flush=True)

    video, out = args.input, args.out
    silent = out + ".silent.mp4"
    audio = out + ".audio.m4a"

    t0 = time.time()
    print("[1/3] 加载 LaMa 模型（首次运行自动下载 big-lama.pt ~200MB）...", flush=True)
    import torch

    dev = args.device
    if dev == "auto":
        if torch.cuda.is_available():
            dev = "cuda"
        elif torch.backends.mps.is_available():
            dev = "mps"
        else:
            dev = "cpu"
    print(f"      推理设备: {dev}", flush=True)
    from simple_lama_inpainting import SimpleLama

    lama = SimpleLama(device=dev)

    print("[2/3] 流式渲染（解码 → LaMa → 编码）...", flush=True)
    if args.keyframe_every != 1:
        print("  ⚠️ keyframe-every != 1 属省算力档，交付级不推荐（坑 16）", flush=True)
    n = render(lama, video, silent, bbox, args.pad, precise_mask, crf=args.crf)
    print(f"[3/3] 渲染完成: {n} 帧, 用时 {time.time() - t0:.0f}s", flush=True)

    # 音轨兜底三连：copy → aac → 无声版（--start 0 产物无音轨的坑在这里兜住）
    print("[+1] 混流原声...", flush=True)
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", video, "-vn", "-c:a", "copy", audio], capture_output=True)
    if r.returncode != 0 or not os.path.exists(audio):
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", video, "-vn", "-c:a", "aac", audio], check=False)
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", silent, "-i", audio, "-map", "0:v", "-map", "1:a",
                    "-c", "copy", "-shortest", out], check=False)
    if not os.path.exists(out):
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", silent, "-c", "copy", out], check=True)
        print("   (原声提取失败, 已输出无声版)", flush=True)

    for f in (silent, audio):
        try:
            os.remove(f)
        except OSError:
            pass

    print(f"完成: {out} (总耗时 {time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

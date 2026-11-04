# -*- coding: utf-8 -*-
"""LaMa AI 逐帧水印修复（流式管道版，零临时帧文件）。

方案:
  1. 水印 bbox 周边裁剪局部 patch 送 LaMa(big-lama) 修复——结构感知填充,
     远强于 delogo 的线性插值; 只贴回局部, 画面其余像素 100% 原样
  2. 固定水印 = 逐帧独立 LaMa(--keyframe-every 1)。静态贴回会留"印记",
     光流 warp 是负优化(见 SKILL.md 坑 12/13/16)
  3. **流式管道**: ffmpeg 解码 -> Python 逐帧处理 -> ffmpeg 编码,
     全程不落临时帧文件。旧版"先抽全部 PNG"在 1080P/3471 帧要吃掉
     7GB+ 磁盘, 磁盘满时 ffmpeg exit 228(见 SKILL.md 坑 17)

用法:
  python lama_remove.py <视频> <x> <y> <w> <h> --out 成片.mp4
      [--start 40] [--duration 8] [--pad 10] [--keyframe-every 1] [--crf 18]
      [--mask-file watermark_mask_1080p.png]

依赖: simple-lama-inpainting / torch / opencv / ffmpeg
  权重 big-lama.pt (~200MB) 首次自动下载; GitHub 直连失败时用镜像:
  curl -L -o ~/.cache/torch/hub/checkpoints/big-lama.pt \
    "https://ghfast.top/https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt"
"""

import argparse
import subprocess
import sys
import time

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage as ndimage_module
from simple_lama_inpainting import SimpleLama

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"


def probe(video: str, entries: str) -> str:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={entries}", "-of", "csv=p=0", video],
        capture_output=True, text=True,
    )
    return r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""


def probe_fps(video: str) -> str:
    return probe(video, "r_frame_rate") or "25"


def probe_size(video: str):
    wh = probe(video, "width,height")
    w, h = wh.split(",")[:2]
    return int(w), int(h)


def inpaint_region(lama, frame_bgr, bbox, pad, precise_mask=None):
    """裁剪 bbox 周边区域送 LaMa, 修复后贴回(其余像素原样)。

    precise_mask: (h, w) bool 数组，True = 修复区(水印精确形状)。
    提供时用精确形状代替矩形 mask——水印周边画面(移动灯光等)完全不碰，
    LaMa 只重建水印真正覆盖的像素，修复面积更小、更不易露馅。
    """
    x, y, w, h = bbox
    H, W = frame_bgr.shape[:2]
    x0, y0 = max(0, x - pad * 3), max(0, y - pad * 3)
    x1, y1 = min(W, x + w + pad * 3), min(H, y + h + pad * 3)

    crop = frame_bgr[y0:y1, x0:x1]
    pil = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

    mask = np.zeros(crop.shape[:2], dtype=np.uint8)
    if precise_mask is not None:
        # 精确形状 + 小膨胀余量(把文字抗锯齿边缘盖全)
        pm = ndimage_module.binary_dilation(
            precise_mask.astype(np.uint8),
            structure=np.ones((3, 3)), iterations=max(2, pad // 3))
        mask[y - y0:y - y0 + h, x - x0:x - x0 + w] = pm * 255
    else:
        mask[max(0, y - y0 - pad):max(0, y - y0) + h + pad,
             max(0, x - x0 - pad):max(0, x - x0) + w + pad] = 255

    out = lama(pil, Image.fromarray(mask))
    out_arr = np.array(out)
    # LaMa 输出可能被 pad 到 8 的倍数, 裁回 crop 原尺寸
    ch, cw = crop.shape[:2]
    out_arr = out_arr[:ch, :cw]
    if out_arr.shape[:2] != (ch, cw):
        out_arr = cv2.resize(out_arr, (cw, ch))

    result = frame_bgr.copy()
    result[y0:y1, x0:x1] = cv2.cvtColor(out_arr, cv2.COLOR_RGB2BGR)
    return result


def warp_flow(src: np.ndarray, flow: np.ndarray) -> np.ndarray:
    """按光流 backward warp（保留给漂移水印场景，固定水印不用）。"""
    h, w = flow.shape[:2]
    gx, gy = np.meshgrid(np.arange(w), np.arange(h))
    mx = (gx + flow[..., 0]).astype(np.float32)
    my = (gy + flow[..., 1]).astype(np.float32)
    return cv2.remap(src, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def blend_patch(frame: np.ndarray, key_result: np.ndarray, bbox, pad: int) -> np.ndarray:
    """把关键帧的修复结果静态贴回当前帧（只用于 keyframe_every>1 的省算力档）。

    - bbox 核心区(水印所在): 100% 用关键帧修复结果
    - bbox 外围余量带: 按羽化权重与当前帧融合
    注意: 会留"印记"，交付级不要用，见 SKILL.md 坑 16。
    """
    x, y, w, h = bbox
    H, W = frame.shape[:2]
    x0, y0 = max(0, x - pad * 3), max(0, y - pad * 3)
    x1, y1 = min(W, x + w + pad * 3), min(H, y + h + pad * 3)

    result = frame.copy()
    region_k = key_result[y0:y1, x0:x1]
    region_f = frame[y0:y1, x0:x1]

    core = np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
    cy0, cy1 = max(0, y - y0 - pad), min(y1 - y0, y - y0 + h + pad)
    cx0, cx1 = max(0, x - x0 - pad), min(x1 - x0, x - x0 + w + pad)
    core[cy0:cy1, cx0:cx1] = 1.0
    feather = cv2.GaussianBlur(core, (0, 0), sigmaX=pad * 1.5)
    alpha = np.clip(feather, 0, 1)[..., None]

    blended = (region_f * (1 - alpha) + region_k * alpha).astype(np.uint8)
    result[y0:y1, x0:x1] = blended
    return result


def render(lama, video, out_silent, bbox, pad, precise_mask, keyframe_every,
           start, duration, progress=50):
    """流式: 解码 -> 逐帧 LaMa -> 编码, 不落临时帧。"""
    W, H = probe_size(video)
    fsz = W * H * 3

    dec_cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"]
    if start:
        dec_cmd += ["-ss", str(start)]
    dec_cmd += ["-i", video]
    if duration:
        dec_cmd += ["-t", str(duration)]
    dec_cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]

    enc_cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
               "-r", probe_fps(video).split("/")[0], "-i", "-",
               "-c:v", "libx264", "-preset", "fast", "-crf", str(CRF),
               "-threads", "1",          # ★ 必须限 1：x264 默认自动开 ~1.5*ncpu
                                         # 线程，会把 CPU 从 LaMa 抢光（实测 load
                                         # 8 核机器冲到 48+，逐帧耗时 2.2s→4.5s）
               "-pix_fmt", "yuv420p", "-an", out_silent]

    dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE)
    enc = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE)

    n, t0, key_result = 0, time.time(), None
    while True:
        buf = dec.stdout.read(fsz)
        if len(buf) < fsz:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3).copy()
        if key_result is None or n % max(1, keyframe_every) == 0:
            key_result = inpaint_region(lama, frame, bbox, pad, precise_mask)
            out = key_result
        else:
            out = blend_patch(frame, key_result, bbox, pad)
        enc.stdin.write(out.tobytes())
        n += 1
        if n % progress == 0:
            el = time.time() - t0
            print(f"    {n} frames  {el:.0f}s  {el / n:.2f}s/frame", flush=True)

    enc.stdin.close()
    enc.wait()
    dec.stdout.close()
    dec.wait()
    return n


CRF = 18


def main():
    global CRF
    ap = argparse.ArgumentParser(description="LaMa AI 去水印(流式, 局部推理)")
    ap.add_argument("video")
    ap.add_argument("x", type=int)
    ap.add_argument("y", type=int)
    ap.add_argument("w", type=int)
    ap.add_argument("h", type=int)
    ap.add_argument("--out", default=None)
    ap.add_argument("--start", type=float, default=0, help="起始秒(打样用)")
    ap.add_argument("--duration", type=float, default=0, help="时长秒, 0=全片")
    ap.add_argument("--pad", type=int, default=10)
    ap.add_argument("--keyframe-every", type=int, default=1)
    ap.add_argument("--mask-file", default=None,
                    help="精确形状 mask PNG(白=修复区); 尺寸需 = (w,h)")
    ap.add_argument("--crf", type=int, default=18)
    args = ap.parse_args()
    CRF = args.crf

    bbox = (args.x, args.y, args.w, args.h)
    precise_mask = None
    if args.mask_file:
        m = cv2.imread(args.mask_file, cv2.IMREAD_GRAYSCALE)
        if m is None:
            raise SystemExit(f"mask 文件读取失败: {args.mask_file}")
        if m.shape != (args.h, args.w):
            m = cv2.resize(m, (args.w, args.h), interpolation=cv2.INTER_NEAREST)
        precise_mask = m > 127
        print(f"[mask] 精确模式: {int(precise_mask.sum())}px "
              f"({precise_mask.sum() / precise_mask.size * 100:.0f}% of bbox 窗口)")

    video = args.video
    out = args.out or video.rsplit(".", 1)[0] + "_lama.mp4"
    silent = out + ".silent.mp4"
    audio = out + ".audio.m4a"

    t0 = time.time()
    print("[1/4] 加载 LaMa 模型(首次运行自动下载 big-lama.pt ~200MB)...", flush=True)
    lama = SimpleLama(device="cpu")

    print("[2/4] 流式渲染(解码 -> LaMa -> 编码)...", flush=True)
    n = render(lama, video, silent, bbox, args.pad, precise_mask,
               args.keyframe_every, args.start, args.duration)
    print(f"[3/4] 视频流完成: {n} 帧, 用时 {time.time() - t0:.0f}s", flush=True)

    print("[4/4] 混流原声...", flush=True)
    acmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if args.start:
        acmd += ["-ss", str(args.start)]
    acmd += ["-i", video]
    if args.duration:
        acmd += ["-t", str(args.duration)]
    acmd += ["-vn", "-c:a", "copy", audio]
    r = subprocess.run(acmd, capture_output=True)
    if r.returncode != 0 or not __import__("os").path.exists(audio):
        acmd[-1] = audio
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", video, "-vn", "-c:a", "aac", audio], check=False)
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", silent,
         "-i", audio, "-map", "0:v", "-map", "1:a", "-c", "copy",
         "-shortest", out], check=False)
    if not __import__("os").path.exists(out):
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", silent, "-c", "copy", out], check=True)
        print("   (原声提取失败, 已输出无声版)")

    for f in (silent, audio):
        try:
            __import__("os").remove(f)
        except OSError:
            pass

    print(f"完成: {out} (总耗时 {time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

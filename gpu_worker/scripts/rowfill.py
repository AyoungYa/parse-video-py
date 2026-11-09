"""rowfill.py — 渐变背景专用：行插值填充 + 边界羽化 + 匹配微噪

适用：水印底下是平滑渐变面（光照渐变/纯色墙面/天空等）。
原理：每行取卡片左右锚点带均值，线性过渡填充整行。
     零发明内容、零伪噪点、时间稳定（锚点随画面自然动）。
羽化：填充区边缘高斯渐隐，与周边真实画面无缝衔接。
微噪：按周边环 detail-σ 补白噪（模拟传感器噪点，防"塑料感"）。
"""
import argparse
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

# 产品化：ffmpeg/ffprobe 探测 + bbox 契约（pipeline 调用）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wm_common import FFMPEG, FFPROBE, parse_bbox  # noqa: E402


def probe(path, entries):
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", f"stream={entries}", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    return (r.stdout or "").strip().split("\n")


def video_meta(path):
    w, h = (int(v) for v in probe(path, "width,height")[:2])
    fr = probe(path, "r_frame_rate")[0]
    num, _, den = fr.partition("/")
    fps = float(num) / float(den or 1)
    return w, h, fps


def render(video, out_path, bbox, args):
    VW, VH, fps = video_meta(video)
    x, y, w, h = bbox
    dur_s = probe(str(video), "duration")[0]
    try:
        total = max(1, int(round(float(dur_s) * fps)))
    except ValueError:
        total = 1
    print(f"源片 {VW}x{VH} @ {fps:g}fps  填充区 x{x} y{y} w{w} h{h}", flush=True)

    # 填充区（bbox）+ 羽化权重
    zone = np.zeros((VH, VW), np.float32)
    zone[y:y+h, x:x+w] = 1.0
    wgt = cv2.GaussianBlur(zone, (0, 0), args.feather)[..., None]
    wgt = np.clip(wgt, 0, 1)

    # 锚点带（bbox 左右外侧）
    AL0, AL1 = max(0, x - args.anchor - 4), max(1, x - 4)
    AR0, AR1 = min(VW - 1, x + w + 5), min(VW, x + w + 5 + args.anchor)
    if AL1 <= AL0 or AR1 <= AR0:
        raise SystemExit("bbox 贴边，锚点区不足")
    print(f"锚点带: 左 {AL0}-{AL1}  右 {AR0}-{AR1}  羽化 σ={args.feather}", flush=True)

    # 噪声基准环（bbox 外扩 ring px）
    ring_zone = np.zeros((VH, VW), bool)
    ry0, ry1 = max(0, y - args.ring), min(VH, y + h + args.ring)
    rx0, rx1 = max(0, x - args.ring), min(VW, x + w + args.ring)
    ring_zone[ry0:ry1, rx0:rx1] = True
    ring_zone[y:y+h, x:x+w] = False

    rng = np.random.default_rng(args.seed)

    dec = subprocess.Popen(
        [FFMPEG, "-v", "error", "-i", str(video), "-f", "rawvideo",
         "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=10**8)
    enc = subprocess.Popen(
        [FFMPEG, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{VW}x{VH}", "-r", f"{fps:g}", "-i", "-",
         "-c:v", "libx264", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
         "-threads", "2", str(out_path)],
        stdin=subprocess.PIPE)

    frame_bytes = VW * VH * 3
    n = 0
    while True:
        raw = dec.stdout.read(frame_bytes)
        if len(raw) < frame_bytes:
            break
        frame = np.frombuffer(raw, np.uint8).reshape(VH, VW, 3).astype(np.float32)

        # 1) 行插值填充
        la = frame[:, AL0:AL1].mean(axis=1)      # (VH,3)
        ra = frame[:, AR0:AR1].mean(axis=1)      # (VH,3)
        t = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :, None]
        interp = la[:, None, :] * (1 - t) + ra[:, None, :] * t   # (VH,w,3)

        # 2) 匹配微噪（逐帧独立 = 传感器噪点时间特性）
        gray = frame.mean(2)
        blur_g = cv2.GaussianBlur(gray, (0, 0), 1.2)
        s_ring = float((gray - blur_g)[ring_zone].std())
        nsig = min(s_ring * args.noise_gain, args.noise_cap)
        noise = rng.normal(0, max(nsig, 0.0), (VH, VW, 1)).astype(np.float32)

        # 3) 合成：填充区 = 插值 + 微噪；羽化过渡
        filled = interp + noise[:, x:x+w, :]
        out = frame.copy()
        blk = out[:, x:x+w]
        out[:, x:x+w] = blk * (1 - wgt[:, x:x+w]) + filled * wgt[:, x:x+w]
        out = np.clip(out, 0, 255)

        enc.stdin.write(out.astype(np.uint8).tobytes())
        n += 1
        if n % 60 == 0:
            # ★ n/total 格式 —— pipeline 靠它上报进度
            print(f"{n}/{total} frames  σ_ring={s_ring:.2f} noise={nsig:.2f}", flush=True)

    dec.stdout.close(); dec.wait()
    enc.stdin.close(); enc.wait()
    print(f"完成: {out_path}  共 {n} 帧", flush=True)
    return n


def main():
    ap = argparse.ArgumentParser(description="渐变背景水印区行插值填充")
    ap.add_argument("--input", required=True)
    ap.add_argument("--mask", default=None, help="（契约占位）rowfill 用整块 bbox 填充，无需 mask")
    ap.add_argument("--bbox", required=True, help='"x y w h" 全图坐标')
    ap.add_argument("--out", required=True)
    ap.add_argument("--audio-from", dest="audio_from", default=None)
    ap.add_argument("--anchor", type=int, default=24, help="锚点带宽度(px)")
    ap.add_argument("--feather", type=float, default=4.0, help="边界羽化 σ")
    ap.add_argument("--ring", type=int, default=40, help="噪声基准环宽")
    ap.add_argument("--noise-gain", type=float, default=1.0)
    ap.add_argument("--noise-cap", type=float, default=0.5)
    ap.add_argument("--crf", type=int, default=17)
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    bbox = parse_bbox(args.bbox)
    if bbox is None:
        raise SystemExit(f"--bbox 格式错误: {args.bbox!r}")
    args.x, args.y, args.w, args.h = bbox

    v = Path(args.input)
    if not v.exists():
        raise SystemExit(f"文件不存在: {v}")

    if args.audio_from:
        tmp = Path(args.out).with_name(Path(args.out).stem + "_silent_raw.mp4")
        render(v, tmp, (args.x, args.y, args.w, args.h), args)
        print(f"混流音轨: {args.audio_from} → {args.out}", flush=True)
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-i", str(tmp), "-i", str(args.audio_from),
             "-map", "0:v", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac",
             "-shortest", str(args.out)], check=True)
        tmp.unlink(missing_ok=True)
    else:
        render(v, args.out, (args.x, args.y, args.w, args.h), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

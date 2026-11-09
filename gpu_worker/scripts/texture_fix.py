# -*- coding: utf-8 -*-
"""消除 LaMa 修复区的「痕迹」：低频结构修复 + 周边真实纹理搬运。

两类痕迹（都会被 ①③ 指标漏掉，但肉眼一眼看到）：

A. **平滑痕迹（过平滑）**：LaMa 在大块平坦区输出**过平滑**的填充，高频
   「细节能量」明显低于周边（实测修复区 σ=2.9 vs 周边 σ=5.1，比值 0.6）。
   → 解法：纹理搬运（--strength），把周边真实细节层分块平铺进修复区。

B. **结构性痕迹（低频错误）**：场景中横穿水印区的条带结构（河岸/地平线/
   光带）在修复区**断裂**、变成模糊光斑，甚至凭空发明亮斑；静态场景下还
   会逐帧闪烁（LaMa 对近似相同的输入帧编出不同内容，实测相邻帧差 3.2 vs
   噪声地板 0.2）。这是**低频/结构性**错误，补高频颗粒救不了。
   → 解法：--lf-mode rowinterp —— 低频用「左右两侧无水印区**逐行线性插值**」
   替换（条带是水平结构 → 逐行插值天然延续它，且**永不复制物体**），
   高频保留 LaMa 细节。锚点取自 bbox 左右两侧（水印不可能盖到那里，
   LaMa 成片在这些区域与原片逐像素一致，直接用成片即可）。

用法:
  # A 类（平滑痕迹）:
  python texture_fix.py 成片.mp4 --x 930 --y 10 --mask mask.png \
      --strength 1.3 --out 成片_去痕.mp4
  # A+B 类都有（推荐先打样确认条带场景）:
  python texture_fix.py 成片.mp4 --x 930 --y 10 --mask mask.png \
      --lf-mode rowinterp --strength 1.3 --audio-from 原片.mp4 --out final.mp4

依赖: numpy / opencv-python / ffmpeg
"""

import argparse
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

# 实测标定：固定强度即可，不必自适应。
#   ★ 踩过的弯路（务必看）：曾试图用「内/外 σ 比」做逐帧自适应反解，连改三版
#     都更差。原因有两个，都很隐蔽：
#     1. **测量口径陷阱**：如果拿实心矩形当 mask 去测 σ（而不是真实的稀疏
#        mask PNG），mask 外的平坦区会被算进来，比值被稀释（实测 0.99 被
#        测成 0.59）→ 误以为"补不够"，于是往上一路加码，越补越糟。
#        **测纹理比值必须用与修复一致的那张稀疏 mask。**
#     2. 自适应反解本身不稳健：σ_in 里已含 LaMa 抹平成分，与注入 patch 的
#        空间分布不同，解析式系统性偏差。
#   ★ 结论：固定 --strength 1.3 实测 median σ比 **0.99**（1.0=完美），
#     比任何自适应版本（median 0.81）都好。**简单方案胜出。**
K_PATCH_EFF = None  # 保留符号，不再用于反解

# 产品化：ffmpeg/ffprobe 不再写死 homebrew 路径（worker 要跨平台部署）
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_sys.path.insert(0, str(_Path(__file__).resolve().parent))
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
    dur = float(probe(path, "duration")[0])
    return w, h, fps, dur


def build_ring(mask_bool, ring_px):
    """mask 外环（用于取细节样本）：膨胀 ring_px 减去 mask 本身。"""
    k = np.ones((ring_px * 2 + 1, ring_px * 2 + 1), np.uint8)
    outer = cv2.dilate(mask_bool.astype(np.uint8), k) > 0
    inner = cv2.dilate(mask_bool.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    return outer & ~inner


def estimate_detail_sigma(img, ring, sigma=1.2):
    """细节层 = 图 - 高斯模糊；返回该区域的细节标准差。"""
    blur = cv2.GaussianBlur(img, (0, 0), sigma)
    detail = img - blur
    return float(detail[ring].std()), detail


def make_texture_patch(img, ring, need_mask, sigma, rng, tile=48):
    """用 ring 的真实细节层**分块平铺**，生成覆盖 need_mask 的颗粒图。

    ★ 为什么用"分块"而不是"逐像素随机采样"：
      逐像素采样的结果 = 独立白噪声，与真实纹理的**空间相关性**不符，
      铺上去像"沙沙的椒盐"，且强度被后续模糊吃掉一大半（实测只把
      内/外 σ 比从 0.62 提到 0.64，几乎没用）。
      分块平铺保留了真实纹理的**局部结构与相关性**，才是"真颗粒"。

    做法：以 tile 为格，随机在 ring 内挑若干个 tile 原点，整块抠细节层，
    直接铺到 need_mask 上；不重叠、不插值。
    """
    blur = cv2.GaussianBlur(img, (0, 0), sigma)
    detail = img - blur
    ys, xs = np.where(ring)
    if len(ys) == 0:
        return np.zeros_like(img)
    H, W = img.shape[:2]
    out = np.zeros_like(img)
    yy, xx = np.where(need_mask)
    if len(yy) == 0:
        return out
    y0, y1, x0, x1 = yy.min(), yy.max() + 1, xx.min(), xx.max() + 1
    hh, ww = y1 - y0, x1 - x0
    # 预解析：把 ring 像素按"能容纳一个 tile 的锚点"组织，保证块内不越界
    ry, rx = np.where(ring)
    ok = (ry >= tile) & (ry < H - tile) & (rx >= tile) & (rx < W - tile)
    ay, ax = ry[ok], rx[ok]
    if len(ay) == 0:
        ay, ax = ry, rx
    n_ty = (hh + tile - 1) // tile
    n_tx = (ww + tile - 1) // tile
    idx = rng.integers(0, len(ay), size=n_ty * n_tx)
    for i in range(n_ty):
        for j in range(n_tx):
            sy = ay[idx[i * n_tx + j]]
            sx = ax[idx[i * n_tx + j]]
            blk = detail[sy:sy + tile, sx:sx + tile]
            dy0, dx0 = y0 + i * tile, x0 + j * tile
            bh = min(tile, y1 - dy0)
            bw = min(tile, x1 - dx0)
            if bh <= 0 or bw <= 0:
                continue
            sub = need_mask[dy0:dy0 + bh, dx0:dx0 + bw]
            out[dy0:dy0 + bh, dx0:dx0 + bw][sub] = blk[:bh, :bw][sub]
    return out


def render(video, out_path, mask_bool, bbox, args):
    x, y, w, h = bbox
    VW, VH, fps, dur = video_meta(video)
    total = max(1, int(round(dur * fps)))
    print(f"源片 {VW}x{VH} @ {fps:g}fps  {dur:.2f}s（约 {total} 帧）", flush=True)

    # mask 全图摆放
    mx, my = x, y
    mh, mw = mask_bool.shape
    mfull = np.zeros((VH, VW), bool)
    y1 = min(VH, my + mh)
    x1 = min(VW, mx + mw)
    mfull[my:y1, mx:x1] = mask_bool[:y1 - my, :x1 - mx]
    ring = build_ring(mfull, args.ring)
    if ring.sum() == 0:
        raise SystemExit("外环为空，请减小 --ring")

    # 羽化权重（mask 边缘渐隐，避免硬接缝）
    wgt = cv2.GaussianBlur(mfull.astype(np.float32), (0, 0), args.feather)
    wgt = np.clip(wgt, 0, 1)

    # 低频结构修复（B 类痕迹：条带断裂/发明亮斑）
    if args.lf_mode == "rowinterp":
        kD = np.ones((args.lf_dilate * 2 + 1, args.lf_dilate * 2 + 1), np.uint8)
        mD = cv2.dilate(mfull.astype(np.uint8), kD) > 0
        wlf = cv2.GaussianBlur(mD.astype(np.float32), (0, 0), args.lf_feather)
        wlf = np.clip(wlf, 0, 1)[..., None]
        # 锚点列范围（bbox 左右两侧的无水印区；LaMa 成片在这些区域=原片）
        xl0, xl1 = max(0, x - 38), max(1, x - 4)
        xr0, xr1 = min(VW - 1, x + w + 4), min(VW, x + w + 20)
        if xl1 <= xl0 or xr1 <= xr0:
            raise SystemExit("bbox 贴边，左右锚点区不足，无法做 rowinterp")
        print(f"低频修复: rowinterp  σ={args.lf_sigma}  锚点列 {xl0}-{xl1} / {xr0}-{xr1}",
              flush=True)

    rng = np.random.default_rng(args.seed)

    # 解码 → 处理 → 编码（流式）
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
    stat_in, stat_ring = [], []
    while True:
        raw = dec.stdout.read(frame_bytes)
        if len(raw) < frame_bytes:
            break
        frame = np.frombuffer(raw, np.uint8).reshape(VH, VW, 3).astype(np.float32)

        # ── B 类修复：低频逐行插值（先做，颗粒统计基于修复后的帧）──
        if args.lf_mode == "rowinterp":
            ww = x1 - mx
            lo = cv2.GaussianBlur(frame, (0, 0), args.lf_sigma)
            la = lo[:, xl0:xl1].mean(axis=1)   # (VH,3) 左锚点
            ra = lo[:, xr0:xr1].mean(axis=1)   # (VH,3) 右锚点
            t = np.linspace(0.0, 1.0, ww, dtype=np.float32)[None, :, None]
            interp = la[:, None, :] * (1 - t) + ra[:, None, :] * t   # (VH,ww,3)
            # 只替换低频、保留原帧高频：frame + (interp - lo)
            corrected = frame[:, mx:x1].copy()
            corrected += (interp - lo[:, mx:x1])
            frame[:, mx:x1] = frame[:, mx:x1] * (1 - wlf[:, mx:x1]) \
                + corrected * wlf[:, mx:x1]

        gray = frame.mean(2)
        s_in, _ = estimate_detail_sigma(gray, mfull, args.layer_sigma)
        s_ring, _ = estimate_detail_sigma(gray, ring, args.layer_sigma)
        stat_in.append(s_in)
        stat_ring.append(s_ring)

        # 目标：把修复区细节 σ 补到**周边水平**。
        #   ★ 关键 1：target 基准是 s_ring（而不是差值 s_ring-s_in）——
        #     实测"补差值"严重不足（差值再被羽化吃掉一半）。
        #   ★ 关键 2：固定 strength 1.3 实测最优（median σ比 0.99）。
        #     曾试过按 σ 比逐帧自适应反解，三版都更差（median 0.81）——
        #     详见文件头 K_PATCH_EFF 注释里的「测量口径陷阱」。
        #     --target-ratio 保留为可选自救开关（默认关闭）。
        strength = args.strength
        if args.target_ratio and args.target_ratio > 0:
            # 可选：仅做**单向下限保护** —— 只补"明显欠纹理"的帧，
            # 且补到 target 就停，避免把已经锐的帧推过头。
            ratio_now = s_in / max(s_ring, 1e-6)
            if ratio_now >= args.target_ratio or s_ring <= 0.05:
                strength = 0.0

        if strength > 1e-3:
            # ★ 时间稳定性：每帧用**相同种子**重建 rng —— 平铺块位置全片固定，
            #   块内容取自该帧的周边真实细节。若用同一个 rng 连续消耗，块位置
            #   逐帧重随机 → 静态场景下颗粒图案每帧跳变 = 可见的"噪声闪烁"
            #   （实测静态段 mask 区相邻帧差 p90=3.4，噪声地板 0.2）。
            #   固定位置后：静态场景颗粒完全静止，动态场景随场景自然流动。
            rng_f = np.random.default_rng(args.seed)
            patch = make_texture_patch(gray, ring, mfull, args.layer_sigma, rng_f,
                                       tile=args.tile)
            cur = patch[mfull].std()
            if cur > 1e-6:
                target = s_ring * strength
                patch = patch * (target / cur)
            # 边缘羽化渐隐，避免硬接缝
            add = (patch * wgt)[..., None]
            frame = np.clip(frame + add, 0, 255)

        enc.stdin.write(frame.astype(np.uint8).tobytes())
        n += 1
        if n % 100 == 0:
            # ★ n/total 格式 —— pipeline 靠它上报进度
            print(f"{n}/{total} frames  σ_in={s_in:.2f} σ_ring={s_ring:.2f}", flush=True)

    dec.stdout.close()
    dec.wait()
    enc.stdin.close()
    enc.wait()

    if stat_in:
        print(f"完成: {out_path}  共 {n} 帧", flush=True)
        print(f"  修复区细节 σ: 处理前均值 {np.mean(stat_in):.2f}"
              f"  (周边参照 {np.mean(stat_ring):.2f})", flush=True)
    return n


def main():
    ap = argparse.ArgumentParser(description="消除 LaMa 修复区的平滑痕迹（纹理搬运）")
    ap.add_argument("--input", required=True, help="LaMa 去水印成片")
    ap.add_argument("--mask", "--mask-file", dest="mask", required=True,
                    help="精确 mask PNG（与去水印同一张，bbox 局部尺寸）")
    ap.add_argument("--bbox", required=True, help='"x y w h" 全图坐标（pipeline 契约）')
    ap.add_argument("--out", required=True)
    ap.add_argument("--audio-from", dest="audio_from", default=None,
                    help="可选：从该文件（通常是原片）取音轨混入输出；"
                         "不给则输出为无声成片")
    ap.add_argument("--ring", type=int, default=40, help="取纹理样本的外环宽度(px)")
    ap.add_argument("--layer-sigma", type=float, default=1.2,
                    help="细节层高斯 σ（越大保留越多低频）")
    ap.add_argument("--feather", type=float, default=2.0, help="mask 边缘羽化 σ")
    ap.add_argument("--strength", type=float, default=1.3,
                    help="颗粒目标倍率。1.0=补到周边 σ；实测 1.3 最贴合"
                         "（内/外 σ 比落在 0.9~1.2）；1.6 会略过。")
    ap.add_argument("--target-ratio", type=float, default=0.0,
                    help="可选「跳过阈值」：若该帧 σ_in/σ_ring 已 ≥ 此值，则**不补**"
                         "（避免把本就锐的帧推过头）。默认 0 = 关闭，全程用"
                         "固定 --strength（实测最优）。")
    ap.add_argument("--ratio-cap", type=float, default=3.5,
                    help="（保留）强度上限占位")
    ap.add_argument("--tile", type=int, default=48, help="纹理搬运的块大小(px)")
    ap.add_argument("--lf-mode", choices=["off", "rowinterp"], default="off",
                    help="低频结构修复（B 类痕迹：条带断裂/发明亮斑/逐帧闪烁）。"
                         "rowinterp = 低频用 bbox 左右两侧无水印区逐行线性插值"
                         "替换（水平条带天然延续、永不复制物体），高频保留 LaMa"
                         "细节。默认 off。")
    ap.add_argument("--lf-sigma", type=float, default=9.0,
                    help="rowinterp 的低频/高频分割 σ")
    ap.add_argument("--lf-dilate", type=int, default=8,
                    help="rowinterp 的作用范围 = mask 膨胀像素数")
    ap.add_argument("--lf-feather", type=float, default=8.0,
                    help="rowinterp 作用范围的边缘羽化 σ")
    ap.add_argument("--device", choices=["auto", "cpu", "mps", "cuda"], default="auto",
                    help="（保留参数）本脚本走 numpy 细节层搬运，不需要推理设备；"
                         "保留是为了与 lama_apply.py 的参数习惯一致")
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    bbox = parse_bbox(args.bbox)
    if bbox is None:
        raise SystemExit(f"--bbox 格式错误: {args.bbox!r}")
    args.x, args.y = bbox[0], bbox[1]

    v = Path(args.input)
    if not v.exists():
        raise SystemExit(f"文件不存在: {v}")
    m = cv2.imread(args.mask, cv2.IMREAD_GRAYSCALE)
    if m is None:
        raise SystemExit(f"mask 读不到: {args.mask}")
    mask_bool = m > 127
    print(f"mask {mask_bool.shape[1]}x{mask_bool.shape[0]}  {int(mask_bool.sum())}px", flush=True)

    if args.audio_from:
        # 先渲染无声，再用原片音轨混流
        tmp = Path(args.out).with_name(Path(args.out).stem + "_silent_raw.mp4")
        render(v, tmp, mask_bool,
               (args.x, args.y, mask_bool.shape[1], mask_bool.shape[0]), args)
        print(f"混流音轨: {args.audio_from} → {args.out}", flush=True)
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-i", str(tmp), "-i", str(args.audio_from),
             "-map", "0:v", "-map", "1:a?", "-c:v", "copy", "-c:a", "aac",
             "-shortest", str(args.out)], check=True)
        tmp.unlink(missing_ok=True)
    else:
        render(v, args.out, mask_bool,
               (args.x, args.y, mask_bool.shape[1], mask_bool.shape[0]), args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

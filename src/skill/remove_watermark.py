# -*- coding: utf-8 -*-
"""视频去水印工具链（固定位置叠加层水印，如微博「视频号」磨砂卡片）。

定位方法（自动，无需人工框选）——双通道联合:
  通道 A: 帧间差图 D（相邻帧 |diff| 的均值）
          磨砂叠加层把底下画面变化抹掉一半以上 → 卡片整块 D 低
          （能抓到完整卡片含磨砂边框）
  通道 B: 时域 std 图（像素沿时间轴的波动）
          白字/Logo 核心完全不透明 → 核心 std 极低
          （能抓到文字核心，作交叉验证）
  两通道候选重叠 → 取并集框（文字核心 + 磨砂边框 = 完整水印）;
  仅单通道命中 → 取该通道结果（B 命中时用 D 图自适应外扩补磨砂边）。

  最后做两项验证:
    - 位置恒定性: 卡片区 vs 同高背景带的时域 std 比 < 0.75
    - 结构验证: 卡片内拉普拉斯方差(白字锐利) vs 磨砂底

  输出 ffmpeg delogo 参数（含安全边距与画面边界 clamp）。

用法:
  python remove_watermark.py <视频文件> [--out 输出.mp4]
      [--preview] [--fps 1] [--pad 6] [--threshold 0.45]

依赖: numpy, opencv-python-headless, scipy, ffmpeg(命令行)
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage

# 候选尺寸门槛: 绝对下限(文字核心不可能比这小) + 相对上限(排除整块静止区域)
_MIN_W, _MIN_H = 60, 15
_MAX_W_FRAC, _MAX_H_FRAC = 0.40, 0.25


def extract_frames(video: str, fps: float = 1.0, max_frames: int = 60) -> list:
    """用 ffmpeg 均匀抽帧（默认 1fps、至多 60 帧），返回 PNG 路径列表。"""
    out_dir = tempfile.mkdtemp(prefix="wm_frames_")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video,
         "-vf", f"fps={fps}", "-frames:v", str(max_frames),
         f"{out_dir}/f_%04d.png"],
        check=True,
    )
    return sorted(Path(out_dir).glob("f_*.png"))


def _load_gray(frames) -> np.ndarray:
    return np.stack(
        [cv2.imread(str(f), cv2.IMREAD_GRAYSCALE).astype(np.float32) for f in frames]
    )


def _largest_rect(mask: np.ndarray):
    """形态学清理后取最大连通域的 bbox (x0, y0, x1, y1)，无则 None。"""
    mask = ndimage.binary_closing(mask, np.ones((9, 9)))
    mask = ndimage.binary_opening(mask, np.ones((5, 5)))
    lab, n = ndimage.label(mask)
    if n == 0:
        return None
    sizes = ndimage.sum(mask, lab, range(1, n + 1))
    i = int(np.argmax(sizes)) + 1
    ys, xs = np.where(lab == i)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()), sizes[i - 1]


def _bbox_ok(x0, y0, x1, y1, W, H, fill) -> bool:
    w, h = x1 - x0 + 1, y1 - y0 + 1
    return (
        w >= _MIN_W and h >= _MIN_H
        and w <= W * _MAX_W_FRAC and h <= H * _MAX_H_FRAC
        and fill > 0.25
    )


def locate_watermark(frames, threshold: float = 0.45, pad: int = 6):
    """双通道扫描定位固定位置磨砂水印，返回候选列表与画面尺寸。"""
    arr = _load_gray(frames)
    H, W = arr.shape[1], arr.shape[2]

    # 通道 A: 帧间差图
    D = np.mean([np.abs(arr[i + 1] - arr[i]) for i in range(len(arr) - 1)], axis=0)
    # 通道 B: 时域 std
    std = arr.std(axis=0)
    # 结构验证用最大投影帧: 白色文字在亮度波动的画面里中值会被抹掉, max 才保真
    maxp = arr.max(axis=0)

    results = []
    corners = {
        "右上": (0, int(H * 0.22), int(W * 0.55), W),
        "左上": (0, int(H * 0.22), 0, int(W * 0.45)),
        "右下": (int(H * 0.78), H, int(W * 0.55), W),
        "左下": (int(H * 0.78), H, 0, int(W * 0.45)),
    }
    for name, (ya, yb, xa, xb) in corners.items():
        cand_a = _largest_rect(D[ya:yb, xa:xb] < np.median(D[ya:yb, xa:xb]) * threshold)
        cand_b = _largest_rect(
            std[ya:yb, xa:xb] < np.median(std[ya:yb, xa:xb]) * threshold
        )

        boxes = []
        for tag, cand in (("A", cand_a), ("B", cand_b)):
            if cand is None:
                continue
            wx0, wy0, wx1, wy1, size = cand
            # 窗口内坐标 -> 全图坐标
            x0, y0, x1, y1 = wx0 + xa, wy0 + ya, wx1 + xa, wy1 + ya
            fill = size / max(1, (x1 - x0 + 1) * (y1 - y0 + 1))
            if _bbox_ok(x0, y0, x1, y1, W, H, fill):
                boxes.append((tag, x0, y0, x1, y1))

        if not boxes:
            continue

        if len(boxes) == 2:
            # 两通道都命中: 重叠则并集, 不重叠取面积大的(帧间差通道优先)
            _, ax0, ay0, ax1, ay1 = boxes[0]
            _, bx0, by0, bx1, by1 = boxes[1]
            overlap = max(0, min(ax1, bx1) - max(ax0, bx0)) * max(
                0, min(ay1, by1) - max(ay0, by0)
            )
            if overlap > 0:
                x0, y0 = min(ax0, bx0), min(ay0, by0)
                x1, y1 = max(ax1, bx1), max(ay1, by1)
            else:
                x0, y0, x1, y1 = (ax0, ay0, ax1, ay1)
        else:
            _, x0, y0, x1, y1 = boxes[0]

        # 单靠 std(文字核心) 时, 用帧间差图自适应外扩盖住磨砂边
        if len(boxes) == 1 and boxes[0][0] == "B":
            dthr = np.median(D[ya:yb, xa:xb]) * 0.6
            h_ = y1 - y0 + 1
            max_grow = (x1 - x0 + 1)  # 每侧最多扩 1 倍核心宽
            gx0, gx1 = x0, x1
            while gx0 - 8 >= 0 and (gx0 + max_grow - x0) > 0 and \
                    D[y0:y1 + 1, gx0 - 8:gx0].mean() < dthr:
                gx0 -= 8
            while gx1 + 8 < W and (x1 - max_grow - gx1) < 0 and \
                    D[y0:y1 + 1, gx1 + 1:gx1 + 9].mean() < dthr:
                gx1 += 8
            if gx1 - gx0 > (x1 - x0):  # 外扩确实生效才采用
                x0, x1 = gx0, gx1

        # 结构验证: 排除"本来就静止的画面"(卡上白字应远锐于磨砂底/周边)
        card = maxp[y0:y1 + 1, x0:x1 + 1]
        sharp_card = cv2.Laplacian(card.astype(np.uint8), cv2.CV_64F).var()
        bg_x0 = max(0, x0 - (x1 - x0))
        bg = maxp[y0:y1 + 1, bg_x0:x0] if x0 - bg_x0 > 20 else None
        sharp_bg = (
            cv2.Laplacian(bg.astype(np.uint8), cv2.CV_64F).var()
            if bg is not None and bg.size else 1.0
        )
        sub_std = std[ya:yb, xa:xb]
        results.append({
            "corner": name,
            "x": max(1, x0 - pad), "y": max(1, y0 - pad),
            "w": min(W - x0 - 1, x1 - x0 + 1 + 2 * pad),
            "h": min(H - y0 - 1, y1 - y0 + 1 + 2 * pad),
            "sharpness_card": round(float(sharp_card), 0),
            "sharpness_bg": round(float(sharp_bg), 0),
        })

    # 软过滤: 双通道时域定位已是强证据, 锐度比只作置信度参考(防静止画面误检)
    for r in results:
        r["confidence"] = (
            "high" if r["sharpness_card"] > 10 * max(1.0, r["sharpness_bg"])
            else ("normal" if r["sharpness_card"] > 2 * max(1.0, r["sharpness_bg"])
                  else "low")
        )
    results.sort(key=lambda r: r["w"] * r["h"], reverse=True)
    return results, (W, H)


def verify_static(frames_arr: np.ndarray, bbox: dict) -> float:
    """位置恒定性: 卡片区 vs 同高背景带的时域 std 比。"""
    x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
    region = frames_arr[:, y:y + h, x:x + w]
    bg = frames_arr[:, y:y + h, max(0, x - w):x] if x - w >= 20 else region
    return float(region.std() / max(1e-6, bg.std()))


def verify_removed(video: str, bbox: dict, bright_thresh: int = 220) -> dict:
    """处理后校验: 水印区域内不应残留高亮文字像素。"""
    frames = extract_frames(video, fps=0.2, max_frames=12)
    arr = _load_gray(frames)
    x, y, w, h = bbox["x"], bbox["y"], bbox["w"], bbox["h"]
    maxp = arr[:, y:y + h, x:x + w].max(axis=0)
    return {
        "bright_pixels": int((maxp > bright_thresh).sum()),
        "region_std": float(arr[:, y:y + h, x:x + w].std()),
    }


def remove_watermark(video: str, out: str, bboxes: list,
                     crf: int = 18, preview: tuple | None = None) -> str:
    """ffmpeg delogo 去水印（多水印时逗号串联）。preview=(start,dur) 只渲染打样片段。"""
    filters = ",".join(
        f"delogo=x={b['x']}:y={b['y']}:w={b['w']}:h={b['h']}" for b in bboxes
    )
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if preview:
        cmd += ["-ss", str(preview[0]), "-t", str(preview[1])]
    cmd += [
        "-i", video, "-vf", filters,
        "-c:v", "libx264", "-preset", "fast", "-crf", str(crf),
        "-c:a", "copy", out,
    ]
    subprocess.run(cmd, check=True)
    return out


def main():
    ap = argparse.ArgumentParser(description="固定位置叠加水印自动定位+delogo 去除")
    ap.add_argument("video", help="输入视频文件")
    ap.add_argument("--out", default=None, help="输出文件 (默认 <input>_clean.mp4)")
    ap.add_argument("--fps", type=float, default=1.0, help="抽帧率 (默认 1fps)")
    ap.add_argument("--pad", type=int, default=6, help="delogo 区域外扩像素")
    ap.add_argument("--threshold", type=float, default=0.55,
                    help="磨砂判定: 图值 < 窗口中值*threshold (默认 0.55)")
    ap.add_argument("--crf", type=int, default=18, help="x264 crf (默认 18)")
    ap.add_argument("--preview", action="store_true",
                    help="只渲染 8 秒打样片段 (人工确认后再全片)")
    args = ap.parse_args()

    video = args.video
    out = args.out or str(Path(video).with_name(Path(video).stem + "_clean.mp4"))

    print(f"[1/4] 抽帧分析: {video}")
    frames = extract_frames(video, fps=args.fps)
    if len(frames) < 8:
        print("帧数不足 8, 无法做时域分析, 请降低 --fps 或检查视频", file=sys.stderr)
        sys.exit(1)
    print(f"      抽取 {len(frames)} 帧")

    print("[2/4] 双通道扫描(帧间差 + 时域std)定位水印...")
    bboxes, (W, H) = [], (0, 0)
    for thr in (args.threshold, args.threshold * 1.18, args.threshold * 0.82):
        if bboxes:
            break
        bboxes, (W, H) = locate_watermark(frames, threshold=thr, pad=args.pad)
        if bboxes:
            print(f"      (使用阈值 {thr:.2f})")
    if not bboxes:
        print("未定位到固定位置磨砂水印。", file=sys.stderr)
        print("可能: ① 水印漂移/滚动 ② 纯文字无磨砂底 ③ 背景本身静止(时域法无信号) "
              "④ 无水印。参考 SKILL.md 人工指定坐标流程。", file=sys.stderr)
        sys.exit(2)
    arr = _load_gray(frames)
    for b in bboxes:
        print(f"      {b['corner']}: x={b['x']} y={b['y']} w={b['w']} h={b['h']} "
              f"(sharpness {b['sharpness_card']:.0f} vs bg {b['sharpness_bg']:.0f})")
    print(json.dumps({"bboxes": bboxes, "size": [W, H]}, ensure_ascii=False, indent=2))

    print("[3/4] 位置恒定性验证...")
    ok = True
    for b in bboxes:
        ratio = verify_static(arr, b)
        b["static_ratio"] = round(ratio, 2)
        status = "OK" if ratio < 0.75 else "WARN(可能非静态, 慎用)"
        ok = ok and ratio < 0.75
        print(f"      {b['corner']}: 区域/背景 时域std比 = {ratio:.2f} {status}")
    if not ok:
        print("候选未通过恒定性验证, 建议人工核对后再渲染", file=sys.stderr)

    print("[4/4] ffmpeg delogo 去除...")
    if args.preview:
        sample = str(Path(out).with_name(Path(out).stem + "_sample8s.mp4"))
        remove_watermark(video, sample, bboxes, crf=args.crf,
                         preview=(int(10 / args.fps) if args.fps <= 1 else 10, 8))
        after = verify_removed(sample, bboxes[0])
        print(f"打样片段: {sample}")
        print(f"打样校验(卡片区): >220 亮像素 {after['bright_pixels']} 个, "
              f"std {after['region_std']:.0f} (应无残留白字)")
        print("人工确认打样后, 去掉 --preview 渲染全片")
    else:
        remove_watermark(video, out, bboxes, crf=args.crf)
        after = verify_removed(out, bboxes[0])
        print(f"完成: {out}")
        print(f"校验(卡片区): >220 亮像素 {after['bright_pixels']} 个, "
              f"std {after['region_std']:.0f}")
        print(json.dumps({"out": out, "bboxes": bboxes, "check": after},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

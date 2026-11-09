# -*- coding: utf-8 -*-
"""产品版验收：四项指标 + 决策树诊断，输出 pipeline 可消费的 report.json。

CLI 契约：
    python verify.py --original 原片 --processed 成片
                     --mask mask.png --bbox "x y w h" --out report.json

指标口径 = wm_studio/metrics.py 的四项（全部相对口径，坑 43）：
  ① glyph_drop     笔画残留降幅（%），≥ 90 达标
  ② region_max     修复区最亮值 ≤ 原片同区 +1（防发明亮块）
  ③ edge_ratio     边界梯度比 ≤ 1.5（防接缝，1.0 最佳）
  ④ sigma_ratio    纹理匹配 σ 比 0.8~1.6（防发糊/伪纹理，坑 37/44）
  ⑤ flicker_p90    修复区帧间差 p90 / 原片同区地板比（坑 39，防时间闪烁）

决策树诊断（坑 44，输出 diagnosis 供 pipeline 决定是否追加修复阶段）：
  clean            全绿，直接交付
  needs_texture    σ 比 < 0.8（过平滑）→ texture_fix 默认模式
  needs_rowinterp  Laplacian 能量比 > 1.5× 周边（LaMa 发明伪噪点/条带断裂）
                   → texture_fix --lf-mode rowinterp
  needs_review     其他不达标组合 → 转人工
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wm_common import die, ok, parse_bbox  # noqa: E402

from parse_video_py.wm_studio.metrics import (  # noqa: E402
    _detail,
    place_mask,
    ring_mask,
)
from parse_video_py.wm_studio.video import grab_frame_bgr, probe_video  # noqa: E402

# 判据阈值（与调参台/踩坑手册一致；改这里要同步前端 MetricReport 的文案）
TH_GLYPH_DROP = 90.0
TH_EDGE_RATIO = 1.5
TH_SIGMA_LO, TH_SIGMA_HI = 0.8, 1.6
TH_FLICKER = 1.5          # 成片/原片 帧间差地板比（与前端 MetricReport 的及格线一致）
TH_LAPLACIAN = 1.5        # 区/环 Laplacian 能量比（坑 44 B 类诊断）
TH_RESIDUAL_ABS = 3.0     # 成片字形残留绝对值（前端「残留 < 3.0」同口径）
TH_FLAT_RING_SIGMA = 1.0  # 环带 σ 低于此值 = 平坦背景，σ 比判据失效
TH_FLICKER_ABS = 1.0       # 修复区帧间差绝对上限（环带静止时的兜底判据）
TH_SIGMA_GAP_ABS = 0.8    # 平坦背景下改判绝对差上限


def _laplacian_energy(gray: np.ndarray, region: np.ndarray) -> float:
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    return float(np.abs(lap[region]).mean())


def _median_or_none(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return float(np.median(vals)) if vals else None


def _worst_or_none(vals: list, mode: str = "max") -> float | None:
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return float(max(vals) if mode == "max" else min(vals))


def sample_frames(video: str, k: int) -> list[int]:
    info = probe_video(video)
    if not info.frames:
        die(1, f"读不到帧数: {video}")
    # 避开片头片尾（淡入淡出/片尾黑场会污染读数）
    lo = int(min(info.frames - 1, max(0, info.fps)))
    hi = int(max(lo + 1, info.frames - 1 - 1.5 * info.fps))
    return np.unique(np.linspace(lo, hi, k).astype(int)).tolist()


def verify_pair(src: str, clean: str, mask: np.ndarray, bbox, k_frames: int) -> dict:
    """对 k 个采样帧算指标，返回汇总报告。"""
    idxs = sample_frames(src, k_frames)
    info = probe_video(src)

    glyph_drops, edge_ratios, sigma_ratios, lap_ratios = [], [], [], []
    residual_abs = []          # 成片字形残留绝对值（前端「残留 <3.0」同口径）
    ring_sigmas, sigma_gaps = [], []   # 环带 σ 与区内-环带 σ 绝对差
    max_clean_in_mask, max_src_in_mask = [], []

    for idx in idxs:
        fs = grab_frame_bgr(src, idx)
        fc = grab_frame_bgr(clean, idx)
        if fs is None or fc is None:
            continue
        g_s = cv2.cvtColor(fs, cv2.COLOR_BGR2GRAY).astype(np.float32)
        g_c = cv2.cvtColor(fc, cv2.COLOR_BGR2GRAY).astype(np.float32)
        m_full = place_mask(mask, fs.shape, bbox)
        r_full = ring_mask(m_full, 11)
        if not m_full.any():
            continue

        # ① glyph_drop（相对背景口径）+ 成片残留绝对值（前端口径）
        if r_full.any():
            ds = float(g_s[m_full].mean()) - float(g_s[r_full].mean())
            dc = float(g_c[m_full].mean()) - float(g_c[r_full].mean())
            if abs(ds) > 1e-6:
                glyph_drops.append((abs(ds) - abs(dc)) / abs(ds) * 100.0)
            residual_abs.append(abs(dc))
            max_clean_in_mask.append(float(g_c[m_full].max()))
            max_src_in_mask.append(float(g_s[m_full].max()))

        # ③ edge_ratio（边界梯度比）
        band = ring_mask(m_full, 2)
        outer = ring_mask(cv2.dilate(m_full.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0, 6)
        if band.any() and outer.any():
            mag_c = cv2.magnitude(cv2.Sobel(g_c, cv2.CV_32F, 1, 0, ksize=3),
                                  cv2.Sobel(g_c, cv2.CV_32F, 0, 1, ksize=3))
            b, o = float(mag_c[band].mean()), float(mag_c[outer].mean())
            if o > 1e-6:
                edge_ratios.append(b / o)

        # ④ sigma_ratio（纹理匹配）+ Laplacian 能量比（坑 44）
        if r_full.any():
            d_c = _detail(g_c)
            s_m, s_r = float(d_c[m_full].std()), float(d_c[r_full].std())
            ring_sigmas.append(s_r)
            sigma_gaps.append(abs(s_m - s_r))
            if s_r > 1e-3:
                sigma_ratios.append(s_m / s_r)
            lap_m = _laplacian_energy(g_c, m_full)
            lap_r = _laplacian_energy(g_c, r_full)
            if lap_r > 1e-3:
                lap_ratios.append(lap_m / lap_r)

    # 帧间闪烁（坑 39）：取 3 个连续 3 帧窗口，比修复区相邻帧差
    flicker = _flicker_p90(src, clean, mask, bbox, info)

    # ★ 平坦背景的 σ 比陷阱（坑 43/44 的延伸）：
    #   周边环带本身是纯色/纯黑场时，它的 detail-σ ≈ 0，此时「σ 比」是
    #   拿 0.02 去比 0.01 —— 数学上有值，物理上毫无意义，还会在帧间乱跳
    #   （同一份成片两次跑出 0.92 和 0.99）。
    #   所以：环带 σ < 1.0 时判「背景平坦」，改用**绝对差**判据
    #   （区内与周边的细节能量绝对差 ≤ 0.8 即视为纹理一致）。
    ring_sigma = _median_or_none(ring_sigmas)
    if ring_sigma is None:
        background = "unknown"
        sigma_gap = None
        sigma_note = "环带无有效采样"
    elif ring_sigma < TH_FLAT_RING_SIGMA:
        background = "flat"
        sigma_gap = _median_or_none(sigma_gaps)
        sigma_note = (f"平坦背景（环σ={ring_sigma:.2f}<{TH_FLAT_RING_SIGMA}），"
                      f"σ比不适用，改判绝对差 {sigma_gap} ≤ {TH_SIGMA_GAP_ABS}")
    else:
        background = "textured"
        sigma_gap = _median_or_none(sigma_gaps)
        sigma_note = f"纹理背景（环σ={ring_sigma:.2f}），按 σ 比判定"

    rep = {
        "samples": len(glyph_drops) or len(sigma_ratios),
        "glyph_drop": {"median": _median_or_none(glyph_drops),
                       "worst": _worst_or_none(glyph_drops, "min")},
        "residual_abs": _median_or_none(residual_abs),
        "region_max": {
            "max_clean": _worst_or_none(max_clean_in_mask, "max"),
            "max_src": _worst_or_none(max_src_in_mask, "max"),
        },
        "edge_ratio": {"median": _median_or_none(edge_ratios),
                       "worst": _worst_or_none(edge_ratios, "max")},
        "sigma_ratio": {"median": _median_or_none(sigma_ratios),
                        "worst": _worst_or_none(sigma_ratios, "min")},
        "ring_sigma": ring_sigma,
        "sigma_gap": sigma_gap,
        "background": background,
        "sigma_note": sigma_note,
        "laplacian_ratio": _median_or_none(lap_ratios),
        "flicker_p90": flicker,
        "video": {"frames": info.frames, "size": f"{info.width}x{info.height}"},
    }
    return rep


def _flicker_p90(src: str, clean: str, mask: np.ndarray, bbox, info) -> float | None:
    """修复区相邻帧差 p90 ÷ **同帧环带的相邻帧差** p90。≈1 = 不闪。

    ★ 为什么不用「÷ 原片水印区地板」（踩坑）：
      原片的水印区是**静止叠加层**，它的帧间差几乎为 0（实测 0.064），
      成片修完后哪怕只有 0.3 的噪声，除以 0.064 也会放大成 4.8 倍
      —— 一个「比值口径」的假警报。
      环带（mask 外 11px）是同一画面的真实运动量，它才是合理的分母：
      成片修复区的帧间差 / 环带的帧间差 ≈ 1 → 说明修复区没有额外闪烁。
    """
    def region_diff(video: str, starts: list[int]) -> tuple[float | None, float | None]:
        inner, outer = [], []
        for s in starts:
            frames = [grab_frame_bgr(video, s + d) for d in (0, 1, 2)]
            if any(f is None for f in frames):
                continue
            grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
                     for f in frames]
            for a, b in zip(grays, grays[1:]):
                m_full = place_mask(mask, a.shape, bbox)
                if not m_full.any():
                    continue
                d = np.abs(a - b)
                inner.append(float(d[m_full].mean()))
                r_full = ring_mask(m_full, 11)
                if r_full.any():
                    outer.append(float(d[r_full].mean()))
        if not inner:
            return None, None
        pi = float(np.percentile(inner, 90))
        po = float(np.percentile(outer, 90)) if outer else None
        return pi, po

    total = info.frames or 0
    if total < 12:
        return None
    starts = [int(total * f) for f in (0.25, 0.6, 0.85)]
    cur_in, cur_out = region_diff(clean, starts)
    if cur_in is None:
        return None
    if cur_out is None or cur_out < 1e-3:
        # 环带完全静止（纯色背景）→ 没有可比的地板。
        # 这种情况用**绝对值**口径：修复区的帧间差本身就是全部信号，
        # 超过 TH_FLICKER_ABS 即视为闪烁（否则纯色片永远"通过"）。
        return round(cur_in, 2)
    # ★ 坑 45：绝对差兜底**只在环带确实接近静止时**生效。
    # 旧逻辑无条件 `if cur_in > 1.0: return max(ratio, 1.6)`，而及格线是 1.5
    # → 任何高运动视频（修复区帧间差天然 >1）必定判 flicker 失败。
    # 实测赛车片环带帧间差 48.6、真实 ratio 0.983（比原片 0.880 还稳），
    # 却被硬报 1.6。环带本身就是同一画面的运动量，它大时用比值口径即可。
    ratio = cur_in / cur_out
    if cur_out < 0.5 and cur_in > TH_FLICKER_ABS:
        return round(max(ratio, TH_FLICKER + 0.1), 2)
    return round(ratio, 2)


def diagnose(rep: dict) -> str:
    """决策树（坑 44）。返回 clean / needs_texture / needs_rowinterp / needs_review。"""
    gd = rep["glyph_drop"]["median"]
    res = rep["residual_abs"]
    mc, ms = rep["region_max"]["max_clean"], rep["region_max"]["max_src"]
    sig = rep["sigma_ratio"]["median"]
    lap = rep["laplacian_ratio"]
    flick = rep["flicker_p90"]
    background = rep.get("background", "unknown")
    sigma_gap = rep.get("sigma_gap")

    # ★ 平坦背景：σ 比无意义（环带 σ≈0），改用绝对差判纹理一致性。
    if background == "flat":
        sigma_ok = sigma_gap is None or sigma_gap <= TH_SIGMA_GAP_ABS
    else:
        sigma_ok = sig is None or (TH_SIGMA_LO <= sig <= TH_SIGMA_HI)

    checks = {
        "glyph_drop": (res is not None and res < TH_RESIDUAL_ABS)
        or (gd is not None and gd >= TH_GLYPH_DROP),
        "region_max": mc is None or ms is None or mc <= ms + 1.0,
        "sigma_ratio": sigma_ok,
        "laplacian": lap is None or lap <= TH_LAPLACIAN,
        "flicker": flick is None or flick <= TH_FLICKER,
    }
    rep["checks"] = checks
    rep["all_green"] = bool(checks) and all(checks.values())

    if rep["all_green"]:
        return "clean"
    # B 类优先（伪纹理/条带是结构问题，补颗粒治不了）
    if lap is not None and lap > TH_LAPLACIAN:
        return "needs_rowinterp"
    # 平坦背景下 σ 比不可用，不因它触发补纹理（否则会发明噪点，坑 44）
    if background != "flat" and sig is not None and sig < TH_SIGMA_LO:
        return "needs_texture"
    if background == "flat" and sigma_gap is not None and sigma_gap > TH_SIGMA_GAP_ABS:
        return "needs_texture"
    return "needs_review"


def main() -> int:
    ap = argparse.ArgumentParser(description="产品版验收（四项指标 + 决策树诊断）")
    ap.add_argument("--original", required=True)
    ap.add_argument("--processed", required=True)
    ap.add_argument("--mask", required=True)
    ap.add_argument("--bbox", required=True, help='"x y w h"')
    ap.add_argument("--out", required=True, help="report.json 输出路径")
    ap.add_argument("--samples", type=int, default=12, help="采样帧数（默认 12）")
    ap.add_argument("--device", default="")             # 契约占位
    args = ap.parse_args()

    for p in (args.original, args.processed, args.mask):
        if not Path(p).exists():
            die(1, f"文件不存在: {p}")

    bbox = parse_bbox(args.bbox)
    if bbox is None:
        die(1, f"--bbox 格式错误: {args.bbox!r}")

    mask = cv2.imread(args.mask, cv2.IMREAD_GRAYSCALE)
    if mask is None:
        die(1, f"mask 读取失败: {args.mask}")

    ok(f"[verify] {args.samples} 帧采样，bbox={args.bbox}")
    rep = verify_pair(args.original, args.processed, mask, bbox, args.samples)
    rep["bbox"] = list(bbox)
    rep["diagnosis"] = diagnose(rep)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2))
    ok(f"[verify] glyph_drop={rep['glyph_drop']['median']} "
       f"sigma={rep['sigma_ratio']['median']} lap={rep['laplacian_ratio']} "
       f"flicker={rep['flicker_p90']} → {rep['diagnosis']}")
    ok(f"报告: {args.out}")
    return 0  # 报告总是成功生成；达不达标由 diagnosis 表达，不靠退出码


if __name__ == "__main__":
    sys.exit(main())

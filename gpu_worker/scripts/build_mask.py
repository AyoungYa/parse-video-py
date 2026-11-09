# -*- coding: utf-8 -*-
"""产品版 build_mask：自动定位 + 精确 mask，一个脚本两件事。

CLI 契约（pipeline.py 的 WM_STAGES_TEMPLATE）：
    python build_mask.py --input 视频 --out mask.png
                         [--bbox "x y w h"]   # 空 = 自动定位
                         [--template 模板.png] # 已知水印模板，跳过统计精修
                         [--frames N] [--device D]

流程：
  1. bbox 给了        → 直接进精修（第 3 步）
  2. bbox 没给        → 全图时域 min 投影自动定位（水印恒亮，坑 43 相对口径），
                        置信度不足 → exit 3（转人工拖框，绝不瞎猜）
  3. 精修：窗口内 min 投影 + 阈值（自动按背景亮度定，坑 43）+ 形态学清理
     + 触边自动扩窗重试（老脚本只告警，产品版必须自己兜住）
  4. 输出 mask.png（bbox 局部尺寸，白=修复区）+ stdout 一行 BBOX_RESULT

铁律来源（video-watermark-removal skill 踩坑手册）：
  * 坑 1：窗口必须覆盖完整水印，触边/窗外内容 = 永远修不到 → 自动扩窗重试
  * 铁律 1：mask 含半透明柔边（min 投影阈值按背景上调，不是死 168）
  * <20px 碎噪连通块丢弃
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wm_common import (  # noqa: E402
    EXIT_FAIL,
    EXIT_LOCATE_LOW_CONFIDENCE,
    EXIT_OK,
    bbox_str,
    die,
    emit_bbox,
    ok,
    parse_bbox,
)

# 复用项目里久经考验的抽帧（含 1GiB 内存分流守卫）与快速建 mask 判据
from parse_video_py.wm_studio.video import grab_frames_many, probe_video  # noqa: E402

# ---------------------------------------------------------------------------
# 自动定位的判据阈值（实测标定，改前先看 auto_locate 的注释）
# ---------------------------------------------------------------------------
LOCATE_WHITE_TH = 225      # 旧版绝对亮度阈值（保留供参考，新逻辑用 score）
LOCATE_MIN_AREA = 60       # 连通块最小像素数（碎噪）
LOCATE_BAND = 260          # 位置恒定性取样：同高度左右各取多少 px 做对照
LOCATE_STD_RATIO = 1.05    # 区内 std ÷ 周边 std 上限。
    #   实测：水印核心块 0.70~0.80，但**水印的次要块**（文字笔画小、
    #   周围有内容）可达 0.84~0.99；人像高光 0.91~1.58。
    #   0.80 太紧会把真水印的次要块滤掉（只抓到 2 块 / 宽度 105px）。
    #   0.91 会放进人像块 —— 所以取 1.05 保住水印全组，
    #   再靠「水平块簇」判据把画面内容排除掉。
LOCATE_CLUSTER_GAP = 260   # 同一水印的 logo/文字块水平聚类间距（实测水印跨 388px）
LOCATE_SOLO_MAX_AREA = 8000  # 单块上限：超过它更像画面内容而非水印（实测巨块 42694px）

# —— 联合评分（恒亮度 + 低波动）——
LOCATE_STD_PENALTY = 2.0   # 时域标准差的惩罚系数（波动大 → 扣分）
LOCATE_SCORE_MIN = 120.0   # score 绝对下限，低于它不认为是水印
LOCATE_SCORE_MARGIN = 60.0  # 从全图 p99.5 往下让多少（自适应阈值）
LOCATE_DARK_FRAME_MEAN = 20.0  # 帧均亮度低于此值算暗帧（黑场/淡出），定位时剔除
LOCATE_EDGE_GROW = 80.0     # 触边时扩窗的像素数（只扩一次，避免死循环）

# —— 模板通道（坑 45/46 之后引入）——
# 半透明 / 深色描边水印的统计判据不可靠：wm_corpus 实测窗口内
# |min投影 − 局部背景| 的「水印区÷环带」只有 0.80~1.80（多数 <1），
# 时域 std 比同样分不开。固定样式的平台水印应直接用模板 mask。
TEMPLATE_ALPHA_TH = 0.15      # 模板归一化后 > 此值算水印（含柔边）
LOCATE_EXPAND_MAX_RATIO = 6.0  # 精修 mask / 定位初始块 面积上限，超了 = 定位到画面内容


# ---------------------------------------------------------------------------
# 1. 自动定位（全图 min 投影）
# ---------------------------------------------------------------------------

def _grab_gray_stack(video: str, n_frames: int) -> list[np.ndarray]:
    """全图抽 n 帧灰度。内存由 grab_frames_many 的分流守卫兜底。"""
    info = probe_video(video)
    if not info.width or not info.height or not info.frames:
        die(EXIT_FAIL, f"读不到视频信息：{video}")
    total = info.frames
    idxs = np.unique(
        np.linspace(
            int(min(total - 1, max(0, 1.0 * info.fps))),          # 避开片头
            int(max(1, total - 1 - 1.5 * info.fps)),               # 避开片尾
            n_frames,
        ).astype(int)
    )
    per_frame = info.width * info.height * 3
    got = grab_frames_many(video, idxs.tolist(), max_bytes=max(per_frame, n_frames * per_frame))
    if len(got) < 8:
        die(EXIT_FAIL, f"抽帧不足（{len(got)}/{n_frames}），文件可能损坏")
    grays = [cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY) for _, fr in got]
    # ★ 剔除暗帧（黑场/片尾淡出）。实测 54s 样片片尾有 6 帧均值 1.3~2.5，
    #   留着会让时域分位数被拉到 0，水印特征彻底消失（定位直接失败）。
    bright = [g for g in grays if float(g.mean()) > LOCATE_DARK_FRAME_MEAN]
    if len(bright) >= 8:
        grays = bright
    return grays


def _candidate_blocks(score: np.ndarray, W: int, H: int) -> list[dict]:
    """从评分图里提取候选块（连通块 + 尺寸先验）。"""
    hi = float(np.percentile(score, 99.5))
    th = float(max(LOCATE_SCORE_MIN, hi - LOCATE_SCORE_MARGIN))
    cand = (score > th).astype(np.uint8)
    cand = cv2.morphologyEx(cand, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n_lab, _, stats, _ = cv2.connectedComponentsWithStats(cand, 8)
    blocks = []
    for i in range(1, n_lab):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < LOCATE_MIN_AREA:
            continue
        bx, by = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
        bw, bh = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
        # 尺寸下限：文字核心不可能太窄太小（skill 的 _MIN_W/_MIN_H）
        if bw < 24 or bh < 12:
            continue
        # 尺寸上限：排除整块静止画面（skill 的 _MAX_W_FRAC/_MAX_H_FRAC）
        if bw > W * 0.40 or bh > H * 0.25:
            continue
        blocks.append({"area": area, "x": bx, "y": by, "w": bw, "h": bh})
    return blocks, th


def _std_ratio(stdev: np.ndarray, b: dict, W: int) -> float:
    """位置恒定性：区内 std ÷ 同高度左右带 std。"""
    r_std = float(stdev[b["y"]:b["y"] + b["h"], b["x"]:b["x"] + b["w"]].mean())
    side_parts = []
    x0, x1 = b["x"], b["x"] + b["w"]
    if x0 - LOCATE_BAND > 0:
        side_parts.append(stdev[b["y"]:b["y"] + b["h"],
                                 max(0, x0 - LOCATE_BAND):max(0, x0 - 8)])
    if x1 + LOCATE_BAND < W:
        side_parts.append(stdev[b["y"]:b["y"] + b["h"],
                                 min(W, x1 + 8):min(W, x1 + LOCATE_BAND)])
    side = np.concatenate([p for p in side_parts if p.size]) if side_parts else np.array([])
    s_std = float(side.mean()) if side.size else 0.0
    return (r_std / s_std) if s_std > 1e-3 else 99.0


def _cluster_blocks(blocks: list[dict]) -> list[list[dict]]:
    """按水平相邻把块聚成组（一个水印 = 一组水平排列的小块）。

    ★ 只看水平相邻，**不要求 y 区间重叠**：
      微博水印是「上行 logo+标题 + 下行 @账号」两行，y 区间完全不重叠
      （实测 y30-60 与 y85-115）。要求 y 重叠会把它们分成两组，
      只取一组 → 水印右侧被切掉 90px（实测成片右侧残留）。
    """
    ordered = sorted(blocks, key=lambda b: b["x"])
    groups: list[list[dict]] = []
    for b in ordered:
        placed = False
        for g in groups:
            last = g[-1]
            gap_x = b["x"] - (last["x"] + last["w"])
            if gap_x < LOCATE_CLUSTER_GAP:
                g.append(b)
                placed = True
                break
        if not placed:
            groups.append([b])
    return groups


def _pick_cluster(groups: list[list[dict]]) -> tuple[list[dict], str]:
    """从块组里挑最像水印的一组。返回 (组, 拒因)。"""
    multi = [g for g in groups if len(g) >= 2]
    if multi:
        multi.sort(key=lambda g: (len(g), sum(b["area"] for b in g)), reverse=True)
        return multi[0], ""
    if groups:
        solo = max(groups, key=lambda g: sum(b["area"] for b in g))
        total = sum(b["area"] for b in solo)
        if total > LOCATE_SOLO_MAX_AREA:
            return [], f"只找到单个恒亮块（{total}px），像画面内容不像水印"
        return solo, ""
    return [], "无候选块"


def auto_locate(video: str, n_frames: int = 40) -> tuple[tuple[int, int, int, int], dict]:
    """全图自动找水印（两条通道并行，取更可信的结果）。

    ★ 为什么必须两条通道（实测踩坑，两个样片各暴露一种失效）：
      通道 A「绝对亮度」：min 投影 > 高阈值。
        纯白水印（微博磨砂卡片）min 投影可达 246 → 抓得到。
        但**半透明水印**（微博视频号 logo，60% 不透明度）最高只有 ~217
        → 硬阈值直接漏掉。
      通道 B「值高 + 低波动」：score = 时域 p90 − 2×时域 std。
        半透明水印能抓到；
        但**大片静态高亮区**（舞台背景，实测 506520px）会把相对分数抬高，
        反而挤掉真水印。

    所以：两条通道各出一份候选，都过「位置恒定 + 水平块簇」检验，
    **选块数更多 / 更靠角落的那个**。
    """
    stack = _grab_gray_stack(video, n_frames)
    arr = np.stack(stack).astype(np.float32)
    lo = np.percentile(arr, 1, axis=0)
    stdev = arr.std(axis=0)
    p90 = np.percentile(arr, 90, axis=0)
    H, W = lo.shape

    channels = []
    # 通道 A：绝对亮度（抓纯白水印）
    hi_a = float(np.percentile(lo, 99.5))
    th_a = float(max(LOCATE_WHITE_TH, min(250.0, hi_a - 8)))
    channels.append(("absolute", lo > th_a, th_a))
    # 通道 B：值高 + 低波动（抓半透明水印）
    score = p90 - stdev * LOCATE_STD_PENALTY
    hi_b = float(np.percentile(score, 99.5))
    th_b = float(max(LOCATE_SCORE_MIN, hi_b - LOCATE_SCORE_MARGIN))
    channels.append(("score", score > th_b, th_b))

    best = None
    reasons = []
    all_candidates: list[dict] = []   # 所有通道的候选（转人工时当默认框给用户）
    for name, mask_img, th in channels:
        cand = mask_img.astype(np.uint8)
        cand = cv2.morphologyEx(cand, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n_lab, _, stats, _ = cv2.connectedComponentsWithStats(cand, 8)
        blocks = []
        for i in range(1, n_lab):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < LOCATE_MIN_AREA:
                continue
            bx, by = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
            bw, bh = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
            if bw < 24 or bh < 12:
                continue
            if bw > W * 0.40 or bh > H * 0.25:
                continue
            blocks.append({"area": area, "x": bx, "y": by, "w": bw, "h": bh})
        if not blocks:
            reasons.append(f"{name}: 无候选块")
            continue
        for b in blocks:
            b["std_ratio"] = _std_ratio(stdev, b, W)
            # 记下候选（转人工时给用户当默认框，省得从零画）
            all_candidates.append(
                {"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"],
                 "area": b["area"], "std_ratio": round(b["std_ratio"], 2), "channel": name}
            )
        stable = [b for b in blocks if b["std_ratio"] < LOCATE_STD_RATIO]
        if not stable:
            bestr = sorted(blocks, key=lambda b: b["std_ratio"])[:3]
            reasons.append(f"{name}: 恒亮块不满足位置恒定性（"
                           + "/".join(f"{b['std_ratio']:.2f}" for b in bestr) + "）")
            continue
        cluster, why = _pick_cluster(_cluster_blocks(stable))
        if not cluster:
            reasons.append(f"{name}: {why}")
            continue
        # 打分：块数越多越像水印；再叠加角落先验
        n_blocks = len(cluster)
        area = sum(b["area"] for b in cluster)
        x0 = min(b["x"] for b in cluster)
        y0 = min(b["y"] for b in cluster)
        x1 = max(b["x"] + b["w"] for b in cluster)
        y1 = max(b["y"] + b["h"] for b in cluster)
        edge_dist = min(x0, y0, W - x1, H - y1)
        corner_bonus = 1.0 if (x0 > W * 0.5 or x1 < W * 0.5) and (y0 > H * 0.5 or y1 < H * 0.5) else 0.0
        score_rank = n_blocks * 2 + (1 if corner_bonus else 0) + min(area / 2000.0, 3.0)
        cand_result = {"name": name, "cluster": cluster, "score": score_rank,
                        "bbox": (x0, y0, x1 - x0, y1 - y0), "area": area,
                        "th": th, "edge_dist": edge_dist,
                        "std_ratio": min(b["std_ratio"] for b in cluster)}
        if best is None or cand_result["score"] > best["score"]:
            best = cand_result

    if best is None:
        return (0, 0, 0, 0), {"confidence": "low",
                              "reason": "；".join(reasons) or "未找到任何水印候选",
                              "candidates": _top_candidates(all_candidates)}

    cluster = best["cluster"]
    x0 = min(b["x"] for b in cluster)
    y0 = min(b["y"] for b in cluster)
    x1 = max(b["x"] + b["w"] for b in cluster)
    y1 = max(b["y"] + b["h"] for b in cluster)
    area = sum(b["area"] for b in cluster)
    margin = 14                                     # 抗锯齿 + 扩窗余量
    bx = max(0, x0 - margin)
    by = max(0, y0 - margin)
    bw = min(W, x1 + margin) - bx
    bh = min(H, y1 + margin) - by

    detail = {
        "confidence": "ok",
        "channel": best["name"],
        "white_th": int(best["th"]),
        "blocks": len(cluster),
        "mask_px": area,
        "std_ratio": round(best["std_ratio"], 2),
        "edge_dist": best["edge_dist"],
        "bbox_area_ratio": round(bw * bh / (W * H), 4),
        "candidates": _top_candidates(all_candidates),
    }
    return (bx, by, bw, bh), detail


def _top_candidates(candidates: list[dict], limit: int = 5) -> list[dict]:
    """挑最像水印的几个候选框（给用户当默认框）。

    排序依据：位置恒定性越好越可信 → 面积适中 → 离画面边缘近（平台水印在角落）。
    """
    if not candidates:
        return []

    def rank(c: dict) -> tuple:
        ratio = c.get("std_ratio", 99.0)
        area = c.get("area", 0)
        # std 比越小越好；面积在 200~8000 之间最像水印；面积太小或太大都降权
        area_penalty = 0 if 200 <= area <= 8000 else 1
        return (ratio > LOCATE_STD_RATIO, area_penalty, ratio, -area)

    picked = sorted(candidates, key=rank)[:limit]
    return [
        {"x": int(c["x"]), "y": int(c["y"]), "w": int(c["w"]), "h": int(c["h"]),
         "std_ratio": c.get("std_ratio")}
        for c in picked
    ]


def _locate_sanity(bbox: tuple[int, int, int, int], detail: dict, video: str) -> str | None:
    """定位结果的健全性检查。返回 None = 通过，否则返回转人工的原因。"""
    _, _, w, h = bbox
    info = probe_video(video)
    ratio = detail.get("bbox_area_ratio") or 0.0
    if ratio > 0.20:
        return f"疑似窗口过大（占画面 {ratio:.0%}）——可能是高光/静态物体误判"
    if ratio < 0.0005:
        return "疑似窗口过小，水印可能只是噪点"
    if detail.get("blocks", 0) > 12:
        return f"检出 {detail['blocks']} 个分散块，水印应是一个视觉组"
    if w < 24 or h < 12:
        return "窗口尺寸不像水印（太小）"
    if w > info.width * 0.9 or h > info.height * 0.9:
        return "窗口几乎覆盖全画面，误判成整片亮区"
    return None


# ---------------------------------------------------------------------------
# 2. 精修（窗口内建 mask，触边自动扩窗）
# ---------------------------------------------------------------------------

def _mask_in_window(video: str, x: int, y: int, w: int, h: int,
                    frames: int, channel: str = "absolute"
                    ) -> tuple[np.ndarray | None, float, float]:
    """在窗口内算精确 mask。返回 (mask, 阈值, 背景中位)。mask 为 None = 空。

    ★ channel 决定阈值口径（与 auto_locate 的两条通道对应）：
      "absolute"：min 投影 > 高阈值 → 抓纯白水印（微博磨砂卡片）
      "score"：p90 − 2×std > 阈值 → 抓半透明水印（微博视频号 logo）
      窗口内不能用「自适应高分位」——扩窗后窗口里混进了大片背景高光，
      自适应阈值会随窗口大小漂移（实测第一片扩窗后 mask 涨到 114699px）。
      所以绝对通道用**全局固定阈值**，score 通道用绝对下限。
    """
    stack = _grab_window_stack(video, x, y, w, h, frames)
    arr = np.stack(stack).astype(np.float32)
    lo = np.percentile(arr, 1, axis=0)
    stdev = arr.std(axis=0)
    p90 = np.percentile(arr, 90, axis=0)
    score = p90 - stdev * LOCATE_STD_PENALTY
    bg = float(np.median(lo))

    if channel == "absolute":
        hi_a = float(np.percentile(lo, 99.5))
        white_th = float(max(LOCATE_WHITE_TH, min(250.0, hi_a - 8)))
        sel = lo > white_th
    else:
        white_th = float(LOCATE_SCORE_MIN)
        sel = score > white_th

    m = sel.astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), 1)     # 盖抗锯齿边（铁律 1）
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    ncc, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros_like(m)
    for i in range(1, ncc):
        if stats[i, cv2.CC_STAT_AREA] >= 20:            # 碎噪（铁律 1）
            keep[labels == i] = 1
    m = keep
    return m, white_th, bg


def build_precise(video: str, bbox: tuple[int, int, int, int],
                  out_path: Path, frames: int = 48,
                  channel: str = "absolute") -> dict:
    """在窗口内建精确 mask。触边时自动扩窗**一次**并重算。

    ★ 触边判定要区分「窗口切边」与「水印本来就在画面边缘」：
      微博水印就在右上角，内容必然贴画面上/右边界。
      只有当**该方向还能往外扩**（窗口没贴到画面边界）时，
      触边才意味着「水印被切在窗口外」。
    ★ 扩窗只做一次：否则水印横跨窗口时会一路扩到画面边缘 → 永远触边
      → 死循环 → 转人工（实测第二片 3 次扩窗仍触边，产品上是失败）。
    ★ 扩窗后必须**在新窗口里重算 mask**：否则 mask 内容还是旧窗口坐标，
      与新 bbox 错位（mask 尺寸对不上 bbox → lama_apply 贴错位置）。
    """
    info = probe_video(video)
    W, H = info.width, info.height
    x, y, w, h = (int(v) for v in bbox)

    m, white_th, bg = _mask_in_window(video, x, y, w, h, frames, channel)
    if m is None or m.sum() == 0:
        return {"ok": False, "error": f"mask 为空（阈值 {white_th:.0f}，背景 {bg:.0f}）"}

    ys, xs = np.where(m > 0)
    touch = (int(xs.min()) < 8 and x > 0,
             w - 1 - int(xs.max()) < 8 and x + w < W,
             int(ys.min()) < 8 and y > 0,
             h - 1 - int(ys.max()) < 8 and y + h < H)
    if any(touch):
        grow = int(LOCATE_EDGE_GROW)
        nx0 = max(0, x - grow) if touch[0] else x
        nx1 = min(W, x + w + grow) if touch[1] else x + w
        ny0 = max(0, y - grow) if touch[2] else y
        ny1 = min(H, y + h + grow) if touch[3] else y + h
        x, y, w, h = nx0, ny0, nx1 - nx0, ny1 - ny0
        ok(f"  内容触边，扩窗一次 → {bbox_str((x, y, w, h))}")
        m, white_th, bg = _mask_in_window(video, x, y, w, h, frames, channel)
        if m is None or m.sum() == 0:
            return {"ok": False, "error": f"扩窗后 mask 为空（阈值 {white_th:.0f}）"}
        ys, xs = np.where(m > 0)

    # 收紧 bbox 到内容范围 + 小余量（LaMa ROI 越小越快）。
    # mask 同步摆进新 bbox 画布：新 bbox 左上角相对旧窗口的偏移要算清楚，
    # 否则 mask 尺寸与 bbox 对不上（lama/verify 都按 bbox 摆 mask）。
    pad = 8
    cx0 = max(0, x + int(xs.min()) - pad)
    cy0 = max(0, y + int(ys.min()) - pad)
    cx1 = min(W, x + int(xs.max()) + 1 + pad)
    cy1 = min(H, y + int(ys.max()) + 1 + pad)
    nb = (cx0, cy0, cx1 - cx0, cy1 - cy0)
    sub = m[int(ys.min()):int(ys.max()) + 1, int(xs.min()):int(xs.max()) + 1]
    canvas = np.zeros((nb[3], nb[2]), np.uint8)
    oy, ox = y + int(ys.min()) - cy0, x + int(xs.min()) - cx0
    canvas[oy:oy + sub.shape[0], ox:ox + sub.shape[1]] = sub

    cv2.imwrite(str(out_path), canvas * 255)
    return {"ok": True, "bbox": nb, "mask_px": int(canvas.sum()),
            "white_th": white_th, "bg_median": round(bg, 1)}


def build_from_template(video: str, bbox: tuple[int, int, int, int],
                        out_path: Path, template_path: Path,
                        soft_dilate: int = 1) -> dict:
    """用已知水印模板生成 mask，跳过统计精修（返回结构与 build_precise 一致）。

    ★ 为什么需要：半透明 / 深色描边水印在窗口内的「时域 min 投影、
      |局部偏差|、时域 std」三类统计判据都分不开水印与背景
      （wm_corpus 15 片实测 dev 比 0.80~1.80，多数 <1）。固定样式的平台水印
      （Shutterstock / 微博 / Sora logo）用模板 mask 才可靠。
      与 skill 里「微博横屏复用 mask_weibo_1080p_landscape.png」同思路。
    ★ 模板会被 resize 到 bbox 的 (w, h) 再按 bbox 摆到全图，
      支持 PNG（灰度 0~255）与 EXR（float alpha 0~1，RGB 三通道是同一份图案）。
    ★ 柔边：二值化后 dilate 1px 盖抗锯齿边（铁律 1）；lama_apply 内部
      还有 3px GaussianBlur 羽化，所以这里别过度膨胀（坑 37：实心大 mask
      会把细节比 0.99 测成 0.59）。
    """
    info = probe_video(video)
    W, H = info.width, info.height
    x, y, w, h = (int(v) for v in bbox)
    x, y = max(0, x), max(0, y)
    w, h = min(w, W - x), min(h, H - y)
    if w <= 0 or h <= 0:
        return {"ok": False, "error": "bbox 超出画面"}

    os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
    tpl = cv2.imread(str(template_path), cv2.IMREAD_UNCHANGED)
    if tpl is None:
        return {"ok": False, "error": f"模板读取失败: {template_path}"}
    if tpl.ndim == 3:
        tpl = tpl[:, :, :3].mean(axis=2)   # EXR 第 4 通道是附加 alpha，不混进来
    tf = tpl.astype(np.float32)
    if tf.max() > 1.5:                    # PNG 0~255 → 归一化
        tf /= 255.0
    tf = cv2.resize(tf, (w, h), interpolation=cv2.INTER_AREA)
    m = (tf > TEMPLATE_ALPHA_TH).astype(np.uint8)
    if soft_dilate:
        m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=soft_dilate)
    if not m.any():
        return {"ok": False, "error": "模板二值化后 mask 为空（阈值过高？）"}

    cv2.imwrite(str(out_path), m * 255)
    return {"ok": True, "bbox": (x, y, w, h), "mask_px": int(m.sum()),
            "white_th": TEMPLATE_ALPHA_TH, "bg_median": 0.0,
            "source": "template"}


def _grab_window_stack(video: str, x: int, y: int, w: int, h: int,
                       n_frames: int) -> np.ndarray:
    """窗口内抽 n 帧灰度栈 (n, h, w)。"""
    info = probe_video(video)
    total = info.frames
    idxs = np.unique(np.linspace(int(1.0 * info.fps), max(1, total - 2), n_frames).astype(int))
    per_frame = info.width * info.height * 3
    got = grab_frames_many(video, idxs.tolist(), max_bytes=max(per_frame, n_frames * per_frame))
    if len(got) < 8:
        die(EXIT_FAIL, f"窗口抽帧不足（{len(got)}）")
    frames = [cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)[y:y + h, x:x + w].astype(np.float32)
              for _, fr in got]
    # 同 auto_locate：剔除暗帧，避免片尾黑场把时域统计拉平
    bright = [f for f in frames if float(f.mean()) > LOCATE_DARK_FRAME_MEAN]
    if len(bright) >= 8:
        frames = bright
    return np.stack(frames)


# ---------------------------------------------------------------------------

def _emit_low_confidence(reason: str, candidates: list[dict]) -> None:
    """输出低置信度结论 + 候选框（供 pipeline 解析后上报云端，让用户微调）。

    ★ 格式契约（pipeline.py 按行首前缀解析）：
        LOW_CONFIDENCE: <原因>
        LOCATE_CANDIDATES: [{"x":..,"y":..,"w":..,"h":..}, ...]
    """
    print(f"LOW_CONFIDENCE: {reason}", flush=True)
    if candidates:
        import json

        print("LOCATE_CANDIDATES: " + json.dumps(candidates, ensure_ascii=False), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="产品版 build_mask：自动定位 + 精确 mask")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bbox", default="", help='"x y w h"，空 = 自动定位')
    ap.add_argument("--device", default="")                # 契约占位（本脚本纯 CPU）
    ap.add_argument("--frames", type=int, default=48, help="抽样帧数")
    ap.add_argument("--locate-frames", type=int, default=40, help="自动定位抽样帧数")
    ap.add_argument("--template", default="",
                    help="已知水印模板 PNG/EXR；给定时跳过统计精修直接生成 mask")
    args = ap.parse_args()

    video = args.input
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    bbox = parse_bbox(args.bbox)
    detail: dict = {}
    candidates: list[dict] = []
    if bbox is None:
        ok("[1/3] 自动定位（全图时域 min 投影）...")
        bbox, detail = auto_locate(video, args.locate_frames)
        candidates = detail.get("candidates") or []
        if detail.get("confidence") != "ok":
            _emit_low_confidence(detail.get("reason", "定位不可信"), candidates)
            return EXIT_LOCATE_LOW_CONFIDENCE
        reason = _locate_sanity(bbox, detail, video)
        if reason:
            _emit_low_confidence(reason, candidates)
            return EXIT_LOCATE_LOW_CONFIDENCE
        ok(f"  定位 bbox: {bbox_str(bbox)}  "
           f"({detail['blocks']} 块, {detail['mask_px']}px, 阈值 {detail['white_th']})")
    else:
        ok(f"[1/3] 使用云端 bbox: {bbox_str(bbox)}")

    if args.template:
        ok(f"[2/3] 模板通道生成 mask（{args.template}）...")
        rep = build_from_template(video, bbox, out_path, Path(args.template))
    else:
        ok("[2/3] 窗口内精修 mask ...")
        rep = build_precise(video, bbox, out_path, frames=args.frames,
                            channel=detail.get("channel", "absolute"))
    if not rep.get("ok"):
        hint = "" if args.template else "；半透明水印可传 --template 用已知模板"
        _emit_low_confidence(rep.get("error", "精修失败") + hint, candidates)
        return EXIT_LOCATE_LOW_CONFIDENCE

    # ★ 坑 46：自动定位给出的初始块很小，精修却膨胀几十倍 = 定位到了画面内容。
    # 实测 cactus 片 403px → 19199px（47×），红框框住的是仙人掌花朵而非水印，
    # 旧版 rc=0 照常交付 mask。这种必须转人工，不能喂给 LaMa。
    seed = detail.get("mask_px") or 0
    if seed > 0 and rep["mask_px"] > seed * LOCATE_EXPAND_MAX_RATIO:
        _emit_low_confidence(
            f"精修 mask 相对定位初始块膨胀 {rep['mask_px'] / seed:.1f}× "
            f"（{seed} → {rep['mask_px']}px），疑似定位到画面内容",
            candidates)
        return EXIT_LOCATE_LOW_CONFIDENCE

    final_bbox = rep["bbox"]
    src_note = ", 来源 模板" if rep.get("source") == "template" else ""
    ok(f"[3/3] mask 写出 {out_path.name}: {rep['mask_px']}px "
       f"(阈值 {rep['white_th']}, 背景 {rep['bg_median']}{src_note})")
    emit_bbox(final_bbox)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

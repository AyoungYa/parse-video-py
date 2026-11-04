# -*- coding: utf-8 -*-
"""验收指标：把「有没有痕」变成可读数字。

指标来源 = skill 的 `verify.py` 三件套 + 踩坑手册 42/43 新增的两项。
但**取样方式全部改成本地单帧或短段**，不跑全片的时域中值（那要抽 90 帧），
所以叫「快速口径」——用于调参时即时反馈，最终交付仍建议跑 `verify.py`。

五项：
  ① 笔画对比度降幅  —— 模板位 − mask 内背景参照（坑 43 的相对口径）
  ② 修复区 max      —— 防"发明亮块"（成片 max 应 ≤ 原片同区 max）
  ③ 边界梯度比      —— 防接缝（1.0 = 无接缝；> 2 = 看得见框）
  ④ 纹理匹配 σ 比   —— 防发糊/伪纹理（0.8~1.5 为可接受带）
  ⑤ 帧对齐          —— 仅整片成片校验时用，预览阶段不适用

★ 坑 43 的教训：亮背景片若用「> 150 像素数」当残留判据，会把整片背景
  算成残留，报出"降幅 15%"的假警报。所以 ① 一律用**相对背景**的口径。
"""

from dataclasses import asdict, dataclass, field

import cv2
import numpy as np


@dataclass
class Metric:
    key: str
    label: str
    value: float | None
    unit: str
    ok: bool | None
    target: str
    note: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class MetricsReport:
    items: list[Metric] = field(default_factory=list)
    passed: int = 0
    total: int = 0

    def to_dict(self):
        return {
            "items": [m.to_dict() for m in self.items],
            "passed": self.passed,
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def place_mask(mask: np.ndarray, frame_shape, bbox) -> np.ndarray:
    """把 bbox 局部 mask（h,w）放到整帧画布上的布尔图。"""
    x, y, w, h = (int(v) for v in bbox)
    H, W = frame_shape[:2]
    full = np.zeros((H, W), bool)
    m = np.asarray(mask)
    if m.ndim == 3:
        m = m[..., 0]
    if m.shape != (h, w):
        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    sx0, sy0 = x0 - x, y0 - y
    if x1 > x0 and y1 > y0:
        full[y0:y1, x0:x1] = m[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)] > 127
    return full


def ring_mask(mask_full: np.ndarray, width: int = 11) -> np.ndarray:
    """mask 外侧的环形取样带（用于取背景参照）。"""
    k = np.ones((width * 2 + 1, width * 2 + 1), np.uint8)
    dil = cv2.dilate(mask_full.astype(np.uint8), k) > 0
    return dil & (~mask_full)


def _sobel_mag(gray_f: np.ndarray) -> np.ndarray:
    gx = cv2.Sobel(gray_f, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_f, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def _detail(gray_f: np.ndarray, sigma: float = 1.2) -> np.ndarray:
    """高频细节层 = 灰度 − 高斯模糊（同 texture_fix.py 的 layer_sigma）。"""
    return gray_f - cv2.GaussianBlur(gray_f, (0, 0), sigma)


# ---------------------------------------------------------------------------
# 五项指标
# ---------------------------------------------------------------------------


def glyph_delta(
    gray: np.ndarray, mask_full: np.ndarray, ring_full: np.ndarray
) -> tuple[float, float] | None:
    """① 相对口径：模板位均值 − mask 内背景参照均值。

    mask_full 是**笔画**模板（稀疏），这里用 ring 作背景参照
    （mask 内非笔画区域在坑 43 里叫 bgref；单帧下环带的背景更稳，
    因为亮背景下 mask 内的透明卡片区本身就接近背景）。
    返回 (delta, bg_mean)。
    """
    if not mask_full.any():
        return None
    t = float(gray[mask_full].mean())
    if ring_full.any():
        b = float(gray[ring_full].mean())
    else:
        b = float(np.median(gray))
    return t - b, b


def compute_metrics(
    src_frame: np.ndarray,
    clean_frame: np.ndarray,
    sparse_mask: np.ndarray,
    bbox,
    ring_px: int = 11,
) -> MetricsReport:
    """对**同一帧**的原片 / 成片算指标 ①~④。"""
    g_s = cv2.cvtColor(src_frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    g_c = cv2.cvtColor(clean_frame, cv2.COLOR_BGR2GRAY).astype(np.float32)

    m_full = place_mask(sparse_mask, src_frame.shape, bbox)
    r_full = ring_mask(m_full, ring_px)
    if not r_full.any():
        r_full = np.zeros_like(m_full)

    rep = MetricsReport()

    # ① 笔画对比度降幅
    ds = glyph_delta(g_s, m_full, r_full)
    dc = glyph_delta(g_c, m_full, r_full)
    if ds and dc:
        d_src, d_cln = ds[0], dc[0]
        if abs(d_src) > 1e-6:
            drop = (abs(d_src) - abs(d_cln)) / abs(d_src) * 100.0
        else:
            drop = 0.0
        rep.items.append(
            Metric(
                key="glyph_drop",
                label="笔画残留降幅",
                value=round(drop, 1),
                unit="%",
                ok=drop >= 90.0,
                target="≥ 90%",
                note=f"原片 Δ{d_src:+.1f} → 成片 Δ{d_cln:+.1f}（相对背景参照）",
            )
        )
    else:
        rep.items.append(
            Metric(
                key="glyph_drop",
                label="笔画残留降幅",
                value=None,
                unit="%",
                ok=None,
                target="≥ 90%",
                note="mask 为空或背景参照缺失",
            )
        )

    # ② 修复区 max（防发明亮块）
    if m_full.any():
        mx_s = float(g_s[m_full].max())
        mx_c = float(g_c[m_full].max())
        rep.items.append(
            Metric(
                key="region_max",
                label="修复区最亮值",
                value=round(mx_c, 1),
                unit="",
                ok=mx_c <= mx_s + 1.0,
                target=f"≤ 原片 {mx_s:.0f}",
                note=f"原片 {mx_s:.0f} → 成片 {mx_c:.0f}；超过原片=LaMa 发明了亮块",
            )
        )

    # ③ 边界梯度比（防接缝）
    band = ring_mask(m_full, 2)
    outer = ring_mask(
        cv2.dilate(m_full.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0, 6
    )
    if band.any() and outer.any():
        mag_c = _sobel_mag(g_c)
        mag_s = _sobel_mag(g_s)
        b_c = float(mag_c[band].mean())
        o_c = float(mag_c[outer].mean())
        ratio = b_c / o_c if o_c > 1e-6 else 0.0
        b_s = float(mag_s[band].mean())
        o_s = float(mag_s[outer].mean())
        ratio_src = b_s / o_s if o_s > 1e-6 else 0.0
        rep.items.append(
            Metric(
                key="edge_ratio",
                label="边界梯度比",
                value=round(ratio, 2),
                unit="",
                ok=ratio <= 1.5,
                target="≤ 1.5（理想 ≈ 1.0）",
                note=f"原片 {ratio_src:.2f} → 成片 {ratio:.2f}；> 2 会看到明显方框接缝",
            )
        )

    # ④ 纹理匹配 σ 比（防发糊 / 伪纹理）
    if m_full.any() and r_full.any():
        d_c = _detail(g_c)
        s_m = float(d_c[m_full].std())
        s_r = float(d_c[r_full].std())
        if s_r > 1e-3:
            ratio = s_m / s_r
            rep.items.append(
                Metric(
                    key="sigma_ratio",
                    label="纹理匹配 σ 比",
                    value=round(ratio, 2),
                    unit="",
                    ok=0.8 <= ratio <= 1.6,
                    target="0.8 ~ 1.6（1.0 最佳）",
                    note=f"区 {s_m:.2f} / 环 {s_r:.2f}；< 0.8 发糊需 texture_fix，"
                    f"> 1.6 疑伪纹理",
                )
            )
        else:
            rep.items.append(
                Metric(
                    key="sigma_ratio",
                    label="纹理匹配 σ 比",
                    value=None,
                    unit="",
                    ok=None,
                    target="0.8 ~ 1.6",
                    note="周边是平坦面（σ≈0），此项不适用",
                )
            )

    rep.total = len(rep.items)
    rep.passed = sum(1 for m in rep.items if m.ok is True)
    return rep

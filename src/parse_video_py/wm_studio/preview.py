# -*- coding: utf-8 -*-
"""单帧预览管线：抽帧 → mask → LaMa → 指标，一次性出结果。"""

import time

import cv2
import numpy as np

from parse_video_py.wm_studio.engine import LamaEngine
from parse_video_py.wm_studio.mask_ops import card_mask_from_sparse
from parse_video_py.wm_studio.metrics import compute_metrics
from parse_video_py.wm_studio.video import grab_frame_bgr, probe_video


def load_mask_png(path: str) -> np.ndarray | None:
    if not path:
        return None
    m = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    return m


def resolve_mask(mask: np.ndarray | None, strategy: str, bbox) -> np.ndarray | None:
    """按策略返回送进引擎的 mask。"""
    if mask is None:
        return None
    if strategy == "card":
        return card_mask_from_sparse(mask)
    return mask


def preview_frame(
    video_path: str,
    bbox,
    frame_idx: int = 0,
    mask: np.ndarray | None = None,
    mask_strategy: str = "card",
    pad: int = 10,
    device: str = "auto",
    blend_mode: str = "mask",
    compute_metric: bool = True,
) -> dict:
    """修复单帧并返回结果（帧图 + 指标 + 耗时）。"""
    t_all = time.time()

    info = probe_video(video_path)
    src = grab_frame_bgr(video_path, frame_idx)
    if src is None:
        raise ValueError(f"取帧失败：帧号 {frame_idx}（总帧数 {info.frames}）")

    use_mask = resolve_mask(mask, mask_strategy, bbox)
    engine = LamaEngine.get(device)
    engine.ensure_loaded()
    res = engine.inpaint_frame(
        src,
        bbox,
        use_mask,
        pad=pad,
        blend_mode=blend_mode,
    )

    out = {
        "src": src,
        "clean": res.frame,
        "infer_ms": round(res.elapsed * 1000),
        "total_ms": round((time.time() - t_all) * 1000),
        "device": res.device,
        "mask_px": res.mask_px,
        "mask_strategy": mask_strategy,
        "frame_idx": frame_idx,
        "video": info.to_dict(),
        "metrics": None,
    }

    if compute_metric and mask is not None:
        rep = compute_metrics(src, res.frame, mask, bbox)
        out["metrics"] = rep.to_dict()
    elif compute_metric:
        out["metrics"] = None

    return out

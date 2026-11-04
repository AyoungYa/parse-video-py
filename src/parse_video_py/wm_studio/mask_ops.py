# -*- coding: utf-8 -*-
"""mask 策略变换（不依赖 torch，纯 cv2/numpy）。

单独成模块的好处：测试和 mask 预览只需 cv2，不必拉起 LaMa/torch。
"""

import cv2
import numpy as np


def card_mask_from_sparse(mask: np.ndarray, dilate: int = 5) -> np.ndarray:
    """稀疏 mask → 全卡片 mask（bbox 内容外接矩形 + 膨胀）。

    ★ 踩坑手册坑 42：高动态 / 亮背景片必须用全卡片 mask。
      稀疏 mask 让 LaMa 在亮背景下"延续"旁边的亮内容进笔画位
      → 不是去水印而是重绘成亮块（实测降幅只有 72~83%）。
      全卡片把整块水印卡当一块重建，LaMa 才能拿到干净的边界上下文。

    全卡片**不要**再叠 rowinterp：实测会从 74% 掉到 48%
    （rowinterp 把卡片整宽按远端锚点拉平，破坏真实内容）。
    """
    m = np.asarray(mask)
    if m.dtype != np.uint8:
        m = (m > 0).astype(np.uint8) * 255
    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        (m > 127).astype(np.uint8), 8
    )
    # 去掉 <20px 碎噪（同铁律 1）
    keep = np.zeros_like(m)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 20:
            keep[labels == i] = 255
    if keep.max() == 0:
        keep = m

    ys, xs = np.where(keep > 0)
    if xs.size == 0:
        return m
    card = np.zeros_like(m)
    card[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1] = 255
    if dilate > 0:
        card = cv2.dilate(card, np.ones((dilate, dilate), np.uint8))
    return card


__all__ = ["card_mask_from_sparse"]

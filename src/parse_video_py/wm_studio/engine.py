# -*- coding: utf-8 -*-
"""LaMa 单例引擎：进程内逐帧推理，供「秒级预览」用。

为什么要在 Web 进程里常驻一个 LaMa：
  `lama_apply.py` 每次启动都要加载 big-lama.pt（约 200MB → MPS 上 5~8 秒）。
  调参时每改一个 slider 都重启一次进程，体验完全不可接受。
  所以这里把模型加载一次常驻，`inpaint_frame()` 直接吃一帧出结果。

为什么全片渲染不在这里跑：
  Python 的 GIL + MPS 上下文被 Web 进程持有，长任务会拖死 HTTP 服务；
  且 `lama_apply.py` 的流式 ffmpeg 管道跑在子进程里更稳（它自己锁 1 线程
  编码器，避免抢 CPU）。全片渲染见 `jobs.py`。

★ 合成方式（踩坑手册铁律 2）
  原版 `lama_apply.py` 第 94 行是**整块回贴 ROI**：
      result[y0:y1, x0:x1] = cv2.cvtColor(out_arr, ...)
  这会把 mask 之外的真实像素也换成 LaMa 重建值 → 稀疏 mask 下周边一圈发糊
  （实测真实像素高频只剩 39%）。这里默认走 **按 mask alpha 合成**
  （`blend_mode="mask"`），只在 mask 上采信 LaMa；需要原版行为时传
  `blend_mode="patch"`（全卡片 mask 下两者等价，可省一次羽化）。
"""

import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage as ndi


@dataclass
class InpaintResult:
    frame: np.ndarray  # 修复后整帧 BGR
    elapsed: float  # 推理耗时（秒）
    device: str
    mask_px: int  # 实际送进 LaMa 的 mask 像素数
    blend_mode: str


class LamaEngine:
    """线程安全的 LaMa 单例。

    模型只在第一次 `ensure_loaded()` 时加载。多线程调用会串行化
    （MPS 本身不能并发跑同一模型实例）。
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self, device: str = "auto"):
        self._device_pref = device
        self._lama = None
        self._device = None
        self._lock = threading.Lock()
        self._load_error = None

    # ---------- 单例 ----------
    @classmethod
    def get(cls, device: str = "auto") -> "LamaEngine":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(device)
            return cls._instance

    # ---------- 加载 ----------
    def resolve_device(self) -> str:
        if self._device_pref != "auto":
            return self._device_pref
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def ensure_loaded(self) -> None:
        """幂等加载模型；失败会记住错误并抛出。"""
        if self._lama is not None:
            return
        if self._load_error is not None:
            raise RuntimeError(self._load_error)
        with self._lock:
            if self._lama is not None:
                return
            if self._load_error is not None:
                raise RuntimeError(self._load_error)
            try:
                from simple_lama_inpainting import SimpleLama

                dev = self.resolve_device()
                self._lama = SimpleLama(device=dev)
                self._device = dev
            except Exception as exc:  # 权重下载失败 / 无 torch 等
                self._load_error = f"LaMa 加载失败: {exc}"
                raise RuntimeError(self._load_error) from exc

    @property
    def loaded(self) -> bool:
        return self._lama is not None

    def warmup(self, repeats: int = 2) -> float:
        """跑几张小图把 MPS 算子编译/显存分配预热掉。

        ★ 实测（261021 片）：启动后第 1 次推理 1234ms、第 2 次 572ms、
          第 3 次起稳定 300ms。前两次的差值是 MPS 内核首次编译 + 显存池
          扩张的固定开销，不是参数问题。预热掉它，用户第一次点预览
          就能拿到真实耗时（否则会误以为 mask 设错了）。
        返回最后一次的耗时（毫秒）。
        """
        self.ensure_loaded()
        dummy = np.zeros((128, 192, 3), np.uint8)
        m = np.zeros((64, 96), np.uint8)
        m[16:48, 24:72] = 255
        last = 0.0
        for _ in range(max(1, repeats)):
            t0 = time.time()
            x, y, w, h = 40, 24, 96, 64
            self.inpaint_frame(dummy, (x, y, w, h), m, pad=10)
            last = (time.time() - t0) * 1000
        return last

    @property
    def device(self) -> str:
        return self._device or self.resolve_device()

    # ---------- 推理 ----------
    def inpaint_frame(
        self,
        frame_bgr: np.ndarray,
        bbox,
        mask: np.ndarray | None = None,
        pad: int = 10,
        blend_mode: str = "mask",
        mask_dilate: int | None = None,
    ) -> InpaintResult:
        """修复**整帧**并返回整帧。

        bbox: (x, y, w, h) 整帧坐标；ROI = bbox 外扩 pad*3。
        mask: (h, w) bool/uint8，**相对 bbox 左上**。None → 用矩形 bbox 作 mask。
        pad: 与 lama_apply.py 同义（ROI 外扩 / 3）。
        blend_mode:
            "mask"  —— 按 mask alpha 合成（推荐，只改 mask 内像素）
            "patch" —— 原版行为：整块 ROI 回贴（全卡片 mask 下等价）
        mask_dilate: mask 额外膨胀像素，None → max(2, pad // 3)（同原版）
        """
        self.ensure_loaded()
        x, y, w, h = (int(v) for v in bbox)
        if w < 1 or h < 1:
            raise ValueError(f"bbox 尺寸非法: {bbox}")

        H, W = frame_bgr.shape[:2]
        x0, y0 = max(0, x - pad * 3), max(0, y - pad * 3)
        x1, y1 = min(W, x + w + pad * 3), min(H, y + h + pad * 3)
        crop = frame_bgr[y0:y1, x0:x1]

        # ---- 构造 ROI 级 mask ----
        roi_h, roi_w = crop.shape[:2]
        mask_roi = np.zeros((roi_h, roi_w), np.uint8)
        ox, oy = x - x0, y - y0  # bbox 在 ROI 内的偏移

        if mask is None:
            # 矩形 mask（原版无 --mask-file 的行为）
            mx0 = max(0, ox - pad)
            my0 = max(0, oy - pad)
            mx1 = min(roi_w, ox + w + pad)
            my1 = min(roi_h, oy + h + pad)
            mask_roi[my0:my1, mx0:mx1] = 255
        else:
            m = np.asarray(mask)
            if m.dtype != np.uint8:
                m = (m > 0).astype(np.uint8) * 255
            if m.shape != (h, w):
                m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
            it = max(2, pad // 3) if mask_dilate is None else max(0, int(mask_dilate))
            if it > 0:
                m = (
                    ndi.binary_dilation(
                        m > 127, structure=np.ones((3, 3)), iterations=it
                    ).astype(np.uint8)
                    * 255
                )
            else:
                m = ((m > 127).astype(np.uint8)) * 255
            # 贴进 ROI（bbox 可能被画面边界裁过，故用切片而非直接赋值）
            sy0, sy1 = max(0, oy), min(roi_h, oy + h)
            sx0, sx1 = max(0, ox), min(roi_w, ox + w)
            my0, my1 = sy0 - oy, sy1 - oy
            mx0, mx1 = sx0 - ox, sx1 - ox
            if sy1 > sy0 and sx1 > sx0:
                mask_roi[sy0:sy1, sx0:sx1] = m[my0:my1, mx0:mx1]

        mask_px = int((mask_roi > 127).sum())
        if mask_px == 0:
            return InpaintResult(frame_bgr.copy(), 0.0, self.device, 0, blend_mode)

        # ---- LaMa 推理 ----
        pil = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        t0 = time.time()
        out = self._lama(pil, Image.fromarray(mask_roi))
        self._sync()
        elapsed = time.time() - t0

        out_arr = np.array(out)
        ch, cw = crop.shape[:2]
        out_arr = out_arr[:ch, :cw]
        if out_arr.shape[:2] != (ch, cw):
            out_arr = cv2.resize(out_arr, (cw, ch))
        inpaint_bgr = cv2.cvtColor(out_arr, cv2.COLOR_RGB2BGR)

        result = frame_bgr.copy()
        if blend_mode == "patch":
            # 原版行为：整块 ROI 回贴
            result[y0:y1, x0:x1] = inpaint_bgr
        else:
            # ★ 铁律 2：按 mask alpha 合成，mask 外真实像素 100% 保留
            a = (mask_roi > 127).astype(np.float32)
            a = cv2.GaussianBlur(a, (0, 0), 1.0)[..., None]
            base = crop.astype(np.float32)
            merged = base * (1 - a) + inpaint_bgr.astype(np.float32) * a
            result[y0:y1, x0:x1] = np.clip(merged, 0, 255).astype(np.uint8)

        return InpaintResult(result, elapsed, self.device, mask_px, blend_mode)

    def _sync(self):
        """MPS/CUDA 异步执行，计时前必须同步。"""
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.synchronize()
            elif torch.backends.mps.is_available():
                torch.mps.synchronize()
        except Exception:
            pass


# mask 策略已移到 mask_ops（不依赖 torch），这里 re-export 保持旧导入可用
from parse_video_py.wm_studio.mask_ops import card_mask_from_sparse  # noqa: E402,F401

__all__ = ["InpaintResult", "LamaEngine", "card_mask_from_sparse"]

# -*- coding: utf-8 -*-
"""wm-studio HTTP 接口。

路由分组：
  GET  /wm                    操作页
  POST /wm/api/open           打开视频（探测信息 + 默认 bbox 建议）
  POST /wm/api/mask           重建 mask（white_th / grow / 策略）
  GET  /wm/api/mask/img       取 mask 可视化图
  POST /wm/api/preview        单帧预览（返回成片帧 + 指标）
  POST /wm/api/compare        左右对比图（原片 | 成片 | 差值放大）
  POST /wm/api/job            创建并启动全片渲染
  GET  /wm/api/jobs           任务列表
  GET  /wm/api/job/{tid}      任务详情
  GET  /wm/api/job/{tid}/log  增量日志
  POST /wm/api/job/{tid}/stop 停止任务
  GET  /wm/api/job/{tid}/file 下载/播放成片
  GET  /wm/media?path=        受控的本地媒体文件（预览视频流）

设计取舍：
  - 全部返回 JSON + base64 图片（本地单用户工具，图省一次请求），
    只有视频流走独立路由（要支持 Range 才能拖动进度条）。
  - 不引入数据库：状态落 workspace/jobs.json。
"""

import base64
import os
import time
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from parse_video_py.wm_studio import masks as masks_mod
from parse_video_py.wm_studio.jobs import manager
from parse_video_py.wm_studio.mask_ops import card_mask_from_sparse
from parse_video_py.wm_studio.metrics import compute_metrics
from parse_video_py.wm_studio.paths import workspace_root
from parse_video_py.wm_studio.preview import preview_frame
from parse_video_py.wm_studio.video import (
    frame_to_jpeg_bytes,
    grab_frame_bgr,
    probe_video,
)

router = APIRouter(prefix="/wm", tags=["watermark-studio"])

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class OpenReq(BaseModel):
    video: str
    # 可选：直接指定 bbox；不给则按右上角给建议值
    bbox: list[int] | None = None


class MaskReq(BaseModel):
    video: str
    bbox: list[int]
    white_th: int = 168
    grow: int = 21
    frames: int = 120
    card_std: float = 30.0
    pad_clip: int = 8
    check_margin: int = 160
    strategy: str = "card"  # sparse | card
    auto_white_th: bool = False
    # fast=True 走本地单次解码批量抽帧（<4s）；False 回到 build_mask.py（慢，含窗口外探测）
    fast: bool = True


class PreviewReq(BaseModel):
    video: str
    bbox: list[int]
    frame_idx: int = 0
    mask_path: str = ""
    strategy: str = "card"
    pad: int = 10
    device: str = "auto"
    blend_mode: str = "mask"
    # 返回时把预览帧裁剪缩放（None = 整帧等比例限宽）
    crop_roi: bool = False
    target_width: int = 1400


class CompareReq(BaseModel):
    video: str
    bbox: list[int]
    frame_idx: int = 0
    mask_path: str = ""
    strategy: str = "card"
    pad: int = 10
    device: str = "auto"
    gain: float = 6.0  # 差值图放大倍数


class JobReq(BaseModel):
    video: str
    bbox: list[int]
    mask_path: str = ""
    strategy: str = "card"
    pad: int = 10
    crf: int = 17
    device: str = "auto"
    keyframe_every: int = 1
    start: float = 0.0
    duration: float = 0.0
    texture_mode: str = "off"  # off | on
    texture_strength: float = 1.3
    texture_tile: int = 48
    texture_lf_mode: str = "off"  # off | rowinterp
    out_video: str = ""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _b64_jpeg(frame: np.ndarray, quality: int = 88) -> str:
    return base64.b64encode(frame_to_jpeg_bytes(frame, quality)).decode("ascii")


def _check_video(path: str) -> str:
    p = str(Path(path).expanduser())
    if not os.path.isfile(p):
        raise HTTPException(404, f"视频不存在: {p}")
    return p


def _resize_to_width(frame: np.ndarray, target: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if target <= 0 or w <= target:
        return frame
    scale = target / w
    return cv2.resize(
        frame, (target, max(1, int(h * scale))), interpolation=cv2.INTER_AREA
    )


def _suggest_bbox(video: str) -> dict:
    """默认建议 bbox：右上角 1/5 宽、1/8 高。"""
    info = probe_video(video)
    W, H = info.width or 1920, info.height or 1080
    w = max(200, int(W * 0.24))
    h = max(80, int(H * 0.13))
    x = W - w - max(10, int(W * 0.012))
    y = max(10, int(H * 0.025))
    return {"x": x, "y": y, "w": w, "h": h}


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return _TEMPLATES.TemplateResponse(request, "wm_index.html", {})


# ---------------------------------------------------------------------------
# 打开 / 探测
# ---------------------------------------------------------------------------


@router.post("/api/open")
def api_open(req: OpenReq):
    video = _check_video(req.video)
    info = probe_video(video)
    bbox = req.bbox or list(_suggest_bbox(video).values())
    if len(bbox) != 4:
        raise HTTPException(400, "bbox 必须是 [x,y,w,h]")

    # 首帧缩略图（用于拖框）
    frame = grab_frame_bgr(video, 0)
    thumb = ""
    if frame is not None:
        thumb = _b64_jpeg(_resize_to_width(frame, 1400), 85)

    # 预热 LaMa（异步不必要：本地单用户，等 1~2s 换后续每次快 4 倍）
    warm = None
    try:
        from parse_video_py.wm_studio.engine import LamaEngine

        eng = LamaEngine.get("auto")
        t0 = time.time()
        eng.ensure_loaded()
        eng.warmup(repeats=2)
        warm = {"device": eng.device, "ms": round((time.time() - t0) * 1000)}
    except Exception as exc:  # 模型缺失时不要挡住载入
        warm = {"error": str(exc)}

    return {
        "video": info.to_dict(),
        "bbox": bbox,
        "thumb": thumb,
        "suggest_bbox": _suggest_bbox(video),
        "workspace": str(workspace_root()),
        "warmup": warm,
    }


@router.get("/favicon.ico", include_in_schema=False)
def favicon():
    """空 favicon，避免浏览器每次 404（本地单用户工具，不值得放图标资源）。"""
    return Response(status_code=204)


@router.post("/api/frame")
def api_frame(req: PreviewReq):
    """只取原始帧（拖动帧号时看原片，不跑 LaMa）。"""
    video = _check_video(req.video)
    frame = grab_frame_bgr(video, req.frame_idx)
    if frame is None:
        raise HTTPException(400, f"取帧失败: {req.frame_idx}")
    out = _resize_to_width(frame, req.target_width)
    return {"frame_idx": req.frame_idx, "img": _b64_jpeg(out, 86)}


# ---------------------------------------------------------------------------
# mask
# ---------------------------------------------------------------------------


@router.post("/api/mask")
def api_mask(req: MaskReq):
    video = _check_video(req.video)
    x, y, w, h = req.bbox

    extra = {}
    if req.auto_white_th:
        extra = masks_mod.auto_white_th(video, x, y, w, h)

    work = workspace_root() / "masks"
    work.mkdir(parents=True, exist_ok=True)
    stem = Path(video).stem
    out = work / f"{stem}_{x}_{y}_{w}_{h}_t{req.white_th}_g{req.grow}.png"

    rep = masks_mod.build_mask(
        video,
        x,
        y,
        w,
        h,
        str(out),
        white_th=req.white_th,
        grow=req.grow,
        frames=req.frames,
        card_std=req.card_std,
        pad_clip=req.pad_clip,
        check_margin=req.check_margin,
        fast=req.fast,
    )
    result = rep.to_dict()
    result["mask_path"] = str(out) if rep.ok else ""
    result["strategy"] = req.strategy
    result["auto"] = extra

    if rep.ok:
        m = cv2.imread(str(out), cv2.IMREAD_GRAYSCALE)
        if m is not None:
            if req.strategy == "card":
                shown = card_mask_from_sparse(m)
                result["card_px"] = int((shown > 127).sum())
            else:
                shown = m
            result["sparse_px"] = int((m > 127).sum())
            # 可视化：mask 叠加在首帧上
            frame = grab_frame_bgr(video, 0)
            if frame is not None:
                result["overlay"] = _b64_jpeg(_mask_overlay(frame, shown, x, y), 84)
    return result


def _mask_overlay(frame: np.ndarray, mask: np.ndarray, x: int, y: int) -> np.ndarray:
    """把 mask 以红色半透明叠在帧上，并画 bbox 与放大图。"""
    vis = frame.copy()
    H, W = vis.shape[:2]
    h, w = mask.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 > x0 and y1 > y0:
        sub = mask[y0 - y : y0 - y + (y1 - y0), x0 - x : x0 - x + (x1 - x0)]
        sel = sub > 127
        roi = vis[y0:y1, x0:x1]
        roi[sel] = (0.35 * roi[sel] + 0.65 * np.array([0, 0, 255])).astype(np.uint8)
        vis[y0:y1, x0:x1] = roi
        cv2.rectangle(vis, (x0, y0), (x1 - 1, y1 - 1), (0, 255, 0), 2)

    # 右上角放大插图（mask 区域常很小，缩略图上看不清）
    pad = 20
    cx0, cy0 = max(0, x - pad), max(0, y - pad)
    cx1, cy1 = min(W, x + w + pad), min(H, y + h + pad)
    crop = vis[cy0:cy1, cx0:cx1]
    if crop.size:
        scale = max(1.0, min(3.0, 720.0 / max(1, crop.shape[1])))
        big = cv2.resize(
            crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST
        )
        bh, bw = big.shape[:2]
        if bh < H and bw < W:
            vis[H - bh - 8 : H - 8, W - bw - 8 : W - 8] = big
            cv2.rectangle(
                vis, (W - bw - 8, H - bh - 8), (W - 9, H - 9), (0, 255, 255), 2
            )
    return _resize_to_width(vis, 1200)


# ---------------------------------------------------------------------------
# 预览
# ---------------------------------------------------------------------------


@router.post("/api/preview")
def api_preview(req: PreviewReq):
    video = _check_video(req.video)
    mask = None
    if req.mask_path and os.path.isfile(req.mask_path):
        mask = cv2.imread(req.mask_path, cv2.IMREAD_GRAYSCALE)

    t0 = time.time()
    try:
        res = preview_frame(
            video,
            tuple(req.bbox),
            req.frame_idx,
            mask,
            mask_strategy=req.strategy,
            pad=req.pad,
            device=req.device,
            blend_mode=req.blend_mode,
        )
    except Exception as exc:
        raise HTTPException(500, f"预览失败: {exc}") from exc

    src, clean = res["src"], res["clean"]
    if req.crop_roi:
        x, y, w, h = req.bbox
        pad = 24
        x0, y0 = max(0, x - pad), max(0, y - pad)
        H, W = src.shape[:2]
        x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
        src = src[y0:y1, x0:x1]
        clean = clean[y0:y1, x0:x1]
        scale = max(1.0, min(4.0, 900.0 / max(1, src.shape[1])))
    else:
        scale = 1.0

    disp_src = _resize_to_width(src, req.target_width)
    disp_cln = _resize_to_width(clean, req.target_width)
    if scale > 1.0:
        disp_src = cv2.resize(
            src, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST
        )
        disp_cln = cv2.resize(
            clean, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST
        )

    result = {
        "frame_idx": res["frame_idx"],
        "src_img": _b64_jpeg(disp_src, 88),
        "clean_img": _b64_jpeg(disp_cln, 88),
        "infer_ms": res["infer_ms"],
        "total_ms": round((time.time() - t0) * 1000),
        "device": res["device"],
        "mask_px": res["mask_px"],
        "mask_strategy": res["mask_strategy"],
        "metrics": res["metrics"],
        "video": res["video"],
    }
    return result


@router.post("/api/compare")
def api_compare(req: CompareReq):
    """三联图：原片 | 成片 | 差值放大。差值图是最能看出残留的形式。"""
    video = _check_video(req.video)
    mask = None
    if req.mask_path and os.path.isfile(req.mask_path):
        mask = cv2.imread(req.mask_path, cv2.IMREAD_GRAYSCALE)

    res = preview_frame(
        video,
        tuple(req.bbox),
        req.frame_idx,
        mask,
        mask_strategy=req.strategy,
        pad=req.pad,
        device=req.device,
        compute_metric=False,
    )
    src, clean = res["src"], res["clean"]

    x, y, w, h = req.bbox
    pad = 24
    H, W = src.shape[:2]
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
    cs, cc = src[y0:y1, x0:x1], clean[y0:y1, x0:x1]

    diff = cv2.absdiff(cs, cc).astype(np.float32)
    gray_diff = diff.max(axis=2)
    amp = np.clip(gray_diff * req.gain, 0, 255).astype(np.uint8)
    heat = cv2.applyColorMap(amp, cv2.COLORMAP_JET)

    scale = max(1.0, min(5.0, 560.0 / max(1, cs.shape[1])))

    def up(img):
        return cv2.resize(
            img, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST
        )

    panels = [up(cs), up(cc), up(heat)]
    hs = max(p.shape[0] for p in panels)
    ws = sum(p.shape[1] for p in panels) + 8 * (len(panels) - 1)
    strip = np.full((hs + 26, ws, 3), 24, np.uint8)
    cx = 0
    for p, label in zip(panels, ("原片", "成片", f"差值 x{req.gain:g}")):
        ph, pw = p.shape[:2]
        strip[26 : 26 + ph, cx : cx + pw] = p
        cv2.putText(
            strip,
            label,
            (cx + 6, 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (230, 230, 230),
            1,
            cv2.LINE_AA,
        )
        cx += pw + 8

    metrics = None
    if mask is not None:
        metrics = compute_metrics(src, clean, mask, req.bbox).to_dict()
    return {
        "img": _b64_jpeg(_resize_to_width(strip, 1500), 88),
        "metrics": metrics,
        "infer_ms": res["infer_ms"],
    }


# ---------------------------------------------------------------------------
# 渲染任务
# ---------------------------------------------------------------------------


@router.post("/api/job")
def api_job_create(req: JobReq):
    video = _check_video(req.video)
    # ★ 必须转绝对路径：任务用 cwd=工作区目录跑，相对路径在那里解不开
    video = os.path.abspath(video)
    if not req.mask_path or not os.path.isfile(req.mask_path):
        raise HTTPException(400, "全片渲染必须提供已生成的 mask（否则无法精确修复）")
    mask_path = os.path.abspath(req.mask_path)
    mgr = manager()
    params = req.model_dump()
    params["mask_path"] = mask_path
    out = os.path.abspath(req.out_video) if req.out_video else ""
    job = mgr.create(video, req.bbox, params, out)
    mgr.start(job.task_id)
    job = mgr.get(job.task_id)
    return job.to_dict() if job else {"task_id": job.task_id}


@router.get("/api/jobs")
def api_jobs():
    return {"jobs": [j.to_dict() for j in manager().list()]}


@router.get("/api/job/{tid}")
def api_job_get(tid: str):
    job = manager().get(tid)
    if job is None:
        raise HTTPException(404, "任务不存在")
    d = job.to_dict()
    if job.work_dir:
        out = Path(job.out_video)
        d["out_exists"] = out.is_file()
        d["out_size_mb"] = round(out.stat().st_size / 1e6, 1) if out.is_file() else 0
    return d


@router.get("/api/job/{tid}/log")
def api_job_log(tid: str, offset: int = 0):
    return manager().log(tid, offset)


@router.post("/api/job/{tid}/stop")
def api_job_stop(tid: str):
    return manager().stop(tid)


@router.get("/api/job/{tid}/file")
def api_job_file(tid: str, request: Request):
    job = manager().get(tid)
    if job is None:
        raise HTTPException(404, "任务不存在")
    out = Path(job.out_video)
    if not out.is_file():
        raise HTTPException(404, f"成片还不存在: {out}")
    # 走 Range 版本，浏览器 <video> 才能拖动进度条
    return _serve_with_range(out, request, filename=out.name)


# ---------------------------------------------------------------------------
# 媒体流（支持 Range，供 <video> 拖动进度）
# ---------------------------------------------------------------------------

_CHUNK = 1024 * 512


def _serve_with_range(p: Path, request: Request, filename: str | None = None):
    """带 Range 的文件响应（206 Partial Content）。

    `FileResponse` 不处理 Range，直接用它浏览器 <video> 拿不到拖动能力。
    """
    size = p.stat().st_size
    suffix = p.suffix.lower()
    ctype = {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(suffix, "application/octet-stream")

    range_header = request.headers.get("range")
    if not range_header:
        headers = {"Accept-Ranges": "bytes"}
        if filename:
            headers["Content-Disposition"] = (
                f"inline; filename*=UTF-8''{quote(filename)}"
            )
        return FileResponse(str(p), media_type=ctype, headers=headers)

    try:
        _, _, spec = range_header.partition("=")
        s, _, e = spec.partition("-")
        start = int(s) if s.strip() else 0
        end = int(e) if e.strip() else size - 1
    except ValueError:
        start, end = 0, size - 1
    start = max(0, min(start, size - 1))
    end = max(start, min(end, size - 1))
    length = end - start + 1

    def gen():
        with open(p, "rb") as f:
            f.seek(start)
            remain = length
            while remain > 0:
                chunk = f.read(min(_CHUNK, remain))
                if not chunk:
                    break
                remain -= len(chunk)
                yield chunk

    return StreamingResponse(
        gen(),
        status_code=206,
        media_type=ctype,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )


@router.get("/media")
def media(path: str, request: Request):
    p = Path(path).expanduser()
    if not p.is_file():
        raise HTTPException(404, "文件不存在")
    return _serve_with_range(p, request)

"""本机处理管线：下载 → 建 mask → LaMa 修复 → 验收 → 回传。

三条来自踩坑手册的铁律，直接固化在代码里：
  1. **判定成功只看退出码**，不看日志里有没有「完成」——`echo 'ALL DONE'` 是 shell 回显，
     中间命令全崩也照打（历史上把崩溃报成成功过）。
  2. **子进程 cwd 换了就必须传绝对路径**，否则 ffprobe 返回空、脚本崩。
  3. **抽帧按内存分流**（BATCH_DECODE_LIMIT）已在脚本内实现，这里不重复解码。

阶段命令来自 settings.WM_STAGES_TEMPLATE，可改 .env 的脚本参数而不动代码。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from django.conf import settings

logger = logging.getLogger(__name__)

FRAME_RE = re.compile(r"(\d{1,7})\s*/\s*(\d{1,7})")
PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
# ★ 方括号包裹的阶段标记（如 "[1/3]"）不是帧进度，必须先剔除。
#   否则 lama_apply 的 "[1/3] 加载模型" 会被当成 1/3 帧 → 进度条来回跳。
BRACKET_RE = re.compile(r"\[\s*\d+\s*/\s*\d+\s*\]")
TAIL_LINES = 200

# 与 scripts/wm_common.py 的退出码约定保持一致（改一处必须同步另一处）
_EXIT_LOCATE_LOW_CONFIDENCE = 3
_BBOX_LINE_RE = re.compile(r"^BBOX_RESULT:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", re.M)
_LOW_CONFIDENCE_RE = re.compile(r"^LOW_CONFIDENCE:\s*(.+)$", re.M)
#: 候选框是一整行 JSON（非贪婪匹配到行尾）
_CANDIDATES_RE = re.compile(r"^LOCATE_CANDIDATES:\s*(\{.*\}|\[.*\])\s*$", re.M)


class PipelineError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = True, candidates: list | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        # 定位失败时带上候选框，云端转人工页面直接拿来当默认框
        self.candidates = candidates or []


class Aborted(Exception):
    """租约丢失或用户取消，主动中止。"""


@dataclass
class StageResult:
    name: str
    exit_code: int
    elapsed: float
    tail: str = ""


@dataclass
class PipelineOutcome:
    out_path: Path
    report_path: Path
    metrics: dict = field(default_factory=dict)
    stages: list[StageResult] = field(default_factory=list)


ProgressFn = Callable[[str, float], None]  # (stage, progress 0~1)


class Pipeline:
    def __init__(
        self,
        scripts_dir: Path | None = None,
        workspace: Path | None = None,
        device: str | None = None,
        python: str | None = None,
    ):
        self.scripts_dir = Path(scripts_dir or settings.WM_SCRIPTS_DIR).resolve()
        self.workspace = Path(workspace or settings.WORKSPACE_DIR).resolve()
        self.device = device or settings.TORCH_DEVICE
        self.python = python or settings.WM_PYTHON
        self.abort_event = threading.Event()

    # ------------------------------------------------------------------
    # 对外主入口
    # ------------------------------------------------------------------
    def prepare(self, payload: dict, on_progress: ProgressFn) -> tuple[list[Path], list[int], list[dict]]:
        """只抽关键帧和定位水印；用户确认前不运行修复。"""
        task_id = payload["task_id"]
        work = self.workspace / task_id
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        video = work / "input.mp4"
        frames = [work / f"keyframe-{i}.jpg" for i in range(3)]
        mask = work / "mask.png"
        on_progress("extracting_frames", 0.02)
        self._download(payload["video_url"], video)
        if self.abort_event.is_set():
            raise Aborted("租约丢失")
        duration = max(0.0, float((payload.get("meta") or {}).get("duration_ms") or 0) / 1000)
        for index, frame in enumerate(frames):
            if self.abort_event.is_set():
                raise Aborted("租约丢失")
            sample_at = min(duration * (0.1 + index * 0.4), max(0.0, duration - 0.1))
            cmd = ["ffmpeg", "-y", "-ss", str(sample_at), "-i", str(video),
                   "-frames:v", "1", str(frame)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if result.returncode or not frame.exists():
                raise PipelineError("KEYFRAME_FAILED", result.stderr[-800:], retryable=True)
            on_progress("extracting_frames", 0.1 + (index + 1) * 0.08)
        on_progress("locating_watermark", 0.35)
        stage = next(s for s in settings.WM_STAGES_TEMPLATE if s["name"] == "build_mask")
        ctx = {"python": self.python, "scripts": str(self.scripts_dir), "input": str(video),
               "mask": str(mask), "bbox": "", "device": self.device}
        result = self._run_stage(stage, ctx, lambda frac: on_progress("locating_watermark", 0.35 + frac * 0.6))
        candidates = self._locate_candidates(result.tail)
        bbox_text = self._bbox_from_output(result.tail)
        if result.exit_code == _EXIT_LOCATE_LOW_CONFIDENCE:
            # 仍交给用户确认：可手动绘制；候选框只是建议。
            bbox = [candidates[0][k] for k in ("x", "y", "w", "h")] if candidates else []
        elif result.exit_code != 0 or not bbox_text:
            raise PipelineError("LOCATE_FAILED", result.tail[-800:], retryable=True)
        else:
            bbox = [int(v) for v in bbox_text.split()]
        return frames, bbox, candidates

    def run(self, payload: dict, on_progress: ProgressFn) -> PipelineOutcome:
        if not (payload.get("params") or {}).get("bbox_confirmed"):
            raise PipelineError("BBOX_UNCONFIRMED", "水印位置尚未由用户确认", retryable=False)
        task_id = payload["task_id"]
        work = self.workspace / task_id
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)

        video = work / "input.mp4"
        mask = work / "mask.png"
        out = work / "output.mp4"
        out_fixed = work / "output_fixed.mp4"
        report = work / "report.json"

        try:
            on_progress("download", 0.02)
            self._download(payload["video_url"], video)

            if payload.get("mask_url"):
                self._download(payload["mask_url"], mask)

            bbox = self._bbox_str(payload.get("params") or {})
            ctx = {
                "python": self.python,
                "scripts": str(self.scripts_dir),
                "work": str(work),
                "input": str(video),
                "mask": str(mask),
                "out": str(out),
                "out_fixed": str(out_fixed),
                "report": str(report),
                "bbox": bbox,
                "pad": str((payload.get("params") or {}).get("pad", settings.DEFAULT_PAD)),
                "device": self.device,
            }

            stages = self._plan_stages(payload, mask)
            stage_results: list[StageResult] = []
            total = max(1, len(stages))
            for idx, stage in enumerate(stages):
                if self.abort_event.is_set():
                    raise Aborted("租约丢失，中止处理")
                on_progress(stage["name"], 0.05 + idx / total * 0.9)

                def stage_progress(frac: float, _idx=idx, _total=total) -> None:
                    overall = (_idx + max(0.0, min(1.0, frac))) / _total
                    # 渲染占 0.05~0.95，给下载和回传留头尾
                    on_progress(stage["name"], 0.05 + overall * 0.9)

                result = self._run_stage(stage, ctx, stage_progress)
                stage_results.append(result)
                if result.exit_code == _EXIT_LOCATE_LOW_CONFIDENCE:
                    # 定位置信度不足 → 转人工拖框，不重试（同样输入还是定位不到）
                    reason = self._low_confidence_reason(result.tail)
                    candidates = self._locate_candidates(result.tail)
                    logger.info("定位置信度不足：%s，候选框 %d 个", reason, len(candidates))
                    raise PipelineError(
                        "LOCATE_LOW_CONFIDENCE",
                        reason,
                        retryable=False,
                        candidates=candidates,
                    )
                if result.exit_code != 0:
                    raise PipelineError(
                        f"STAGE_{stage['name'].upper()}_FAILED",
                        f"阶段 {stage['name']} 退出码 {result.exit_code}\n{result.tail[-800:]}",
                        retryable=not self._is_oom(result.tail),
                    )
                # 阶段产物缺失也算失败（退出码 0 但没产出，历史踩过）
                if stage["name"] == "lama_apply" and not out.exists():
                    raise PipelineError("NO_OUTPUT", f"渲染结束但 {out} 不存在", retryable=True)

                # build_mask 自动定位后回填最终 bbox（lama/verify/修复轮都要用）
                if stage["name"] == "build_mask":
                    ctx["bbox"] = self._bbox_from_output(result.tail) or ctx["bbox"]
                    logger.info("build_mask 最终 bbox: %s", ctx["bbox"])

            metrics = self._read_report(report)
            if not out.exists():
                raise PipelineError("NO_OUTPUT", "未生成成片", retryable=True)

            # ---- 决策树修复轮（坑 44）：不绿 → 按诊断追加一次修复 + 复验 ----
            final_out = out
            repair_name = {"needs_texture": "texture_fix", "needs_rowinterp": "texture_fix_rowinterp"}
            diagnosis = (metrics or {}).get("diagnosis")
            if diagnosis in settings.WM_REPAIR_STAGES and not metrics.get("all_green"):
                logger.info("决策树：diagnosis=%s，进入修复轮", diagnosis)
                repair_stage = {
                    "name": repair_name[diagnosis],
                    "cmd": settings.WM_REPAIR_STAGES[diagnosis],
                }
                res = self._run_stage(repair_stage, ctx, lambda f: on_progress(repair_name[diagnosis], 0.05 + 0.9 * min(1.0, f)))
                stage_results.append(res)
                if res.exit_code != 0:
                    # 修复失败不判整个任务死刑：交付第一轮成片 + 原始诊断
                    logger.warning("修复轮失败（exit %s），回退第一轮成片", res.exit_code)
                elif out_fixed.exists():
                    reverify = {"name": "verify_final", "cmd": settings.WM_REVERIFY_CMD}
                    res2 = self._run_stage(reverify, ctx, lambda f: on_progress("verify_final", 0.05 + 0.9 * min(1.0, f)))
                    stage_results.append(res2)
                    m2 = self._read_report(report)
                    if res2.exit_code == 0 and m2:
                        # 两轮都不绿时，交付「评分更好」的那份
                        if m2.get("all_green") or self._score(m2) >= self._score(metrics):
                            final_out = out_fixed
                            metrics = m2
                            logger.info("修复轮生效：diagnosis=%s", m2.get("diagnosis"))
                        else:
                            logger.info("修复轮未改善（%s → %s），保留第一轮",
                                        metrics.get("diagnosis"), m2.get("diagnosis"))

            return PipelineOutcome(out_path=final_out, report_path=report,
                                   metrics=metrics, stages=stage_results)
        except Aborted:
            raise
        except PipelineError:
            raise
        except requests.RequestException as exc:
            raise PipelineError("NETWORK", f"文件传输失败：{exc}", retryable=True) from exc
        except Exception as exc:  # noqa: BLE001
            raise PipelineError("PIPELINE_UNEXPECTED", repr(exc), retryable=True) from exc

    def abort(self) -> None:
        self.abort_event.set()

    def cleanup(self, task_id: str) -> None:
        shutil.rmtree(self.workspace / task_id, ignore_errors=True)

    # ------------------------------------------------------------------
    # 阶段编排
    # ------------------------------------------------------------------
    def _plan_stages(self, payload: dict, mask_path: Path) -> list[dict]:
        has_mask = bool(payload.get("mask_url")) or mask_path.exists()
        planned = []
        for stage in settings.WM_STAGES_TEMPLATE:
            if stage.get("enabled_when") == "no_mask" and has_mask:
                continue
            planned.append(stage)
        return planned

    def _run_stage(self, stage: dict, ctx: dict, on_progress: Callable[[float], None]) -> StageResult:
        cmd = [part.format(**ctx) for part in stage["cmd"]]
        name = stage["name"]
        logger.info("阶段 %s 开始：%s", name, " ".join(cmd))

        started = time.monotonic()
        tail: list[str] = []
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.scripts_dir),  # 脚本用绝对路径，cwd 只影响相对资源
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=_creation_flags(),
        )
        assert proc.stdout is not None

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                tail.append(line)
                if len(tail) > TAIL_LINES:
                    tail = tail[-TAIL_LINES:]
                frac = _parse_progress(line)
                if frac is not None:
                    on_progress(frac)
                if self.abort_event.is_set():
                    logger.warning("检测到中止信号，终止阶段 %s", name)
                    _kill(proc)
                    raise Aborted("租约丢失，已终止本地渲染")
            proc.wait()
        finally:
            if proc.poll() is None:
                _kill(proc)

        elapsed = time.monotonic() - started
        logger.info("阶段 %s 结束：exit=%s 用时 %.1fs", name, proc.returncode, elapsed)
        return StageResult(name=name, exit_code=proc.returncode or 0, elapsed=elapsed, tail="\n".join(tail))

    # ------------------------------------------------------------------
    # 传输
    # ------------------------------------------------------------------
    def _download(self, url: str, dest: Path) -> None:
        with requests.get(url, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if self.abort_event.is_set():
                        raise Aborted("下载中收到中止信号")
                    fh.write(chunk)
        if dest.stat().st_size == 0:
            raise PipelineError("EMPTY_DOWNLOAD", "下载到的文件为空", retryable=True)

    def upload(self, url: str, path: Path, content_type: str = "application/octet-stream") -> None:
        """PUT 到云端签发的签名 URL。算力机不持有 OSS 凭证。"""
        size = path.stat().st_size
        with open(path, "rb") as fh:
            resp = requests.put(
                url, data=fh, headers={"Content-Type": content_type}, timeout=1800
            )
        if resp.status_code >= 400:
            raise PipelineError(
                "UPLOAD_FAILED", f"回传失败 {resp.status_code}：{resp.text[:300]}", retryable=True
            )
        logger.info("回传完成 %s（%.1f MB）", path.name, size / 1048576)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _bbox_str(params: dict) -> str:
        bbox = params.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            return " ".join(str(int(v)) for v in bbox)
        if isinstance(bbox, str) and bbox.strip():
            return bbox.strip()
        return ""  # 空则交给 build_mask 自动定位（脚本内走 min 投影）

    @staticmethod
    def _bbox_from_output(stage_tail: str) -> str | None:
        """build_mask 自动定位后，从阶段输出里解析最终 bbox 回填 ctx。"""
        m = _BBOX_LINE_RE.search(stage_tail or "")
        if not m:
            return None
        return " ".join(m.groups())

    @staticmethod
    def _low_confidence_reason(stage_tail: str) -> str:
        """从 build_mask 输出里取低置信度原因。"""
        m = _LOW_CONFIDENCE_RE.search(stage_tail or "")
        if m:
            return m.group(1).strip()
        return "自动定位不可信，请手动框选水印位置"

    @staticmethod
    def _locate_candidates(stage_tail: str) -> list[dict]:
        """从 build_mask 输出里取候选框列表。"""
        m = _CANDIDATES_RE.search(stage_tail or "")
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        out = []
        for c in data[:5]:
            if isinstance(c, dict) and {"x", "y", "w", "h"} <= set(c):
                out.append({k: int(c[k]) for k in ("x", "y", "w", "h")})
        return out

    @staticmethod
    def _score(metrics: dict | None) -> float:
        """两轮成片择优的评分（越大越好）。全绿与否优先，再看各分项。"""
        if not metrics:
            return -1e9
        if metrics.get("all_green"):
            return 1e6
        raw = metrics.get("raw") or metrics
        score = 0.0
        gd = raw.get("glyph_drop") or {}
        sig = raw.get("sigma_ratio") or {}
        lap = raw.get("laplacian_ratio")
        fl = raw.get("flicker_p90")
        if isinstance(gd, dict) and gd.get("median") is not None:
            score += min(gd["median"], 100.0)
        if isinstance(sig, dict) and sig.get("median") is not None:
            score -= abs(sig["median"] - 1.1) * 10   # σ 越接近 1.1 越好
        if lap is not None:
            score -= max(0.0, lap - 1.0) * 20
        if fl is not None:
            score -= max(0.0, fl - 1.0) * 20
        return score

    @staticmethod
    def _read_report(report_path: Path) -> dict:
        if not report_path.exists():
            return {}
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            logger.warning("报告解析失败：%s", exc)
            return {}
        return _normalize_metrics(data)

    @staticmethod
    def _is_oom(output: str) -> bool:
        lower = output.lower()
        return "out of memory" in lower or "cuda error" in lower


def _normalize_metrics(raw: dict) -> dict:
    """把 verify.py 的报告收敛成前端能直接展示的指标。

    产品版 verify.py（gpu_worker/scripts/verify.py）的 report 已是统一结构：
      residual_abs / sigma_ratio.median / flicker_p90 / diagnosis / all_green。
    这里只做三件事：展平嵌套、兼容旧版扁平字段、标记缺省。
    前端 MetricReport.vue 只认三个 key：residual_max / gradient_p50_ratio / flicker_p90。
    """
    flat = raw
    if isinstance(raw.get("summary"), dict):
        flat = {**raw, **raw["summary"]}

    def pick(path: str, *names, default=None):
        # 先按 "a.b" 路径取（产品版嵌套结构），再退回扁平 key（旧版兼容）
        cur: object = flat
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        if isinstance(cur, (int, float)):
            return float(cur)
        for n in names:
            v = flat.get(n)
            if isinstance(v, (int, float)):
                return float(v)
        return default

    sigma = pick("sigma_ratio.median", "sigma_ratio", "gradient_p50_ratio", "detail_ratio")
    metrics = {
        "residual_max": pick("residual_abs", "residual_max", "residual", "max_residual"),
        "gradient_p50_ratio": sigma,
        "flicker_p90": pick("flicker_p90", "flicker", "temporal_p90"),
        "diagnosis": flat.get("diagnosis"),
    }
    checks = {
        "residual_max": metrics["residual_max"] is not None and metrics["residual_max"] < 3,
        "gradient_p50_ratio": metrics["gradient_p50_ratio"] is not None
        and metrics["gradient_p50_ratio"] > 0.85,
        "flicker_p90": metrics["flicker_p90"] is not None and metrics["flicker_p90"] < 1.5,
    }
    # verify 自己的 all_green 优先（它看的检查项更全：region_max/laplacian 等）
    metrics["all_green"] = bool(flat.get("all_green")) or (
        bool(checks) and all(checks.values()) and all(metrics[k] is not None for k in checks)
    )
    metrics["raw"] = raw
    return metrics


def _creation_flags() -> int:
    """Windows 下不弹黑窗（子进程常驻后台）。"""
    if os.name == "nt":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _parse_progress(line: str) -> float | None:
    """从脚本输出里抠进度：优先识别 `1843/4428` 帧号，其次 `42%`。

    ★ 阶段标记 `[1/3]` 要先剔除：它是"第几步"不是"第几帧"，
      不剔除会让进度条在 33%~100% 之间乱跳（实测踩过）。
    """
    cleaned = BRACKET_RE.sub(" ", line)
    m = FRAME_RE.search(cleaned)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        if total > 0:
            return min(1.0, done / total)
    m = PCT_RE.search(cleaned)
    if m:
        return min(1.0, float(m.group(1)) / 100)
    return None

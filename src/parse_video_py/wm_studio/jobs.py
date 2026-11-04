# -*- coding: utf-8 -*-
"""全片渲染任务管理：子进程 + 日志轮询 + 可停止。

为什么必须走子进程而不是进程内跑：
  - 7239 帧的 4K 片要 52 分钟，HTTP 请求不可能等；
  - LaMa 长跑会把 GIL / MPS 上下文占住，Web 服务直接失去响应；
  - `lama_apply.py` 自带流式 ffmpeg 管道（编码器锁 1 线程防抢 CPU），
    这些已经在踩坑手册里调优过，直接复用比自己重写安全。

任务脱离会话（`start_new_session=True`）：关掉浏览器 / 重启 Web 进程
都不会中断正在跑的渲染，进度可以从日志文件恢复。

阶段链（可选）：
  1. lama_apply  —— 必须
  2. texture_fix —— 可选（`texture_mode != off`）
  3. rowfill     —— 可选（`fill_mode == rowfill`，纯渐变背景才用）
"""

import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from parse_video_py.wm_studio.paths import script, workspace_root

PROGRESS_RE = None  # 延迟编译，避免导入开销


@dataclass
class Job:
    task_id: str
    video: str
    out_video: str
    bbox: tuple
    params: dict
    stages: list = field(default_factory=list)
    created: float = 0.0
    pid: int = 0
    status: str = "pending"  # pending/running/done/failed/stopped
    stage_idx: int = 0
    log_path: str = ""
    work_dir: str = ""
    started: float = 0.0
    finished: float = 0.0
    message: str = ""

    def to_dict(self):
        d = asdict(self)
        d["elapsed"] = (
            round((self.finished or time.time()) - self.started, 1)
            if self.started
            else 0.0
        )
        return d


class JobManager:
    """单进程内的任务表 + 磁盘 JSON 持久化（Web 重启后可读回）。"""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._load_from_disk()

    # ---------- 持久化 ----------
    def _state_file(self) -> Path:
        return workspace_root() / "jobs.json"

    def _load_from_disk(self):
        f = self._state_file()
        if not f.is_file():
            return
        try:
            data = json.loads(f.read_text("utf-8"))
        except Exception:
            return
        for tid, d in data.items():
            try:
                d["bbox"] = tuple(d.get("bbox") or (0, 0, 0, 0))
                job = Job(**{k: v for k, v in d.items() if k in Job.__annotations__})
                # 上次退出时还在跑的，标成 running 但不持有进程句柄
                if job.status in ("pending", "running"):
                    job.message = "Web 进程曾重启，任务状态未知（看日志判断）"
                self._jobs[job.task_id] = job
            except Exception:
                continue

    def _save(self):
        f = self._state_file()
        try:
            f.write_text(
                json.dumps(
                    {k: v.to_dict() for k, v in self._jobs.items()},
                    ensure_ascii=False,
                    indent=1,
                ),
                "utf-8",
            )
        except Exception:
            pass

    # ---------- 创建 ----------
    def create(self, video: str, bbox, params: dict, out_video: str = "") -> Job:
        tid = time.strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        work = workspace_root() / tid
        work.mkdir(parents=True, exist_ok=True)

        if not out_video:
            stem = Path(video).stem
            out_video = str(work / f"{stem}_clean.mp4")

        job = Job(
            task_id=tid,
            video=str(video),
            out_video=str(out_video),
            bbox=tuple(bbox),
            params=dict(params),
            created=time.time(),
            work_dir=str(work),
            log_path=str(work / "run.log"),
        )
        self._jobs[tid] = job
        self._save()
        return job

    # ---------- 启动 ----------
    def start(self, tid: str) -> Job:
        job = self._jobs[tid]
        if job.status == "running":
            return job

        stages = self._build_stages(job)
        job.stages = [s[0] for s in stages]
        job.status = "running"
        job.started = time.time()
        Path(job.log_path).write_text("", "utf-8")

        # ★ 用 set -e + 逐阶段判退出码：任何阶段失败立刻停，并把退出码写进日志
        #   结尾标记。**不能只看 `=== ALL DONE ===` 这行**——那是 echo，只要脚本
        #   跑到末尾就会打印，中间命令全崩了也照打（实测踩过：lama_apply 抛
        #   ValueError 后任务被报成 done，成片根本不存在）。
        lines = ["#!/bin/bash", "set -o pipefail"]
        for i, (name, cmd) in enumerate(stages):
            lines.append(f"echo '=== stage: {name} ==='")
            lines.append(cmd)
            lines.append("rc=$?")
            lines.append(
                f'if [ $rc -ne 0 ]; then echo "=== STAGE_FAILED {name} rc=$rc '
                f'==="; echo "=== EXIT rc=$rc ==="; exit $rc; fi'
            )
        lines.append("echo '=== EXIT rc=0 ==='")
        script_path = Path(job.work_dir) / "run.sh"
        script_path.write_text("\n".join(lines) + "\n", "utf-8")
        script_path.chmod(0o755)

        logf = open(job.log_path, "ab", buffering=0)
        proc = subprocess.Popen(
            ["/bin/bash", str(script_path)],
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=job.work_dir,
        )
        self._procs[tid] = proc
        job.pid = proc.pid
        self._save()
        return job

    def _build_stages(self, job: Job) -> list[tuple[str, str]]:
        """组装阶段命令（每阶段一行 shell）。"""
        p = job.params
        x, y, w, h = (int(v) for v in job.bbox)
        stages = []

        lama = [
            sys.executable,
            "-u",
            str(script("lama_apply.py")),
            shlex.quote(job.video),
            str(x),
            str(y),
            str(w),
            str(h),
            "--out",
            shlex.quote(job.out_video),
            "--pad",
            str(int(p.get("pad", 10))),
            "--keyframe-every",
            str(int(p.get("keyframe_every", 1))),
            "--crf",
            str(int(p.get("crf", 17))),
            "--device",
            str(p.get("device", "auto")),
        ]
        if p.get("mask_path"):
            lama += ["--mask-file", shlex.quote(str(p["mask_path"]))]
        if p.get("start"):
            lama += ["--start", str(float(p["start"]))]
        if p.get("duration"):
            lama += ["--duration", str(float(p["duration"]))]
        stages.append(("lama_apply", " ".join(lama)))

        if p.get("texture_mode") and p.get("texture_mode") != "off":
            tx_out = job.out_video.replace(".mp4", "_tx.mp4")
            tx = [
                sys.executable,
                "-u",
                str(script("texture_fix.py")),
                shlex.quote(job.out_video),
                "--x",
                str(x),
                "--y",
                str(y),
                "--mask",
                shlex.quote(str(p.get("mask_path", ""))),
                "--out",
                shlex.quote(tx_out),
                "--strength",
                str(float(p.get("texture_strength", 1.3))),
                "--tile",
                str(int(p.get("texture_tile", 48))),
                "--lf-mode",
                str(p.get("texture_lf_mode", "off")),
                "--audio-from",
                shlex.quote(job.video),
                "--device",
                str(p.get("device", "auto")),
            ]
            stages.append(("texture_fix", " ".join(tx)))
            job.params["_tx_out"] = tx_out
        return stages

    # ---------- 查询 ----------
    def get(self, tid: str) -> Job | None:
        job = self._jobs.get(tid)
        if job is None:
            return None
        self._refresh(job)
        return job

    def list(self) -> list[Job]:
        out = []
        for job in sorted(self._jobs.values(), key=lambda j: j.created, reverse=True):
            self._refresh(job)
            out.append(job)
        return out

    def _refresh(self, job: Job):
        """用「日志里的退出码标记」+「进程状态」共同判定任务成败。

        ★ 判定优先级：
          1. 日志出现 `=== EXIT rc=0 ===`     → done（并核对成片是否存在）
          2. 日志出现 `=== EXIT rc=N ==`      → failed（N != 0）
          3. 进程已退出但没标记               → 按 returncode 兜底
          4. 进程还在                        → running
        """
        if job.status not in ("running", "pending"):
            return

        proc = self._procs.get(job.task_id)
        tail = self._tail(job.log_path)
        rc_mark = _parse_exit_mark(tail)
        stage_fail = _parse_failed_stage(tail)

        if rc_mark is not None:
            if rc_mark == 0:
                # rc=0 但要确认成片真的落盘了（防止"脚本正常结束但没产物"）
                if job.out_video and os.path.isfile(job.out_video):
                    job.status = "done"
                    job.message = "完成"
                else:
                    job.status = "failed"
                    job.message = "脚本退出码 0，但成片文件不存在（检查输出路径）"
            else:
                job.status = "failed"
                job.message = (
                    f"阶段 {stage_fail} 失败，退出码 {rc_mark}"
                    if stage_fail
                    else f"退出码 {rc_mark}"
                )
            job.finished = time.time()
            self._save()
            return

        if proc is not None and proc.poll() is not None:
            rc = proc.returncode
            if rc == 0 and job.out_video and os.path.isfile(job.out_video):
                job.status = "done"
                job.message = "完成（无退出标记，按进程状态判断）"
            elif rc == 0:
                job.status = "failed"
                job.message = "进程退出码 0，但成片不存在"
            else:
                job.status = "failed"
                job.message = f"进程退出码 {rc}（看日志尾部）"
            job.finished = time.time()
            self._save()
            return

        job.stage_idx = _count_stage_marks(tail)

    def _tail(self, path: str, max_bytes: int = 200_000) -> str:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def log(self, tid: str, offset: int = 0, limit: int = 40_000) -> dict:
        """增量读日志：offset 是上次读到的字节位置。"""
        job = self._jobs.get(tid)
        if job is None:
            return {"error": "任务不存在"}
        self._refresh(job)
        try:
            size = os.path.getsize(job.log_path)
            with open(job.log_path, "rb") as f:
                f.seek(max(0, offset))
                chunk = f.read(limit)
        except OSError:
            size, chunk = 0, b""
        text = chunk.decode("utf-8", "replace")
        return {
            "task_id": tid,
            "offset": offset + len(chunk),
            "size": size,
            "text": text,
            "status": job.status,
            "progress": _parse_progress(text)
            or _parse_progress(self._tail(job.log_path)),
        }

    # ---------- 控制 ----------
    def stop(self, tid: str) -> dict:
        job = self._jobs.get(tid)
        if job is None:
            return {"ok": False, "error": "任务不存在"}
        proc = self._procs.get(tid)
        killed = False
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                killed = True
            except Exception:
                try:
                    proc.terminate()
                    killed = True
                except Exception:
                    pass
        elif job.pid:
            # Web 重启后没有句柄，按 pid 杀整个进程组
            try:
                os.killpg(job.pid, signal.SIGTERM)
                killed = True
            except Exception:
                pass
        job.status = "stopped"
        job.finished = time.time()
        job.message = "已手动停止"
        self._save()
        return {"ok": True, "killed": killed}


def _parse_exit_mark(text: str) -> int | None:
    """从日志找 `=== EXIT rc=N ===`，返回 N；找不到返回 None。"""
    import re

    last = None
    for m in re.finditer(r"=== EXIT rc=(-?\d+) ===", text):
        last = m
    return int(last.group(1)) if last else None


def _parse_failed_stage(text: str) -> str:
    """从日志找 `=== STAGE_FAILED <name> rc=N ===` 的阶段名。"""
    import re

    last = None
    for m in re.finditer(r"=== STAGE_FAILED (\S+) rc=", text):
        last = m
    return last.group(1) if last else ""


def _count_stage_marks(text: str) -> int:
    """数 `=== stage: xxx ===` 出现次数 → 当前阶段索引。"""
    n = sum(1 for line in text.splitlines() if line.startswith("=== stage: "))
    return max(0, n - 1)


def _parse_progress(text: str) -> dict | None:
    """从 lama_apply 的进度行解析出帧数/耗时。

    格式：`    150 frames  33s  0.22s/frame`
    """
    import re

    last = None
    for m in re.finditer(r"(\d+)\s+frames\s+(\d+)s\s+([\d.]+)s/frame", text):
        last = m
    if not last:
        return None
    return {
        "frames": int(last.group(1)),
        "seconds": int(last.group(2)),
        "sec_per_frame": float(last.group(3)),
    }


# 全局单例
_manager: JobManager | None = None


def manager() -> JobManager:
    global _manager
    if _manager is None:
        _manager = JobManager()
    return _manager

#!/usr/bin/env python
"""用真实视频文件跑一遍「签发 → 浏览器直传 OSS → 回读校验」。

与 e2e_oss.py 的区别：
  · e2e_oss.py 用 1KB 假字节验状态机；
  · 本脚本用**真实 mp4**（默认 demo/weibo-1027.mp4，约 38MB）走完整 HTTP 链路，
    并把文件原样下载回来做 MD5 比对——验证大文件上传、断点无关的完整性、签名下载。

跑法：
    .venv/bin/python scripts/oss_upload_demo.py                    # 默认文件
    .venv/bin/python scripts/oss_upload_demo.py --file demo/x.mp4  # 指定文件
    .venv/bin/python scripts/oss_upload_demo.py --keep             # 保留对象便于控制台查看

前置：
  · dev server 在 127.0.0.1:8000 跑着（脚本走真 HTTP，不绕 WSGI）；
  · 数据库用 .env 里的 MySQL（config.settings.dev），**不碰本地 3307**；
  · OSS 配置齐全（OSS_BUCKET / AK / SK）。

安全边界：探针用户手机号 139 开头 + 时间戳，跑完删任务/用户/对象/令牌。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

CLOUD_WEB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CLOUD_WEB))
sys.path.insert(0, str(CLOUD_WEB / "scripts"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

import _oss_probe as probe  # noqa: E402
import requests  # noqa: E402
from apps.accounts.models import AuthToken, User  # noqa: E402
from apps.billing.models import PointLedger, PointReason  # noqa: E402
from apps.tasks.models import Task  # noqa: E402
from apps.tasks.services import oss as oss_service  # noqa: E402
from django.conf import settings  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, YELLOW = "\033[32m", "\033[31m", "\033[33m"

PASSED: list[str] = []
CREATED_KEYS: list[str] = []
CREATED_TASK_ID: str | None = None
CREATED_USER: User | None = None
CREATED_TOKEN: AuthToken | None = None


def step(name: str, detail: str = "") -> None:
    line = f"  {GREEN}✅{RESET} {name}" + (f"  {DIM}→ {detail}{RESET}" if detail else "")
    PASSED.append(name)
    print(line, flush=True)


def fail(name: str, detail: str) -> None:
    print(f"  {RED}❌ {name}{RESET}\n     {detail}", flush=True)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n}B"


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def probe_video(path: Path) -> dict:
    """用 ffprobe 读时长/分辨率/帧率；没装 ffprobe 就返回 0（服务端照收）。"""
    import json
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        return {"duration_ms": 0, "width": 0, "height": 0, "fps": 0}
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    stream = (info.get("streams") or [{}])[0]
    num, _, den = (stream.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0
    return {
        "duration_ms": int(float(info.get("format", {}).get("duration") or 0) * 1000),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": round(fps, 3),
    }


def api_health() -> None:
    try:
        r = requests.get("http://127.0.0.1:8000/healthz", timeout=5)
    except requests.RequestException as exc:
        raise SystemExit(
            "dev server 没起来（127.0.0.1:8000）。先启动：\n"
            "  cd cloud_web && DJANGO_SETTINGS_MODULE=config.settings.dev "
            ".venv/bin/python manage.py runserver 127.0.0.1:8000 --noreload"
        ) from exc
    if r.status_code != 200:
        raise SystemExit(f"healthz 异常 HTTP {r.status_code}")


def make_probe_user() -> str:
    """建一个带积分的探针用户，返回 Bearer 明文。"""
    global CREATED_USER, CREATED_TOKEN
    stamp = str(int(time.time()))[-8:]
    user = User.objects.create_user(
        username=f"ossprobe_{stamp}", phone=f"139{stamp}", nickname="oss-probe"
    )
    PointLedger.objects.create(
        user=user, delta=10, balance_after=10, reason=PointReason.SIGN_IN
    )
    token, raw = AuthToken.issue(user)
    CREATED_USER, CREATED_TOKEN = user, token
    step("创建探针用户 + 积分 + 令牌", f"phone={user.phone} user_no={user.user_no}")
    return raw


def upload_sign(raw_token: str, size: int) -> dict:
    r = requests.post(
        f"{BASE}/uploads/sign",
        json={"size": size},
        headers={"Authorization": f"Bearer {raw_token}"},
        timeout=30,
    )
    if r.status_code != 200:
        raise SystemExit(f"sign 失败 HTTP {r.status_code}: {r.text[:400]}")
    data = r.json()
    policy = data["upload"]
    assert policy["host"].startswith("https://"), policy
    assert "internal" not in policy["host"], "host 不能是内网 endpoint"
    CREATED_KEYS.append(policy["key"])
    global CREATED_TASK_ID
    CREATED_TASK_ID = data["task_id"]
    step(
        "POST /uploads/sign",
        f"task_no={data['task_no']} host={policy['host']} key={policy['key']}",
    )
    return data


def direct_upload(policy: dict, path: Path, content: bytes) -> float:
    body, ctype = probe.multipart_body(
        {
            "key": policy["key"],
            "policy": policy["policy"],
            "OSSAccessKeyId": policy["accessid"],
            "signature": policy["signature"],
        },
        path.name,
        content,
        content_type="video/mp4",
    )
    t0 = time.time()
    r = requests.post(
        policy["host"], data=body, headers={"Content-Type": ctype}, timeout=600
    )
    elapsed = time.time() - t0
    if r.status_code not in (200, 204):
        raise SystemExit(f"直传失败 HTTP {r.status_code}: {r.text[:600]}")
    speed = len(content) / 1024 / 1024 / max(elapsed, 0.01)
    step(
        "浏览器直传 OSS（真 multipart，38MB 级）",
        f"HTTP {r.status_code} 耗时 {elapsed:.1f}s ≈ {speed:.1f}MB/s",
    )
    return elapsed


def confirm(raw_token: str, task_id: str, duration_ms: int, meta: dict) -> None:
    r = requests.post(
        f"{BASE}/tasks/{task_id}/confirm",
        json={"duration_ms": duration_ms, **meta},
        headers={"Authorization": f"Bearer {raw_token}"},
        timeout=60,
    )
    if r.status_code == 200:
        data = r.json()
        step(
            "POST /tasks/{id}/confirm（服务端真查 OSS）",
            f"status={data['status']} 扣分={data['points_cost']}",
        )
        return
    # 422 且 code=duration_exceeded 说明「对象存在性检查已通过」，时长策略正常拦截
    body = r.json() if r.content else {}
    if r.status_code == 422 and body.get("code") == "duration_exceeded":
        step(
            "POST /tasks/{id}/confirm（服务端真查 OSS 通过）",
            f"已被时长策略拦下：{body.get('message')}",
        )
        return
    raise SystemExit(f"confirm 失败 HTTP {r.status_code}: {r.text[:400]}")


def verify_object(key: str, content: bytes) -> None:
    """HEAD 对象 + 签名 URL 全量下载，双重校验。"""
    if not oss_service.object_exists(key):
        raise SystemExit("服务端查不到刚上传的对象")
    size = oss_service.object_size(key)
    if size != len(content):
        raise SystemExit(f"OSS 对象大小不一致：{size} vs {len(content)}")
    step("HEAD 对象（服务端 SDK 视角）", f"size={human(size)}")

    url = oss_service.presign_get(key)
    assert url.startswith("https://"), f"签名 URL 不是 https：{url[:80]}"
    t0 = time.time()
    r = requests.get(url, timeout=600)
    elapsed = time.time() - t0
    if r.status_code != 200:
        raise SystemExit(f"签名下载 HTTP {r.status_code}: {r.text[:300]}")
    got = r.content
    if len(got) != len(content):
        raise SystemExit(f"下载长度不一致 {len(got)} vs {len(content)}")
    if md5_of(got) != md5_of(content):
        raise SystemExit("下载内容 MD5 与本地文件不一致（数据损坏）")
    step(
        "签名 URL 全量下载 + MD5 比对",
        f"HTTP 200 {human(len(got))} {elapsed:.1f}s md5={md5_of(got)[:12]}… 一致",
    )


def cleanup(keep: bool) -> None:
    if keep:
        print(f"\n{YELLOW}--keep 已指定：对象保留在桶里{RESET}")
        for key in CREATED_KEYS:
            print(f"    oss://{settings.OSS_BUCKET}/{key}")
    else:
        for key in CREATED_KEYS:
            try:
                oss_service.delete_object(key)
            except Exception as exc:  # noqa: BLE001 清理失败不该盖住主结果
                print(f"    清理对象失败 {key}: {exc}")
        if CREATED_KEYS:
            step("清理 OSS 对象", f"{len(CREATED_KEYS)} 个")
    if CREATED_TASK_ID:
        Task.objects.filter(pk=CREATED_TASK_ID).delete()
    if CREATED_TOKEN:
        CREATED_TOKEN.delete()
    if CREATED_USER:
        CREATED_USER.delete()
    step("清理探针任务/令牌/用户", "调试库不留脏数据")


def main() -> int:
    parser = argparse.ArgumentParser(description="真实视频文件 OSS 上传冒烟")
    parser.add_argument(
        "--file",
        default=str(CLOUD_WEB.parent / "demo" / "weibo-1027.mp4"),
        help="要上传的视频文件",
    )
    parser.add_argument("--duration-ms", type=int, default=0, help="视频时长（毫秒）")
    parser.add_argument("--keep", action="store_true", help="保留 OSS 对象")
    args = parser.parse_args()

    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"文件不存在：{path}")
    content = path.read_bytes()
    meta = probe_video(path)
    duration_ms = args.duration_ms or meta["duration_ms"]

    db = settings.DATABASES["default"]
    print("=" * 68)
    print(f"{BOLD}真实视频 OSS 上传冒烟{RESET}")
    print(f"  文件   {path.name}  {human(len(content))}  md5={md5_of(content)[:12]}…")
    print(
        f"  视频   {meta['width']}x{meta['height']} {meta['fps']}fps "
        f"{duration_ms / 1000:.1f}s"
    )
    print(f"  数据库 {db['HOST']}:{db['PORT']}/{db['NAME']}  {DIM}（.env 配置）{RESET}")
    print(f"  OSS    {settings.OSS_BUCKET} @ {oss_service.endpoint_host()}")
    print("=" * 68)

    try:
        api_health()
        step("dev server 存活", "GET /healthz 200")
        session_token = make_probe_user()
        signed = upload_sign(session_token, len(content))
        direct_upload(signed["upload"], path, content)
        if duration_ms > 0:
            confirm(
                session_token,
                signed["task_id"],
                duration_ms,
                {k: meta[k] for k in ("width", "height", "fps")},
            )
        else:
            print(f"  {DIM}（没有时长信息，跳过 confirm 一步）{RESET}")
        verify_object(signed["upload"]["key"], content)
    except SystemExit as exc:
        fail("链路中断", str(exc))
        cleanup(args.keep)
        return 1

    print(f"\n{GREEN}{BOLD}全部 {len(PASSED)} 项通过{RESET}")
    cleanup(args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

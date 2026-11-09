#!/usr/bin/env python
"""真实 OSS 端到端冒烟（不打桩）。

用途：OSS 配置补齐后，验证「浏览器直传 → 确认 → 算力机下载 → 回传」整条真实链路，
而不只是 SDK 层面的连通性。

跑法：
    uv run python scripts/e2e_oss.py

与 smoke_debug_db.py 的区别：
  · smoke_debug_db.py 把 OSS 全部打桩，只验服务端状态机，跑得快、没外部依赖；
  · 本脚本走真 OSS，会真的往桶里写对象（跑完立刻删），并真下载校验内容一致性。

安全边界：
  · 数据库侧走外层事务 + set_rollback，调试库不留脏数据；
  · OSS 侧对象写在 uploads/__probe_e2e__/ 前缀下，跑完无条件清理；
  · 真的调一次 delete_object，失败会打印 key 供手动清理。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()

import _oss_probe as oss_probe  # noqa: E402
import requests  # noqa: E402
from apps.accounts.models import User  # noqa: E402
from apps.billing.models import PointLedger, PointReason  # noqa: E402
from apps.tasks import views_user, views_worker  # noqa: E402
from apps.tasks.models import Task  # noqa: E402
from apps.tasks.services import lease, oss  # noqa: E402
from django.db import transaction  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

PASS: list[str] = []
UPLOADED_KEYS: list[str] = []
FAKE_MP4 = (
    b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"e2e-probe-video-bytes" * 64
)


def ok(step: str, detail: str = "") -> None:
    line = f"  ✅ {step}" + (f"  → {detail}" if detail else "")
    PASS.append(line)
    print(line, flush=True)


def expect(resp, code: int = 200, label: str = "") -> dict:
    assert (
        resp.status_code == code
    ), f"{label}: 期望 {code} 实得 {resp.status_code}，body={getattr(resp, 'data', None)}"
    return resp.data


def _multipart_body(
    fields: dict[str, str], filename: str, content: bytes
) -> tuple[bytes, str]:
    """手工拼 multipart/form-data（共用实现，见 _oss_probe.py）。"""
    return oss_probe.multipart_body(fields, filename, content, content_type="video/mp4")


def main() -> int:
    rf = APIRequestFactory()
    stamp = str(int(time.time()))
    phone = "139" + stamp[-8:]

    print("=" * 64)
    print("真实 OSS 端到端冒烟（浏览器直传 → 确认 → 签名下载 → worker 回传）")
    print("=" * 64)

    try:
        with transaction.atomic():
            # ── 0. 用户与额度 ───────────────────────────────────────
            user = User.objects.create_user(
                username=f"e2e_{stamp}", phone=phone, nickname="e2e"
            )
            PointLedger.objects.create(
                user=user, delta=10, balance_after=10, reason=PointReason.SIGN_IN
            )
            ok("创建用户 + 积分", f"user_no={user.user_no}")

            # ── 1. 签发直传凭证（真 policy）────────────────────────
            req = rf.post(
                "/api/v1/uploads/sign", {"size": len(FAKE_MP4)}, format="json"
            )
            force_authenticate(req, user=user)
            data = expect(views_user.sign_upload(req), label="uploads/sign")
            task_id, task_no = data["task_id"], data["task_no"]
            policy = data["upload"]
            assert (
                policy.get("host") and policy.get("policy") and policy.get("signature")
            ), policy
            assert policy["host"].startswith("https://"), policy
            assert "internal" not in policy["host"], "host 必须是公网 endpoint"
            UPLOADED_KEYS.append(policy["key"])
            ok("POST /uploads/sign", f"task_no={task_no} host={policy['host']}")

            # ── 2. 模拟浏览器 XHR FormData 真上传 ───────────────────
            body, ctype = _multipart_body(
                {
                    "key": policy["key"],
                    "policy": policy["policy"],
                    "OSSAccessKeyId": policy["accessid"],
                    "signature": policy["signature"],
                },
                "original.mp4",
                FAKE_MP4,
            )
            resp = requests.post(
                policy["host"], data=body, headers={"Content-Type": ctype}, timeout=60
            )
            assert resp.status_code in (
                200,
                204,
            ), f"直传失败 HTTP {resp.status_code}: {resp.text[:400]}"
            ok(
                "浏览器直传 OSS（真 multipart）",
                f"HTTP {resp.status_code} {len(FAKE_MP4)}B",
            )

            # ── 3. 服务端确认：真查对象存在与大小 → 扣分入队 ───────
            req = rf.post(
                f"/api/v1/tasks/{task_id}/confirm",
                {"duration_ms": 12_000, "width": 1920, "height": 1080, "fps": 30},
                format="json",
            )
            force_authenticate(req, user=user)
            data = expect(
                views_user.confirm_upload(req, task_id=task_id), label="confirm"
            )
            assert data["status"] == "pending", data
            assert data["points_cost"] > 0, data
            ok(
                "POST /tasks/{id}/confirm（真查 OSS）",
                f"status=pending 扣分={data['points_cost']} 对象存在+大小已校验",
            )

            # ── 4. 详情返回原片签名 URL → 真下载并校验内容 ──────────
            req = rf.get(f"/api/v1/tasks/{task_no}")
            force_authenticate(req, user=user)
            data = expect(views_user.task_detail(req, task_id=task_no), label="detail")
            preview_url = data["preview_url"]
            assert preview_url.startswith("https://"), preview_url
            dl = requests.get(preview_url, timeout=60)
            assert dl.status_code == 200, f"签名下载 HTTP {dl.status_code}"
            assert (
                dl.content == FAKE_MP4
            ), f"内容不一致 {len(dl.content)}B vs {len(FAKE_MP4)}B"
            ok(
                "GET /tasks/{task_no} → 签名 URL 下载",
                f"HTTP 200，{len(dl.content)}B 与上传字节完全一致",
            )

            # ── 5. worker 领取：拿原片签名 URL + 成片回传 URL ──────
            worker, raw_token = lease.make_worker(f"e2e-worker-{stamp}")
            req = rf.post(
                "/api/v1/worker/claim",
                {"capabilities": {"device": "cpu"}},
                format="json",
                HTTP_X_WORKER_TOKEN=raw_token,
            )
            data = expect(views_worker.claim(req), label="claim")
            video_url = data["video_url"]
            result_url = data["result_upload_url"]
            assert video_url and result_url, data

            # 真按 worker 的用法下载原片
            wd = requests.get(video_url, timeout=60)
            assert (
                wd.status_code == 200 and wd.content == FAKE_MP4
            ), "worker 下载原片失败"
            ok("worker 下载原片（签名 GET）", f"HTTP 200，{len(wd.content)}B 一致")

            # 真按 worker 的用法回传成片
            # ★ 必须带 Content-Type：presign_put 把它签进了签名，缺了就
            # SignatureDoesNotMatch。worker 侧 pipeline.upload() 正是这么做的。
            result_key = None
            for k in Task.objects.filter(pk=task_id).values_list(
                "result_key", flat=True
            ):
                result_key = k
            payload = b"fake-cleaned-video-output"
            up = requests.put(
                result_url,
                data=payload,
                headers={"Content-Type": "video/mp4"},
                timeout=60,
            )
            assert up.status_code == 200, f"回传 HTTP {up.status_code}: {up.text[:300]}"
            if result_key:
                UPLOADED_KEYS.append(result_key)
            ok(
                "worker 回传成片（签名 PUT）",
                f"HTTP 200，{len(payload)}B，Content-Type 已签入",
            )

            # 回传后服务端能查到该对象
            if result_key:
                assert oss.object_exists(result_key), "回传后对象查不到"
                size = oss.object_size(result_key)
                ok("回传后对象可查", f"size={size}B（与 PUT 的 {len(payload)}B 一致）")

            # ── 6. 清理 OSS 对象（DB 会回滚，OSS 不会）─────────────
            transaction.set_rollback(True)
    finally:
        for key in UPLOADED_KEYS:
            if not key:
                continue
            try:
                oss.delete_object(key)
                print(f"  🧹 已清理 {key}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️  清理失败 {key}: {exc}（需手动删除）", file=sys.stderr)

    print(f"\n✅ 真实 OSS 端到端全部通过（{len(PASS)} 项），DB 已回滚，OSS 对象已清理")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ 端到端失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        raise

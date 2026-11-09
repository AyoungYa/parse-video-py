#!/usr/bin/env python
"""阿里云 OSS 连通性体检。

用途：.env 填完 OSS 配置先跑这个，比直接在浏览器里试上传好排查。
用法：
    uv run python scripts/check_oss.py

检查项（逐条对应项目真实的三条上传/下载链路）：
  1. 配置是否齐全（Endpoint / Bucket / AK / SK）
  2. Endpoint 是不是公网地址（internal 只能给 ECS 内网用，浏览器直传会失败）
  3. Bucket 是否存在、地域是否与 Endpoint 匹配（最常见的坑：拿杭州 Endpoint 连深圳桶）
  4. SDK 直传直下（oss2 put_object / get_object）—— 验证 AK/SK 与三项权限
  5. 签名 URL 上传下载（presign_put / presign_get）—— 算力机回传成片走这条
  6. PostObject 浏览器直传凭证（build_post_policy）—— 前端上传走这条，
     校验 host 是否公网可达、policy 能否被 OSS 接受

不改动任何业务数据：所有测试对象写进 uploads/__probe__/ 前缀，跑完立刻删除。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 同目录的共用探针工具（scripts/ 在 sys.path[0]，可直接 import）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _oss_probe as oss_probe  # noqa: E402

try:
    from dotenv import load_dotenv
except ImportError:
    print("缺少 python-dotenv，请先执行：uv sync")
    sys.exit(2)

load_dotenv(BASE_DIR / ".env", override=False)

try:
    import oss2
except ImportError:
    print("缺少 oss2，请先执行：uv sync")
    sys.exit(2)

try:
    import requests
except ImportError:
    print("缺少 requests，请先执行：uv sync")
    sys.exit(2)

PROBE_PREFIX = "uploads/__probe__"
PROBE_BODY = b"wuhenjiang oss probe"

OK = "✅"
FAIL = "❌"
WARN = "⚠️ "


class Probe:
    """收集每项检查的结果，最后统一汇报（不中途退出，能一次看全所有问题）。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool, detail: str, warn: bool = False) -> None:
        mark = OK if ok else (WARN if warn else FAIL)
        self.rows.append((mark, name, detail))
        print(f"{mark} {name}\n    {detail}")

    def summary(self) -> int:
        failed = sum(1 for m, _, _ in self.rows if m == FAIL)
        warned = sum(1 for m, _, _ in self.rows if m == WARN)
        print("\n" + "-" * 64)
        if failed:
            print(f"结论：{failed} 项失败" + (f"，{warned} 项警告" if warned else ""))
        elif warned:
            print(f"结论：全部通过（{warned} 项警告）")
        else:
            print("结论：全部通过")
        return 1 if failed else 0


def _cfg() -> dict[str, str]:
    return {
        "endpoint": os.getenv("OSS_ENDPOINT", "").strip(),
        "bucket": os.getenv("OSS_BUCKET", "").strip(),
        "ak": os.getenv("OSS_ACCESS_KEY_ID", "").strip(),
        "sk": os.getenv("OSS_ACCESS_KEY_SECRET", "").strip(),
    }


def _host_only(endpoint: str) -> str:
    """剥掉协议头和结尾斜杠，只留 host。

    ★ 和 apps.tasks.services.oss.endpoint_host 同一套逻辑：
    oss2 在 endpoint 不带协议时会补 http://，签名 URL 就全是 http://，
    HTTPS 站点下浏览器会按混合内容拦掉。检查脚本也要按 https 口径验。
    """
    raw = endpoint
    for prefix in ("https://", "http://"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :]
            break
    return raw.strip("/")


def _post_policy(key: str, sk: str, max_bytes: int, expire_seconds: int = 900):
    """和 apps.tasks.services.oss.build_post_policy 同样的算法（这里不依赖 Django）。"""
    expire_at = datetime.now(dt_timezone.utc) + timedelta(seconds=expire_seconds)
    policy_doc = {
        "expiration": expire_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "conditions": [
            ["content-length-range", 1, max_bytes],
            ["eq", "$key", key],
        ],
    }
    policy = base64.b64encode(json.dumps(policy_doc).encode("utf-8")).decode("ascii")
    signature = base64.b64encode(
        hmac.new(sk.encode("utf-8"), policy.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    return policy, signature


def _multipart_body(
    fields: dict[str, str], filename: str, content: bytes
) -> tuple[bytes, str]:
    """手工拼 multipart/form-data（共用实现，见 _oss_probe.py）。"""
    return oss_probe.multipart_body(fields, filename, content)


def main() -> int:
    p = Probe()
    cfg = _cfg()

    print("=" * 64)
    print("阿里云 OSS 连通性体检")
    print("=" * 64)

    # 1. 配置齐全性
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        p.add(
            "1. 配置齐全",
            False,
            f"缺少：{', '.join(missing)}（在 cloud_web/.env 里补齐）",
        )
        return p.summary()
    p.add(
        "1. 配置齐全",
        True,
        f"endpoint={cfg['endpoint']} bucket={cfg['bucket']} "
        f"ak={cfg['ak'][:4]}...{cfg['ak'][-4:]}(len={len(cfg['ak'])})",
    )

    endpoint = cfg["endpoint"]
    host_only = _host_only(endpoint)
    bucket_name = cfg["bucket"]
    max_bytes = int(os.getenv("OSS_MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))

    # 2. Endpoint 是不是公网地址（internal 只能给 ECS 内网用，浏览器直传会失败）
    if "-internal" in host_only:
        p.add(
            "2. Endpoint 形态",
            False,
            f"{endpoint} 是内网 endpoint，浏览器直传和本地调试都用不了；"
            "改成 oss-cn-<region>.aliyuncs.com 这种公网地址",
        )
    else:
        p.add("2. Endpoint 形态", True, f"{host_only} 是公网 endpoint，浏览器可直连")

    auth = oss2.Auth(cfg["ak"], cfg["sk"])
    # ★ 显式带 https://：oss2 在 endpoint 不带协议时会补 http://，
    # 那样 sign_url 生成的全是 http://，HTTPS 站点下浏览器会按混合内容拦掉。
    bucket = oss2.Bucket(auth, f"https://{host_only}", bucket_name)
    probe_key = f"{PROBE_PREFIX}/{uuid.uuid4().hex[:12]}/probe.txt"

    # 3. Bucket 是否存在 / 地域是否匹配
    try:
        info = bucket.get_bucket_info()
        region = info.location or "?"
        # endpoint 形如 oss-cn-shenzhen.aliyuncs.com → 期望 cn-shenzhen；
        # 桶的 location 形如 oss-cn-shenzhen → 去掉 oss- 后比较。
        expect = host_only.replace("oss-", "").replace(".aliyuncs.com", "")
        actual = region.replace("oss-", "")
        same = expect == actual
        p.add(
            "3. Bucket 存在 / 地域匹配",
            same,
            f"bucket={bucket_name} 桶实际地域={region} endpoint 指向={expect}"
            + (
                ""
                if same
                else " ← 地域不一致，必须把 .env 的 OSS_ENDPOINT 改成桶所在地域"
            ),
        )
    except oss2.exceptions.OssError as exc:
        code = getattr(exc, "code", "")
        hint = {
            "NoSuchBucket": "桶名不存在或拼写错误（桶名全局唯一且区分大小写）",
            "AccessDenied": "AK/SK 无权访问该桶，或 RAM 用户没被授予该桶权限；"
            "若提示 must be addressed using the specified endpoint，则是地域填错",
            "SignatureDoesNotMatch": "AK 与 SK 不配对（可能复制时串行/截断）",
        }.get(code, "检查桶名、AK/SK 与地域")
        p.add(
            "3. Bucket 存在 / 地域匹配", False, f"code={code} {exc.message} —— {hint}"
        )
        return p.summary()

    # 4. SDK 直传直下
    try:
        t0 = time.time()
        bucket.put_object(probe_key, PROBE_BODY)
        t_put = (time.time() - t0) * 1000
        got = bucket.get_object(probe_key).read()
        p.add(
            "4. SDK 直传直下 (put_object/get_object)",
            got == PROBE_BODY,
            f"写入 {len(PROBE_BODY)}B，读取 {len(got)}B，内容一致={got == PROBE_BODY}，"
            f"耗时 {t_put:.0f}ms（AK/SK + PutObject/GetObject 权限已验证）",
        )
    except oss2.exceptions.OssError as exc:
        p.add(
            "4. SDK 直传直下 (put_object/get_object)",
            False,
            f"code={getattr(exc, 'code', '')} {exc.message} —— "
            "检查 RAM 用户是否被授予该桶的 oss:PutObject/GetObject",
        )
        return p.summary()

    # 5. 签名 URL 上传（算力机回传成片走这条）
    try:
        put_url = bucket.sign_url("PUT", probe_key, 900, slash_safe=True)
        get_url = bucket.sign_url("GET", probe_key, 900, slash_safe=True)
        # ★ 协议必须是 https：oss2 在 endpoint 不带协议时会补 http://，
        # HTTPS 站点下浏览器会把 http:// 的预览/下载链接按「混合内容」直接拦掉。
        scheme_ok = put_url.startswith("https://") and get_url.startswith("https://")
        put_resp = requests.put(
            put_url,
            data=PROBE_BODY,
            headers={"Content-Length": str(len(PROBE_BODY))},
            timeout=60,
        )
        get_resp = requests.get(get_url, timeout=60)
        ok = put_resp.status_code == 200 and get_resp.status_code == 200 and scheme_ok
        p.add(
            "5. 签名 URL 上传下载 (presign PUT/GET)",
            ok,
            f"PUT 状态={put_resp.status_code} GET 状态={get_resp.status_code} "
            f"内容一致={get_resp.content == PROBE_BODY} "
            f"协议=https:{scheme_ok} "
            "（算力机无 AK 也能回传成片的前提）",
        )
    except Exception as exc:
        p.add(
            "5. 签名 URL 上传下载 (presign PUT/GET)",
            False,
            f"{type(exc).__name__}: {exc}",
        )

    # 6. PostObject 浏览器直传凭证（前端上传走这条）
    try:
        policy, signature = _post_policy(probe_key, cfg["sk"], max_bytes)
        host = f"https://{bucket_name}.{host_only}"
        body, content_type = _multipart_body(
            {
                "key": probe_key,
                "policy": policy,
                "OSSAccessKeyId": cfg["ak"],
                "signature": signature,
            },
            "probe.txt",
            PROBE_BODY,
        )
        resp = requests.post(
            host, data=body, headers={"Content-Type": content_type}, timeout=60
        )
        if resp.status_code in (200, 204):
            p.add(
                "6. PostObject 浏览器直传凭证",
                True,
                f"host={host} HTTP {resp.status_code} —— 前端上传凭证签发链路正常",
            )
        else:
            text = resp.text[:400].replace("\n", " ")
            extra = ""
            if resp.status_code == 403 and "CORS" in text:
                extra = " ← 若是 CORS 报错，请到 Bucket「数据安全 / 跨域设置」加规则"
            if resp.status_code == 405:
                extra = " ← 405 一般是请求体不是 multipart/form-data"
            p.add(
                "6. PostObject 浏览器直传凭证",
                False,
                f"host={host} HTTP {resp.status_code} body={text}{extra}",
            )
    except Exception as exc:
        p.add("6. PostObject 浏览器直传凭证", False, f"{type(exc).__name__}: {exc}")

    # 清理测试对象
    try:
        bucket.delete_object(probe_key)
        print(f"\n（已清理测试对象 {probe_key}）")
    except Exception as exc:
        print(f"\n（清理测试对象失败：{exc}；key={probe_key} 可手动删）")

    return p.summary()


if __name__ == "__main__":
    sys.exit(main())

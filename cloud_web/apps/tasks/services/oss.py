"""阿里云 OSS 适配层。

三条铁律（对应架构设计 §七）：
  1. AK/SK 只出现在云端进程内存里，绝不下发给浏览器或算力机；
  2. 浏览器拿到的是 PostObject policy —— 只能往**当前任务的前缀**写、限大小、15 分钟过期；
  3. 算力机拿到的是**按对象签发**的 GET 签名 URL，同样 15 分钟过期。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings

logger = logging.getLogger(__name__)


class OssNotConfigured(RuntimeError):
    """OSS 未配置（本地开发常见）。"""


def _require_config() -> None:
    missing = [
        name
        for name, value in {
            "OSS_BUCKET": settings.OSS_BUCKET,
            "OSS_ACCESS_KEY_ID": settings.OSS_ACCESS_KEY_ID,
            "OSS_ACCESS_KEY_SECRET": settings.OSS_ACCESS_KEY_SECRET,
        }.items()
        if not value
    ]
    if missing:
        raise OssNotConfigured(f"OSS 配置缺失：{', '.join(missing)}")


def endpoint_host() -> str:
    """归一化 endpoint：去掉协议头和结尾斜杠，只留 host。

    ★ 必须自己剥掉协议：oss2 的 _normalize_endpoint 在 endpoint 没写协议时会补
    **http://**，于是 sign_url 生成的下载/上传链接全是 http://。站点是 HTTPS 时
    浏览器会按「混合内容」直接拦掉预览和下载 —— 生产必炸。
    所以这里统一强制 https，不依赖 .env 里有没有写协议。
    """
    raw = (settings.OSS_ENDPOINT or "").strip()
    for prefix in ("https://", "http://"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :]
            break
    return raw.strip("/")


def _bucket():
    """延迟导入 oss2，避免本地无依赖时 import 失败。"""
    import oss2

    _require_config()
    auth = oss2.Auth(settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET)
    # 显式带 https://，避免 oss2 把无协议 endpoint 补成 http://
    return oss2.Bucket(auth, f"https://{endpoint_host()}", settings.OSS_BUCKET)


def upload_key(task_id: str) -> str:
    return f"{settings.OSS_UPLOAD_PREFIX}{task_id}/original.mp4"


def mask_key(task_id: str) -> str:
    return f"{settings.OSS_UPLOAD_PREFIX}{task_id}/mask.png"


def keyframe_key(task_id: str, index: int = 0) -> str:
    return f"{settings.OSS_UPLOAD_PREFIX}{task_id}/keyframe-{index}.jpg"


def result_key(task_id: str) -> str:
    return f"{settings.OSS_RESULT_PREFIX}{task_id}/out.mp4"


def report_key(task_id: str) -> str:
    return f"{settings.OSS_RESULT_PREFIX}{task_id}/report.json"


def build_post_policy(task_id: str, key: str) -> dict:
    """生成浏览器直传 OSS 用的 PostObject 凭证。

    policy 里用 content-length-range 卡住大小、用 eq 卡住 key（=只能写本任务前缀），
    这是防「拿到凭证随便传」的关键。刻意不约束 Content-Type：浏览器对 mp4/mov/mkv
    给出的 MIME 五花八门，硬卡会把正常上传挡掉，体积和 key 已经足够。
    """
    _require_config()
    expire_at = datetime.now(dt_timezone.utc) + timedelta(
        seconds=settings.OSS_SIGN_EXPIRE_SECONDS
    )
    policy_doc = {
        "expiration": expire_at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "conditions": [
            ["content-length-range", 1, settings.OSS_MAX_UPLOAD_BYTES],
            ["eq", "$key", key],
        ],
    }
    policy = base64.b64encode(json.dumps(policy_doc).encode("utf-8")).decode("ascii")
    signature = base64.b64encode(
        hmac.new(
            settings.OSS_ACCESS_KEY_SECRET.encode("utf-8"),
            policy.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")

    return {
        "host": f"https://{settings.OSS_BUCKET}.{endpoint_host()}",
        "accessid": settings.OSS_ACCESS_KEY_ID,
        "policy": policy,
        "signature": signature,
        "key": key,
        "expire_at": expire_at.isoformat(),
        "max_bytes": settings.OSS_MAX_UPLOAD_BYTES,
    }


def presign_get(key: str, expire_seconds: int | None = None) -> str:
    """签发一个只读、单对象、短时效的 URL。"""
    expire = expire_seconds or settings.OSS_SIGN_EXPIRE_SECONDS
    return _bucket().sign_url("GET", key, expire, slash_safe=True)


def presign_put(
    key: str, expire_seconds: int | None = None, content_type: str = ""
) -> str:
    """签发一个只写、单对象、短时效的 URL，给算力机回传成片用。

    这样算力机不需要持有 OSS AK/SK —— 它只能往这一个 key 写，且 URL 会过期。
    """
    expire = (
        expire_seconds or settings.OSS_SIGN_EXPIRE_SECONDS * 4
    )  # 大文件上传给足时间
    headers = {"Content-Type": content_type} if content_type else None
    return _bucket().sign_url("PUT", key, expire, headers=headers, slash_safe=True)


def object_exists(key: str) -> bool:
    import oss2

    try:
        return _bucket().object_exists(key)
    except oss2.exceptions.OssError as exc:  # pragma: no cover
        logger.warning("object_exists 失败 key=%s err=%s", key, exc)
        return False


def object_size(key: str) -> int:
    try:
        return _bucket().head_object(key).content_length
    except Exception as exc:  # pragma: no cover
        logger.warning("object_size 失败 key=%s err=%s", key, exc)
        return 0


def delete_object(key: str) -> None:
    if not key:
        return
    try:
        _bucket().delete_object(key)
    except Exception as exc:  # pragma: no cover
        logger.warning("删除 OSS 对象失败 key=%s err=%s", key, exc)


def delete_task_objects(task) -> None:
    for key in (task.original_key, task.mask_key, task.result_key, task.report_key,
                *(keyframe_key(str(task.id), i) for i in range(3))):
        delete_object(key)

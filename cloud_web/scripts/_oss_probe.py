"""OSS 探针共用工具：手工拼 multipart/form-data。

为什么不用 requests 的 files=：
  requests 的 `data={...含 tuple}` **不会**生成 multipart，而是被 URL 编码成
  application/x-www-form-urlencoded，OSS 直接回 405 MethodNotAllowed。
  浏览器的 FormData 才是真 multipart，所以探针必须手工拼。

抽成共用模块：check_oss.py 与 e2e_oss.py 都要用同一份拼装逻辑，
避免两处实现漂移（一处改了另一处没改，排查时最难缠）。
"""

from __future__ import annotations

import uuid


def multipart_body(
    fields: dict[str, str],
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> tuple[bytes, str]:
    """拼一个 multipart/form-data 请求体。

    返回 (body_bytes, content_type_header_value)。
    字段顺序：普通字段在前，file 在最后（与浏览器 FormData 行为一致）。
    """
    boundary = "----ossProbeBoundary" + uuid.uuid4().hex[:16]
    parts: list[bytes] = []
    for name, value in fields.items():
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
        parts.append(head.encode("utf-8"))
    file_head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    )
    parts.append(file_head.encode("utf-8") + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"

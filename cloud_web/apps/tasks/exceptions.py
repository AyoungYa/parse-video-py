"""统一错误响应：{code, message, detail?}。

前端只认 `code` 做分支，`message` 直接展示给用户（所以必须是人话）。
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    429: "rate_limited",
    500: "server_error",
}


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("未捕获异常：%s", exc)
        return Response(
            {"code": "server_error", "message": "服务器开小差了，请稍后重试"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    payload = response.data
    if isinstance(payload, dict) and "code" in payload:
        return response  # 视图已自行构造

    message = "请求无效"
    if isinstance(payload, dict):
        if "detail" in payload:
            message = str(payload["detail"])
        else:
            first_key = next(iter(payload), None)
            if first_key:
                value = payload[first_key]
                message = f"{first_key}: {value[0] if isinstance(value, list) else value}"
    elif isinstance(payload, list) and payload:
        message = str(payload[0])

    response.data = {
        "code": _CODE_BY_STATUS.get(response.status_code, "error"),
        "message": message,
    }
    return response

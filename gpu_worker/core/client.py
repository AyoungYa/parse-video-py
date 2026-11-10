"""云端 API 客户端。

只做出站 HTTPS，不监听任何端口 —— 这就是「不需要内网穿透」的原因。
所有请求都带重试；唯一**不能重试**的是 claim（重试会多领任务）。
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class LeaseLost(Exception):
    """云端说租约没了（超时被回收 / 任务被取消）。必须立刻停本地渲染。"""


class CloudError(Exception):
    def __init__(self, code: str, message: str, status: int = 0):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class CloudClient:
    def __init__(self, base_url: str, worker_id: str, token: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.token = token
        self.timeout = timeout

        self.session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({"X-Worker-Token": token, "User-Agent": f"wuhenjiang-worker/{worker_id}"})

    # ---- 底层 ----
    def _post(self, path: str, payload: dict, timeout: int | None = None) -> requests.Response:
        url = f"{self.base_url}/api/v1/worker/{path}"
        resp = self.session.post(url, json=payload, timeout=timeout or self.timeout)
        if resp.status_code == 401:
            raise CloudError("invalid_worker_token", "算力节点令牌无效，请检查 .env 的 WORKER_TOKEN", 401)
        if resp.status_code == 409:
            data = _safe_json(resp)
            if data.get("code") == "lease_lost":
                raise LeaseLost(data.get("message", "租约已失效"))
            raise CloudError(data.get("code", "conflict"), data.get("message", "状态冲突"), 409)
        if resp.status_code >= 400:
            data = _safe_json(resp)
            raise CloudError(data.get("code", "http_error"), data.get("message", resp.text[:200]), resp.status_code)
        return resp

    # ---- 协议四件 ----
    def claim(self, capabilities: dict[str, Any] | None = None) -> dict | None:
        """长轮询领任务。无任务返回 None。

        claim **不做自动重试**：一次领取失败就等下个循环，避免重复领到同一任务造成浪费。
        """
        url = f"{self.base_url}/api/v1/worker/claim"
        try:
            resp = self.session.post(
                url,
                json={"worker_id": self.worker_id, "capabilities": capabilities or {}},
                timeout=self.timeout + 20,  # 长轮询要留够
            )
        except requests.RequestException as exc:
            logger.warning("claim 网络异常：%s", exc)
            return None

        if resp.status_code == 204:
            return None
        if resp.status_code == 401:
            raise CloudError("invalid_worker_token", "算力节点令牌无效", 401)
        if resp.status_code >= 400:
            data = _safe_json(resp)
            raise CloudError(data.get("code", "claim_failed"), data.get("message", resp.text[:200]), resp.status_code)
        return resp.json()

    def heartbeat(self, task_id: str, stage: str = "", progress: float | None = None, message: str = "") -> int:
        payload = {"task_id": task_id, "stage": stage, "message": message}
        if progress is not None:
            payload["progress"] = round(progress, 4)
        resp = self._post("heartbeat", payload)
        return int(resp.json().get("lease_seconds", 90))

    def complete(self, task_id: str, result_key: str, metrics: dict, report_key: str = "") -> dict:
        return self._post(
            "complete",
            {"task_id": task_id, "result_key": result_key, "metrics": metrics, "report_key": report_key},
        ).json()

    def prepared(self, task_id: str, bbox: list[int], candidates: list[dict]) -> dict:
        return self._post("prepared", {"task_id": task_id, "bbox": bbox, "candidates": candidates}).json()

    def fail(
        self,
        task_id: str,
        error_code: str,
        error_message: str = "",
        retryable: bool = True,
        candidates: list | None = None,
    ) -> dict:
        payload: dict = {
            "task_id": task_id,
            "error_code": error_code[:64],
            "error_message": error_message[:1900],
            "retryable": retryable,
        }
        # 定位失败时带上候选框，云端转人工页面直接拿来当默认框
        if candidates:
            payload["candidates"] = candidates[:5]
        return self._post("fail", payload).json()


def _safe_json(resp: requests.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}

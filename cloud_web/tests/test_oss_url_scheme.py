"""OSS 签名 URL 协议测试。

背景（真实事故）：
  oss2 的 `_normalize_endpoint` 在 endpoint 没写协议时会补 **http://**，
  于是 `sign_url` 生成的下载/上传链接全是 `http://`。
  站点是 HTTPS 时，浏览器会按「混合内容（mixed content）」直接拦掉预览和下载 ——
  生产环境必炸，且报错在浏览器里，排查成本高。

  这里锁死三件事：
  1. endpoint 归一化后必须能拼出 https 的 host；
  2. build_post_policy 返回的 host 必须是 https；
  3. presign_get / presign_put 生成的签名 URL 必须是 https。

跑法（在 cloud_web/ 下）：
    uv run python scripts/run_tests_local.py
"""

from __future__ import annotations

from unittest.mock import patch

import oss2
from apps.tasks.services import oss
from django.test import TestCase, override_settings


class EndpointNormalizeTest(TestCase):
    """endpoint_host()：剥掉协议头和结尾斜杠，只留 host。"""

    def test_strips_scheme_and_trailing_slash(self):
        cases = {
            "oss-cn-shenzhen.aliyuncs.com": "oss-cn-shenzhen.aliyuncs.com",
            "oss-cn-shenzhen.aliyuncs.com/": "oss-cn-shenzhen.aliyuncs.com",
            "https://oss-cn-shenzhen.aliyuncs.com": "oss-cn-shenzhen.aliyuncs.com",
            "http://oss-cn-shenzhen.aliyuncs.com": "oss-cn-shenzhen.aliyuncs.com",
            "https://oss-cn-shenzhen.aliyuncs.com/": "oss-cn-shenzhen.aliyuncs.com",
            "  oss-cn-shenzhen.aliyuncs.com  ": "oss-cn-shenzhen.aliyuncs.com",
        }
        for raw, expect in cases.items():
            with self.subTest(raw=raw):
                with override_settings(OSS_ENDPOINT=raw):
                    self.assertEqual(oss.endpoint_host(), expect)

    def test_keeps_internal_endpoint_shape(self):
        """内网 endpoint 只是不被浏览器用，形状本身要保持可识别。"""
        with override_settings(OSS_ENDPOINT="oss-cn-shenzhen-internal.aliyuncs.com"):
            self.assertEqual(
                oss.endpoint_host(), "oss-cn-shenzhen-internal.aliyuncs.com"
            )


class PostPolicyHostTest(TestCase):
    """build_post_policy 返回的 host 必须是 https 公网地址。"""

    def _policy(self, endpoint: str) -> dict:
        with override_settings(
            OSS_ENDPOINT=endpoint,
            OSS_BUCKET="test-bucket",
            OSS_ACCESS_KEY_ID="ak",
            OSS_ACCESS_KEY_SECRET="sk",
        ):
            return oss.build_post_policy("task-id", "uploads/task-id/original.mp4")

    def test_host_is_https(self):
        policy = self._policy("oss-cn-shenzhen.aliyuncs.com")
        self.assertEqual(
            policy["host"], "https://test-bucket.oss-cn-shenzhen.aliyuncs.com"
        )
        self.assertTrue(policy["host"].startswith("https://"))

    def test_host_https_even_if_env_writes_http(self):
        """即使 .env 里写了 http://，对外暴露的 host 也必须是 https。"""
        policy = self._policy("http://oss-cn-shenzhen.aliyuncs.com")
        self.assertTrue(policy["host"].startswith("https://"))

    def test_host_not_internal(self):
        """内网 endpoint 不能当上传 host 返回给浏览器。"""
        policy = self._policy("oss-cn-shenzhen-internal.aliyuncs.com")
        self.assertIn("internal", policy["host"])


class SignedUrlSchemeTest(TestCase):
    """presign_get / presign_put 生成的签名 URL 必须是 https。

    oss2 在 endpoint 不带协议时补 http:// —— 所以这里**不用 mock**，
    直接用真实 oss2 算一遍签名，看 URL 到底是什么协议。
    （签名是纯本地计算，不需要网络。）
    """

    def _signed(self, endpoint: str) -> tuple[str, str, str]:
        """返回 (传给 oss2.Bucket 的 endpoint, GET 签名 URL, PUT 签名 URL)。"""
        with override_settings(
            OSS_ENDPOINT=endpoint,
            OSS_BUCKET="test-bucket",
            OSS_ACCESS_KEY_ID="ak",
            OSS_ACCESS_KEY_SECRET="sk",
        ):
            with patch("oss2.Bucket", wraps=oss2.Bucket) as fake_bucket:
                got_get = oss.presign_get("uploads/x/original.mp4")
                got_put = oss.presign_put("results/x/out.mp4", content_type="video/mp4")
                passed_endpoint = fake_bucket.call_args[0][1]
            return passed_endpoint, got_get, got_put

    def test_bucket_created_with_https_endpoint(self):
        passed_endpoint, _, _ = self._signed("oss-cn-shenzhen.aliyuncs.com")
        self.assertTrue(
            passed_endpoint.startswith("https://"),
            f"传给 oss2 的 endpoint 必须是 https，实际={passed_endpoint}",
        )

    def test_bucket_created_with_https_even_if_env_writes_http(self):
        passed_endpoint, _, _ = self._signed("http://oss-cn-shenzhen.aliyuncs.com")
        self.assertTrue(
            passed_endpoint.startswith("https://"),
            f"传给 oss2 的 endpoint 必须是 https，实际={passed_endpoint}",
        )

    def test_signed_urls_are_https(self):
        """★ 回归：曾因 endpoint 不带协议，签名 URL 全变成 http://，
        HTTPS 站点下浏览器按混合内容拦截预览与下载。"""
        _, got_get, got_put = self._signed("oss-cn-shenzhen.aliyuncs.com")
        self.assertTrue(
            got_get.startswith("https://"), f"GET 签名 URL 必须是 https：{got_get}"
        )
        self.assertTrue(
            got_put.startswith("https://"), f"PUT 签名 URL 必须是 https：{got_put}"
        )

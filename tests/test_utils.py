import os
from unittest.mock import patch

from parse_video_py.utils import (
    build_download_header_candidates,
    create_async_client,
    extract_url,
    is_downloadable_url,
    match_download_referer,
)


class TestCreateAsyncClient:
    """测试代理客户端工厂函数"""

    def test_no_proxy_env(self):
        """未设置代理环境变量时，httpx.AsyncClient 不传 proxy 参数"""
        with patch.dict(os.environ, {}, clear=True):
            with patch("parse_video_py.utils.httpx.AsyncClient") as mock:
                create_async_client()
                mock.assert_called_once_with()

    def test_with_proxy_env(self):
        """设置代理环境变量后，proxy 参数被注入"""
        proxy_url = "http://user:pass@proxy.example.com:8080"
        with patch.dict(os.environ, {"PARSE_VIDEO_PROXY": proxy_url}):
            with patch("parse_video_py.utils.httpx.AsyncClient") as mock:
                create_async_client()
                mock.assert_called_once_with(proxy=proxy_url)

    def test_proxy_with_follow_redirects(self):
        """代理和 follow_redirects 参数可共存"""
        proxy_url = "http://proxy.example.com:8080"
        with patch.dict(os.environ, {"PARSE_VIDEO_PROXY": proxy_url}):
            with patch("parse_video_py.utils.httpx.AsyncClient") as mock:
                create_async_client(follow_redirects=True)
                mock.assert_called_once_with(proxy=proxy_url, follow_redirects=True)

    def test_existing_kwargs_preserved(self):
        """未设代理时，透传的 kwargs 不变"""
        with patch.dict(os.environ, {}, clear=True):
            with patch("parse_video_py.utils.httpx.AsyncClient") as mock:
                create_async_client(follow_redirects=False)
                mock.assert_called_once_with(follow_redirects=False)


class TestExtractUrl:
    """测试 URL 提取工具函数"""

    def test_sohu_base64_url(self):
        """搜狐 base64 编码 URL 不被截断"""
        url = "https://tv.sohu.com/v/" "dXMvMzM1OTQyMjE0LzM5OTU3MTYxMi5zaHRtbA==.html"
        assert extract_url(url) == url

    def test_sohu_base64_in_text(self):
        """从混合文本中提取搜狐 base64 URL"""
        text = (
            "看看这个 https://tv.sohu.com/v/"
            "dXMvMzM1OTQyMjE0LzM5OTU3MTYxMi5zaHRtbA==.html 好看"
        )
        expected = (
            "https://tv.sohu.com/v/" "dXMvMzM1OTQyMjE0LzM5OTU3MTYxMi5zaHRtbA==.html"
        )
        assert extract_url(text) == expected

    def test_regular_url(self):
        """常规 URL 不受影响"""
        url = "https://v.douyin.com/abc123"
        assert extract_url(url) == url

    def test_url_with_query_params(self):
        """带查询参数的 URL"""
        url = "https://v.qq.com/x/page/l3502vppd13.html?ptag=v_qq_com"
        assert extract_url(url) == url


class TestDownloadHelpers:
    """测试下载代理相关的工具函数"""

    def test_match_referer_for_weibo_cdn(self):
        """微博视频 CDN 命中微博 Referer 规则"""
        url = "https://f.video.weibocdn.com/u0/xxx.mp4?ssig=abc"
        assert match_download_referer(url) == "https://weibo.com/"

    def test_match_referer_for_sinaimg(self):
        """新浪图床同样使用微博 Referer"""
        url = "https://wx4.sinaimg.cn/orj480/xxx.jpg"
        assert match_download_referer(url) == "https://weibo.com/"

    def test_match_referer_for_unknown_host(self):
        """未知域名不返回 Referer"""
        assert match_download_referer("https://cdn.example.com/a.mp4") is None

    def test_candidates_prefer_referer_and_ua(self):
        """命中平台规则时, 首选"浏览器 UA + 平台 Referer"组合"""
        candidates = build_download_header_candidates(
            "https://f.video.weibocdn.com/u0/xxx.mp4"
        )
        assert candidates[0]["Referer"] == "https://weibo.com/"
        assert "Mozilla/5.0" in candidates[0]["User-Agent"]

    def test_candidates_fallback_without_headers(self):
        """兜底候选不带任何伪装头, 应对只放行无 UA 请求的 CDN"""
        candidates = build_download_header_candidates(
            "https://f.video.weibocdn.com/u0/xxx.mp4"
        )
        assert candidates[-1] == {}

    def test_candidates_for_unknown_host(self):
        """未知域名只剩"仅 UA"与"无伪装头"两档候选"""
        candidates = build_download_header_candidates("https://cdn.example.com/a.mp4")
        assert len(candidates) == 2
        assert "Referer" not in candidates[0]

    def test_is_downloadable_url_accepts_cdn(self):
        """正常的 https CDN 地址允许转发"""
        assert is_downloadable_url("https://f.video.weibocdn.com/u0/xxx.mp4")

    def test_is_downloadable_url_rejects_unsupported_scheme(self):
        """非 http/https 协议被拒绝"""
        assert not is_downloadable_url("file:///etc/passwd")
        assert not is_downloadable_url("ftp://example.com/a.mp4")

    def test_is_downloadable_url_rejects_local_and_private_hosts(self):
        """本机与内网地址被拒绝, 避免 SSRF"""
        assert not is_downloadable_url("http://localhost:8000/health")
        assert not is_downloadable_url("http://127.0.0.1:8000/health")
        assert not is_downloadable_url("http://192.168.1.1/a.mp4")
        assert not is_downloadable_url("http://10.0.0.5/a.mp4")
        assert not is_downloadable_url("http://172.16.0.9/a.mp4")
        assert not is_downloadable_url("http://router.local/a.mp4")

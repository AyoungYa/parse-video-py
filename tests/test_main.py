from fastapi.testclient import TestClient

from parse_video_py.web import app

client = TestClient(app)


def test_share_url_parse_returns_400_when_no_url_found():
    response = client.get("/video/share/url/parse", params={"url": "这不是链接"})

    assert response.status_code == 200
    assert response.json() == {"code": 400, "msg": "未检测到有效的分享链接"}


def test_share_url_parse_returns_400_for_empty_string():
    response = client.get("/video/share/url/parse", params={"url": ""})

    assert response.status_code == 200
    assert response.json() == {"code": 400, "msg": "未检测到有效的分享链接"}


def test_share_url_parse_returns_400_for_partial_url_without_scheme():
    response = client.get(
        "/video/share/url/parse", params={"url": "example.com/video/123"}
    )

    assert response.status_code == 200
    assert response.json() == {"code": 400, "msg": "未检测到有效的分享链接"}


def test_share_url_parse_returns_422_when_url_param_missing():
    response = client.get("/video/share/url/parse")

    assert response.status_code == 422


class _FakeUpstream:
    """模拟 httpx 流式响应"""

    def __init__(self, status_code=200, headers=None, chunks=(b"abc",)):
        self.status_code = status_code
        self.headers = headers or {
            "content-type": "video/mp4",
            "content-length": str(sum(len(c) for c in chunks)),
        }
        self._chunks = chunks
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class _FakeClient:
    """模拟 httpx.AsyncClient, 按序返回预设响应并记录请求头"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent_headers = []
        self.closed = False

    def build_request(self, method, url, headers=None):
        return {"method": method, "url": url, "headers": headers}

    async def send(self, request, stream=False):
        self.sent_headers.append(request["headers"])
        return self._responses.pop(0)

    async def aclose(self):
        self.closed = True


def _patch_client(monkeypatch, fake_client):
    import parse_video_py.web as web_module

    monkeypatch.setattr(web_module, "create_async_client", lambda **kwargs: fake_client)


def test_video_download_rejects_unsupported_url():
    response = client.get("/video/download", params={"url": "file:///etc/passwd"})

    assert response.status_code == 400
    assert response.json() == {"code": 400, "msg": "不支持的下载地址"}


def test_video_download_requires_url_param():
    response = client.get("/video/download")

    assert response.status_code == 422


def test_video_download_streams_body_with_weibo_referer(monkeypatch):
    fake_client = _FakeClient([_FakeUpstream(chunks=(b"hello", b" world"))])
    _patch_client(monkeypatch, fake_client)

    response = client.get(
        "/video/download",
        params={
            "url": "https://f.video.weibocdn.com/u0/xxx.mp4",
            "filename": "示例视频.mp4",
        },
    )

    assert response.status_code == 200
    assert response.content == b"hello world"
    assert fake_client.sent_headers[0]["Referer"] == "https://weibo.com/"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "UTF-8''" in disposition
    assert fake_client.closed is True


def test_video_download_is_inline_without_filename(monkeypatch):
    fake_client = _FakeClient([_FakeUpstream(headers={"content-type": "image/jpeg"})])
    _patch_client(monkeypatch, fake_client)

    response = client.get(
        "/video/download",
        params={"url": "https://wx4.sinaimg.cn/orj480/xxx.jpg"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert "content-disposition" not in response.headers


def test_video_download_retries_without_headers_on_403(monkeypatch):
    fake_client = _FakeClient(
        [
            _FakeUpstream(status_code=403, headers={}),
            _FakeUpstream(
                status_code=206,
                headers={
                    "content-type": "video/mp4",
                    "content-range": "bytes 0-2/1000",
                    "content-length": "3",
                },
                chunks=(b"abc",),
            ),
        ]
    )
    _patch_client(monkeypatch, fake_client)

    response = client.get(
        "/video/download", params={"url": "https://f.video.weibocdn.com/u0/xxx.mp4"}
    )

    assert response.status_code == 206
    assert response.content == b"abc"
    assert fake_client.sent_headers[0]["Referer"] == "https://weibo.com/"
    assert fake_client.sent_headers[1] == {}
    assert response.headers["content-range"] == "bytes 0-2/1000"


def test_video_download_returns_502_when_all_attempts_rejected(monkeypatch):
    fake_client = _FakeClient(
        [
            _FakeUpstream(status_code=403, headers={}),
            _FakeUpstream(status_code=403, headers={}),
        ]
    )
    _patch_client(monkeypatch, fake_client)

    response = client.get(
        "/video/download", params={"url": "https://f.video.weibocdn.com/u0/xxx.mp4"}
    )

    assert response.status_code == 502
    assert response.json()["code"] == 502


def test_video_download_forwards_range_header(monkeypatch):
    fake_client = _FakeClient([_FakeUpstream(status_code=206, chunks=(b"abc",))])
    _patch_client(monkeypatch, fake_client)

    response = client.get(
        "/video/download",
        params={"url": "https://f.video.weibocdn.com/u0/xxx.mp4"},
        headers={"Range": "bytes=0-2"},
    )

    assert response.status_code == 206
    assert fake_client.sent_headers[0]["Range"] == "bytes=0-2"


def test_video_download_ascii_filename_fallback_for_chinese_title(monkeypatch):
    """纯中文文件名无法作为 ASCII 兜底名, 应退化为 download.<ext>, 同时保留 RFC 5987 名"""
    fake_client = _FakeClient([_FakeUpstream(chunks=(b"abc",))])
    _patch_client(monkeypatch, fake_client)

    response = client.get(
        "/video/download",
        params={
            "url": "https://f.video.weibocdn.com/u0/xxx.mp4",
            "filename": "微博示例视频.mp4",
        },
    )

    disposition = response.headers["content-disposition"]
    assert 'filename="download.mp4"' in disposition
    assert "%E5%BE%AE%E5%8D%9A" in disposition

import dataclasses
import os
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from fastapi_mcp import FastApiMCP

from parse_video_py.parser import VideoSource, parse_video_id, parse_video_share_url
from parse_video_py.utils import (
    build_download_header_candidates,
    create_async_client,
    extract_url,
    is_downloadable_url,
)

# 下载代理的上游读取超时（按块读取，超时指单个数据块的等待间隔）
_DOWNLOAD_TIMEOUT_SECONDS = 60.0


def _get_templates_dir() -> str:
    # 模板已移入 src/parse_video_py/templates/，与 web.py 同级
    templates_dir = Path(__file__).parent / "templates"
    if templates_dir.is_dir():
        return str(templates_dir)
    raise FileNotFoundError("templates 目录未找到")


app = FastAPI()

mcp = FastApiMCP(app)
mcp.mount_http()

templates = Jinja2Templates(directory=_get_templates_dir())


def _build_auth_dependency() -> list[Depends]:
    """根据环境变量动态构建 Basic Auth 依赖项"""
    basic_auth_username = os.getenv("PARSE_VIDEO_USERNAME")
    basic_auth_password = os.getenv("PARSE_VIDEO_PASSWORD")

    if not (basic_auth_username and basic_auth_password):
        return []

    security = HTTPBasic()

    def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
        correct_username = secrets.compare_digest(
            credentials.username, basic_auth_username
        )
        correct_password = secrets.compare_digest(
            credentials.password, basic_auth_password
        )
        if not (correct_username and correct_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials

    return [Depends(verify_credentials)]


# 模块加载时构建一次，避免每个路由装饰器重复调用
_auth_dependency = _build_auth_dependency()


@app.get("/", response_class=HTMLResponse, dependencies=_auth_dependency)
async def read_item(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "title": "github.com/wujunwei928/parse-video-py Demo",
        },
    )


@app.get("/video/share/url/parse", dependencies=_auth_dependency)
async def share_url_parse(url: str):
    video_share_url = extract_url(url)
    if video_share_url is None:
        return {
            "code": 400,
            "msg": "未检测到有效的分享链接",
        }

    try:
        video_info = await parse_video_share_url(video_share_url)
        return {
            "code": 200,
            "msg": "解析成功",
            "data": dataclasses.asdict(video_info),
        }
    except Exception as err:
        return {
            "code": 500,
            "msg": str(err),
        }


@app.get("/video/id/parse", dependencies=_auth_dependency)
async def video_id_parse(source: VideoSource, video_id: str):
    try:
        video_info = await parse_video_id(source, video_id)
        return {
            "code": 200,
            "msg": "解析成功",
            "data": dataclasses.asdict(video_info),
        }
    except Exception as err:
        return {
            "code": 500,
            "msg": str(err),
        }


async def _iter_upstream(upstream, client):
    """按块转发上游响应体，转发结束或中断时释放连接。"""
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
    finally:
        await upstream.aclose()
        await client.aclose()


@app.get("/video/download", dependencies=_auth_dependency)
async def video_download(request: Request, url: str, filename: str | None = None):
    """代理转发平台 CDN 资源，解决浏览器直链下载被防盗链拦截(403)的问题。

    - 传 `filename` 参数：以附件形式返回，用于页面上的下载按钮；
    - 不传 `filename`：按原始 Content-Type 内联返回，供页面 `<img>` 直接引用。
    """
    if not is_downloadable_url(url):
        return JSONResponse(
            status_code=400,
            content={"code": 400, "msg": "不支持的下载地址"},
        )

    range_header = request.headers.get("range")
    client = create_async_client(
        follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_SECONDS
    )
    upstream = None
    last_status = None

    # 依次尝试不同的请求头组合，直到上游返回 200/206
    for candidate_headers in build_download_header_candidates(url):
        headers = dict(candidate_headers)
        if range_header:
            headers["Range"] = range_header

        try:
            upstream_request = client.build_request("GET", url, headers=headers)
            response = await client.send(upstream_request, stream=True)
        except Exception as err:
            await client.aclose()
            return JSONResponse(
                status_code=502,
                content={"code": 502, "msg": f"下载失败: {err}"},
            )

        last_status = response.status_code
        if response.status_code in (200, 206):
            upstream = response
            break

        await response.aclose()

    if upstream is None:
        await client.aclose()
        return JSONResponse(
            status_code=502,
            content={
                "code": 502,
                "msg": f"下载失败, 上游返回状态码: {last_status}",
            },
        )

    response_headers = {
        "Cache-Control": "no-store",
        "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
    }
    for header_name in ("content-length", "content-range", "etag", "last-modified"):
        if header_name in upstream.headers:
            response_headers[header_name] = upstream.headers[header_name]

    if filename:
        # 中文文件名无法作为 ASCII 兜底名，退化时保留扩展名、主体用 download
        ascii_filename = filename.encode("ascii", "ignore").decode().strip()
        stem, dot, ext = ascii_filename.rpartition(".")
        if not stem.strip("._- "):
            ascii_filename = f"download.{ext}" if dot and ext else "download"
        response_headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_filename}"; '
            f"filename*=UTF-8''{quote(filename)}"
        )

    media_type = upstream.headers.get("content-type", "application/octet-stream").split(
        ";"
    )[0]

    return StreamingResponse(
        _iter_upstream(upstream, client),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=media_type.strip(),
    )


# ---- 去水印调参台（可选：需安装 [wm] extras）----
# 用 try/except 兜住：没装 torch/opencv 时主服务照常启动，只是 /wm 不可用。
try:
    from parse_video_py.wm_studio.api import router as wm_router

    app.include_router(wm_router)
except ImportError as _wm_exc:  # pragma: no cover - 取决于可选依赖
    import logging

    logging.getLogger(__name__).info(
        "去水印调参台未启用（缺依赖，装 `.[wm]` 后可用）: %s", _wm_exc
    )


mcp.setup_server()

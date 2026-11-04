# -*- coding: utf-8 -*-
"""
 <p>@Description: </p>
 <p>@Author: zeyingyang</p>.
 <p>@Time: 2026/10/14 15:59</p>
"""

import asyncio
import json
import sys
from pathlib import Path

from parse_video_py.parser import parse_video_share_url
from parse_video_py.utils import build_download_header_candidates, create_async_client

async def download(url: str, save_path: str) -> Path:
    """CDN 直链下载到本地：自动按平台补 Referer/UA，逐块流式落盘。"""
    path = Path(save_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    async with create_async_client(follow_redirects=True, timeout=60.0) as client:
        for headers in build_download_header_candidates(
            url
        ):  # 微博Referer+UA → 仅UA → 无头
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code not in (200, 206):
                    continue
                with path.open("wb") as fp:
                    async for chunk in resp.aiter_bytes():
                        fp.write(chunk)
                return path
    raise RuntimeError("下载失败: 所有请求头组合都被拒绝")


# 默认演示链接（微博用户主页的弹层 URL，会被风控重定向，用于演示报错提示）
url = (
    "https://weibo.com/n/%E7%8E%8B%E4%BC%A0%E5%90%9B"
    "?tabtype=newVideo&layerid=5355209434858024"
)
# 支持命令行覆盖：python test1.py "<分享链接>"
if len(sys.argv) > 1:
    url = sys.argv[1]

# 根据分享链接解析
video_info = asyncio.run(parse_video_share_url(url))
print(
    "解析分享链接：\n",
    json.dumps(video_info, ensure_ascii=False, indent=4, default=lambda x: x.__dict__),
    "\n",
)

video_url = video_info.video_url
if not video_url:
    exit(1)
print(f"{video_url}")

# 注意: 传给 download() 的一定是解析出来的 CDN 直链 video_url,
# 不是分享页链接 url (那是网页 HTML, 下载下来就是 10K 的错误文件)
asyncio.run(download(video_url, "./weibo111.mp4"))

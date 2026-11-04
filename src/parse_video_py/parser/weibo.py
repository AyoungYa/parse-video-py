import json
import re
from urllib.parse import urlparse

import fake_useragent

from ..utils import (
    build_cookie_header,
    create_async_client,
    get_val_from_url_by_query_key,
    get_weibo_cookie,
)
from .base import BaseParser, ImgInfo, VideoAuthor, VideoInfo


class WeiBo(BaseParser):
    """
    微博
    """

    async def parse_share_url(self, share_url: str) -> VideoInfo:
        layer_id = get_val_from_url_by_query_key(share_url, "layerid")
        if layer_id:
            return await self.parse_post_url(
                layer_id, f"https://m.weibo.cn/detail/{layer_id}"
            )

        # Handle video URLs
        if "show?fid=" in share_url:
            video_id = get_val_from_url_by_query_key(share_url, "fid")
            return await self.parse_video_id(video_id)
        elif "/tv/show/" in share_url:
            url_info = urlparse(share_url)
            video_id = url_info.path.replace("/tv/show/", "")
            return await self.parse_video_id(video_id)
        else:
            # Handle regular post URLs (potential image albums)
            # Extract post ID from URLs like https://weibo.com/2543858012/Q9pcJ4S21
            url_info = urlparse(share_url)
            path_parts = url_info.path.strip("/").split("/")
            if len(path_parts) >= 2:
                post_id = path_parts[-1]
                return await self.parse_post_url(post_id, share_url)

        raise Exception("unsupported weibo url format")

    async def parse_video_id(self, video_id: str) -> VideoInfo:
        req_url = f"https://h5.video.weibo.com/api/component?page=/show/{video_id}"
        headers = {
            "Referer": f"https://h5.video.weibo.com/show/{video_id}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": fake_useragent.UserAgent(os="iOS").random,
            **build_cookie_header(get_weibo_cookie()),
        }
        post_content = 'data={"Component_Play_Playinfo":{"oid":"' + video_id + '"}}'
        async with create_async_client(follow_redirects=True) as client:
            response = await client.post(req_url, headers=headers, content=post_content)
            response.raise_for_status()

        json_data = response.json()
        data = json_data["data"]["Component_Play_Playinfo"]

        video_url = data["stream_url"]
        if len(data["urls"]) > 0:
            # stream_url码率最低，urls中第一条码率最高
            _, first_mp4_url = next(iter(data["urls"].items()))
            video_url = f"https:{first_mp4_url}"

        video_info = VideoInfo(
            video_url=video_url,
            cover_url="https:" + data["cover_image"],
            title=data["title"],
            author=VideoAuthor(
                uid=str(data["user"]["id"]),
                name=data["author"],
                avatar="https:" + data["avatar"],
            ),
        )
        return video_info

    async def parse_post_url(self, post_id: str, original_url: str) -> VideoInfo:
        """
        Parse Weibo post (potential image album)
        """
        cookie_header = build_cookie_header(get_weibo_cookie())

        # Try mobile API first
        req_url = f"https://m.weibo.cn/statuses/show?id={post_id}"
        headers = {
            "User-Agent": fake_useragent.UserAgent(os="iOS").random,
            "Referer": "https://m.weibo.cn/",
            "Content-Type": "application/json;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            **cookie_header,
        }

        mobile_error: Exception | None = None
        try:
            async with create_async_client(follow_redirects=True) as client:
                response = await client.get(req_url, headers=headers)
                response.raise_for_status()

            json_data = response.json()
            if "data" in json_data:
                return await self._parse_mobile_api_data(json_data["data"])
            mobile_error = Exception(
                f"mobile api returned no data field: {str(json_data)[:200]}"
            )
        except Exception as err:
            # 不再静默吞掉：记录失败原因，便于与下面的 HTML 解析失败对照排查
            mobile_error = err

        # Fallback to desktop page parsing using the original URL
        headers = {
            "User-Agent": fake_useragent.UserAgent(os="iOS").random,
            **cookie_header,
        }

        async with create_async_client(follow_redirects=True) as client:
            response = await client.get(original_url, headers=headers)
            response.raise_for_status()
            final_url = str(response.url)

        # 未带 Cookie 时微博常把请求 302 到访客系统(登录页)，这里给出明确提示
        if "passport.weibo.com" in final_url or "visitor" in final_url:
            raise Exception(
                "weibo 要求登录：请求被重定向到访客/登录页 "
                f"({final_url})。请设置环境变量 PARSE_VIDEO_WEIBO_COOKIE "
                "传入浏览器中的微博 Cookie 后重试。"
                f" (mobile api 失败原因: {mobile_error})"
            )

        try:
            return await self._parse_html_page(response.text)
        except (ValueError, KeyError, TypeError) as err:
            raise Exception(
                "微博页面未包含有效帖子数据，可能需要登录或链接已失效；"
                "请检查链接，必要时设置 PARSE_VIDEO_WEIBO_COOKIE。"
                f" mobile api 失败原因: {type(mobile_error).__name__}; "
                f"HTML 失败原因: {err}"
            ) from err

    async def _parse_mobile_api_data(self, data: dict) -> VideoInfo:
        """
        Parse data from mobile API
        """
        # Extract basic info
        title = data.get("text", "")
        author_info = data.get("user", {})
        author_name = author_info.get("screen_name", "")
        author_avatar = author_info.get("avatar_large", "")

        # Get images
        images = []
        pics_data = data.get("pics", [])
        for pic in pics_data:
            # Get the largest image URL available
            large_pic_url = ""
            for size in ["large", "original", "bmiddle", "url"]:
                value = pic.get(size)
                if isinstance(value, str) and value:
                    large_pic_url = value
                    break
                if isinstance(value, dict) and value.get("url"):
                    large_pic_url = value["url"]
                    break

            if large_pic_url:
                images.append(ImgInfo(url=large_pic_url))

        page_info = data.get("page_info") or {}
        media_info = page_info.get("media_info") or {}
        video_url = next(
            (media_info[key] for key in (
                "stream_url_hd", "stream_url", "mp4_720p_mp4", "mp4_sd_url"
            ) if media_info.get(key)),
            "",
        )
        if not video_url and page_info.get("type") == "video":
            object_id = page_info.get("object_id")
            if object_id:
                return await self.parse_video_id(str(object_id))
        video_info = VideoInfo(
            video_url=video_url,
            cover_url=(page_info.get("page_pic") or {}).get("url", ""),
            title=self._clean_text(title),
            images=images,
            author=VideoAuthor(
                name=author_name,
                avatar=author_avatar,
            ),
        )
        return video_info

    async def _parse_html_page(self, html_content: str) -> VideoInfo:
        """
        Parse data from HTML page
        """
        # Decode only the JSON value; trailing JavaScript such as [0] is not JSON.
        match = re.search(r"\$render_data\s*=\s*", html_content)
        if not match:
            raise ValueError("parse weibo html page fail: missing $render_data")
        data, _ = json.JSONDecoder().raw_decode(html_content[match.end():].lstrip())
        if isinstance(data, list):
            if not data:
                raise ValueError("empty weibo render data")
            data = data[0]
        if not isinstance(data, dict) or not isinstance(data.get("status"), dict):
            raise ValueError("missing weibo status data")
        return await self._parse_mobile_api_data(data["status"])

    def _clean_text(self, text: str) -> str:
        """
        Remove HTML tags from text
        """
        # Remove HTML tags
        cleaned = re.sub(r"<[^>]*>", "", text)
        return cleaned.strip()

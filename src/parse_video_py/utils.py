import os
import re
from urllib.parse import parse_qs, urlparse

import httpx

URL_REG = re.compile(r"http[s]?:\/\/[\w.-]+[\w\/-]*[\w.-]*\??[\w=&:\-\+\%.]*[/]*")

# 下载代理使用的桌面浏览器 UA。
# 部分平台 CDN（如微博/新浪）会对直链做防盗链校验："浏览器 UA + 平台站内 Referer" 才放行。
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PARSE_VIDEO_WEIBO_COOKIE = 'SCF=Al_ANIP3uuZ8pzrfjQeVT7PLzixbI442-0-K7Ng1r5lIMSWU3SjMy6pbb4TZbicopKaipJFTmO0LNczGNEczKd8.; SUBP=0033WrSXqPxfM725Ws9jqgMF55529P9D9WFLg1_60AR3U3EEce-cSI7k5JpX5KMhUgL.Fo.peoBES0.ReKq2dJLoIEXLxKqLBo.L1KqLxK-L1K-L122LxK-LBo5L12qLxKnLBo2L1hqLxKnLBo2L1hqt; SINAGLOBAL=1405401480947.9941.1788923659624; UOR=,,tophub.today; XSRF-TOKEN=MlO6zrKbbzwiEFu2XHo7G9MP; _s_tentry=weibo.com; Apache=820511669830.0474.1790678523388; ULV=1790678523390:2:2:1:820511669830.0474.1790678523388:1788923659627; ALF=1794619685; SUB=_2A25H1Fx1DeRhGe9P6VYT9yfEyjqIHXVkqNG9rDV8PUJbkNANLXnfkW1NdTjSo0-5WSZ5275RvpWa0CKHuODi6cg5; PC_TOKEN=25c5f2e159; WBPSESS=u1gV4ralDNUMjN3gmx-iPN8WgW-f53DLcWls_nmdCJdC-1P3Ro0z-7jLwZjjv5mUSTyLr1EJrCLuQJTqY4lJgoao5COZaBnm0AG3Wl2F-A_E0-H6WdaDzXtaaWyOIhTgMqtaUhaxhXhseRcz_xCPGA=='


# CDN 域名关键字 → 防盗链 Referer 的映射规则，按顺序匹配，命中即返回。
_DOWNLOAD_REFERER_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "weibocdn.com",
            "weibo.com",
            "weibo.cn",
            "sinaimg.cn",
            "sina.com.cn",
            "sina.cn",
        ),
        "https://weibo.com/",
    ),
    (
        (
            "douyin.com",
            "douyinpic.com",
            "douyinvod.com",
            "bytedance",
            "snssdk.com",
            "byteimg.com",
            "pstatp.com",
            "ixigua",
            "huoshan",
        ),
        "https://www.douyin.com/",
    ),
    (
        (
            "bilibili.com",
            "bilivideo.com",
            "bilivideo.cn",
            "hdslb.com",
            "acgvideo.com",
        ),
        "https://www.bilibili.com/",
    ),
    (
        ("kuaishou.com", "kwimgs.com", "yximgs.com", "gifshow.com"),
        "https://www.kuaishou.com/",
    ),
    (
        ("xiaohongshu.com", "xhscdn.com", "sns-img", "sns-video"),
        "https://www.xiaohongshu.com/",
    ),
)

# 拒绝转发的内网/本机地址前缀，避免下载代理被当作 SSRF 跳板
_PRIVATE_HOST_RE = re.compile(
    r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)"
)


def extract_url(text: str) -> str | None:
    """从文本中提取第一个匹配的 URL"""
    match = URL_REG.search(text)
    return match.group() if match else None


def get_val_from_url_by_query_key(url: str, query_key: str) -> str:
    """
    从url的query参数中解析出query_key对应的值
    :param url: url地址
    :param query_key: query参数的key
    :return:
    """
    url_res = urlparse(url)
    url_query = parse_qs(url_res.query, keep_blank_values=True)

    try:
        query_val = url_query[query_key][0]
    except KeyError:
        raise KeyError(f"url中不存在query参数: {query_key}")

    if len(query_val) == 0:
        raise ValueError(f"url中query参数值长度为0: {query_key}")

    return url_query[query_key][0]


def create_async_client(**kwargs) -> httpx.AsyncClient:
    """创建 httpx.AsyncClient，自动注入代理配置。

    从环境变量 PARSE_VIDEO_PROXY 读取代理地址（如 http://user:pass@host:port），
    未设置则不使用代理。其余参数透传给 httpx.AsyncClient。
    """
    proxy = os.getenv("PARSE_VIDEO_PROXY")
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


def get_weibo_cookie() -> str:
    """读取微博 Cookie。

    从环境变量 PARSE_VIDEO_WEIBO_COOKIE 读取（浏览器 F12 复制的完整 Cookie 字符串）。
    未设置则返回空字符串，此时请求不带 Cookie（会命中微博访客风控，易被重定向到登录页）。
    """
    return PARSE_VIDEO_WEIBO_COOKIE
    return os.getenv("PARSE_VIDEO_WEIBO_COOKIE", "").strip()


def build_cookie_header(cookie: str) -> dict[str, str]:
    """把 Cookie 字符串包成请求头，空值返回空 dict（便于 ** 展开）。"""
    cookie = (cookie or "").strip()
    if not cookie:
        return {}
    return {"Cookie": cookie}


def is_downloadable_url(url: str) -> bool:
    """校验下载代理的目标地址是否可以转发。

    仅允许 http/https，并拒绝 localhost 与内网地址，防止代理端点被用作 SSRF 跳板。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False

    if hostname in ("localhost", "localhost.localdomain", "::1", "0.0.0.0"):
        return False
    if hostname.endswith(".local"):
        return False
    if _PRIVATE_HOST_RE.match(hostname):
        return False

    return True


def match_download_referer(url: str) -> str | None:
    """按 CDN 域名匹配防盗链 Referer，未命中返回 None。"""
    hostname = (urlparse(url).hostname or "").lower()
    for domain_keywords, referer in _DOWNLOAD_REFERER_RULES:
        if any(keyword in hostname for keyword in domain_keywords):
            return referer
    return None


def build_download_header_candidates(url: str) -> list[dict[str, str]]:
    """生成下载代理的请求头候选列表，按优先级从高到低排列。

    依次尝试："浏览器 UA + 平台 Referer"（命中平台规则时）→ "仅浏览器 UA" →
    "不带任何伪装头"。微博 CDN 只对无 UA 请求放行，故需保留最后一档兜底。
    """
    referer = match_download_referer(url)

    candidates: list[dict[str, str]] = []
    if referer:
        candidates.append({"User-Agent": _BROWSER_UA, "Referer": referer})
    else:
        candidates.append({"User-Agent": _BROWSER_UA})
    candidates.append({})

    return candidates

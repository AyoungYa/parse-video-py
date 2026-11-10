---
knowledge_version: 1
last_scanned_at: 2026-06-08
source_commit: dd55df3
confidence: high
---

# 核心业务流程

## 流程：cloud_web 视频去水印任务

上传确认后进入 `pending`。worker 首次领取进入 `leased`，先下载原片、提取关键帧（`extracting_frames`），再通过抽样帧定位水印（`locating_watermark`）。worker 把关键帧上传到任务专属 OSS 对象，并通过 `POST /api/v1/worker/prepared` 提交建议框；云端释放租约，转为 `awaiting_confirmation`。前端在关键帧上以红框显示建议区域，用户可直接确认或重新框选。

用户通过 `POST /api/v1/tasks/<id>/bbox` 确认后，写入 `bbox` 和 `bbox_confirmed`，不重复扣费，任务回到 `pending`。worker 第二次领取时 `phase=render`，按该区域生成 mask 并去水印（`removing_watermark`），随后进入 `verifying`。只有已确认水印区域且进入验收阶段的任务可上报完成，转 `succeeded`。

定位置信度不足时仍进入待确认阶段，允许用户从候选框选择或手动画框。准备或渲染阶段租约超时、可重试错误回到 `pending`；重试耗尽转 `failed`。`awaiting_confirmation` 可取消并退款。所有状态变化写 `TaskEvent`。关键帧对象与原片、成片一并按保留期清理。

相关代码：`cloud_web/apps/tasks/services/lease.py`、`cloud_web/apps/tasks/views_worker.py`、`cloud_web/apps/tasks/views_user.py`、`gpu_worker/core/pipeline.py`、`gpu_worker/core/runner.py`。

## 流程：分享链接解析

### 是否已确认

- 状态：已确认
- 证据来源：`web.py:75-95`、`parser/__init__.py:148-173`

### 触发入口

| 类型 | 路径/接口/命令 | 入口代码 |
|---|---|---|
| HTTP API | `GET /video/share/url/parse?url=<share_url>` | `web.py:share_url_parse` |
| CLI | `parse-video-py parse "<url>"` | `cli/__init__.py:parse` → `cli/_parse.py:run_parse` |
| SDK | `asyncio.run(parse_video_share_url(url))` | `parser/__init__.py:148` |

### 执行链路

1. 接收分享链接（URL 参数/CLI 参数/SDK 参数）
2. `utils.py:extract_url()` 从文本中提取 URL
3. `parser/__init__.py:parse_video_share_url()` 遍历 `video_source_info_mapping`，用域名匹配确定平台
4. 实例化对应平台的 Parser 类
5. 调用 `parser.parse_share_url(share_url)` 获取 `VideoInfo`
6. 各平台解析器内部流程：
   - 解析 URL 域名和路径，提取视频 ID
   - 构造平台 API 请求（带 UA 伪装）
   - 解析响应（JSON/HTML），提取视频/图集信息
   - 返回标准 `VideoInfo` 对象
7. Web/API 模式：`dataclasses.asdict(video_info)` 转为 JSON 返回

### 关键代码

| 类/函数/文件 | 职责 | 来源 |
|---|---|---|
| `parse_video_share_url()` | URL→平台路由→调用解析器 | `parser/__init__.py:148` |
| `video_source_info_mapping` | 平台→域名→解析器映射表 | `parser/__init__.py:29-145` |
| `BaseParser.parse_share_url()` | 解析器抽象接口 | `parser/base.py:103` |
| `extract_url()` | 从文本中提取 URL | `utils.py:7` |
| `dataclasses.asdict()` | VideoInfo 序列化 | `web.py:89` |

### 数据流

- 输入：用户分享链接（字符串）
- 处理：域名匹配→平台识别→HTTP 请求→HTML/JSON 解析
- 输出：`VideoInfo`（video_url, cover_url, title, music_url, images, author）
- 存储：无持久化

### 配置项

| 配置项 | 用途 | 读取位置 |
|---|---|---|
| `PARSE_VIDEO_USERNAME` | Basic Auth 用户名 | `web.py:34` |
| `PARSE_VIDEO_PASSWORD` | Basic Auth 密码 | `web.py:35` |
| `fake_useragent` | 随机 UA 伪装 | `parser/base.py:99` |

### 异常处理

- URL 无法识别平台：抛出 `ValueError("share url does not have source config")` → `parser/__init__.py:164`
- 解析器未配置：抛出 `ValueError("source has no video parser")` → `parser/__init__.py:167`
- Web 层捕获所有异常返回 `{"code": 500, "msg": str(err)}` → `web.py:91-95`
- CLI 层区分单条失败和全部失败 → `cli/_parse.py:84-101`

### 修改风险

- 修改 `video_source_info_mapping` 影响所有平台的 URL 路由
- 修改 `BaseParser` 接口影响所有 26 个解析器
- 修改 `VideoInfo` 数据结构影响 Web API 响应格式

### 未确认事项

- 无

---

## 流程：视频 ID 解析

### 是否已确认

- 状态：已确认
- 证据来源：`web.py:98-111`、`parser/__init__.py:176-193`

### 触发入口

| 类型 | 路径/接口/命令 | 入口代码 |
|---|---|---|
| HTTP API | `GET /video/id/parse?source=<VideoSource>&video_id=<id>` | `web.py:video_id_parse` |
| SDK | `asyncio.run(parse_video_id(VideoSource.DouYin, "xxx"))` | `parser/__init__.py:176` |

### 执行链路

1. 接收 VideoSource 枚举和视频 ID
2. 从 `video_source_info_mapping` 查找对应解析器
3. 实例化解析器，调用 `parser.parse_video_id(video_id)`
4. 返回标准 `VideoInfo` 对象

### 关键代码

| 类/函数/文件 | 职责 | 来源 |
|---|---|---|
| `parse_video_id()` | 根据平台枚举和 ID 调用解析器 | `parser/__init__.py:176` |
| `VideoSource` | 平台枚举定义 | `parser/base.py:9-38` |

### 修改风险

- 与分享链接解析共用 `video_source_info_mapping`，修改映射表需同时验证两条链路

### 未确认事项

- 无

---

## 流程：CLI 批量解析

### 是否已确认

- 状态：已确认
- 证据来源：`cli/_parse.py`

### 触发入口

| 类型 | 命令 | 入口代码 |
|---|---|---|
| CLI | `parse-video-py parse "url1" "url2"` | `cli/__init__.py:parse` |
| CLI（文件） | `parse-video-py parse -f urls.txt` | `cli/_parse.py:18-28` |
| CLI（管道） | `echo "url" \| parse-video-py parse -f -` | `cli/_parse.py:20-21` |

### 执行链路

1. 接收 URL 列表（参数/文件/stdin）
2. 单条：`asyncio.run(_parse_single())` → 直接解析
3. 多条：`asyncio.run(_parse_batch())` → `asyncio.Semaphore(10)` 控制并发
4. 格式化输出（text 或 json）

### 关键代码

| 类/函数/文件 | 职责 | 来源 |
|---|---|---|
| `_CONCURRENCY_LIMIT = 10` | 并发限制 | `cli/_parse.py:14` |
| `_limited_parse_single()` | 带信号量的单条解析 | `cli/_parse.py:45-48` |
| `_parse_batch()` | 批量并发解析 | `cli/_parse.py:51-57` |

### 未确认事项

- 无

---

## 流程：下载代理转发（防盗链 403 修复）

### 是否已确认

- 状态：已确认
- 证据来源：`web.py:video_download`、`utils.py:build_download_header_candidates()`、`templates/index.html:proxyUrl()`

### 背景

各平台 CDN 对直链下载做防盗链校验，浏览器直接打开解析结果中的 `video_url` 会返回
`403 Forbidden`（微博 CDN 返回 Tengine 错误页）。实测微博/新浪 CDN 的放行条件是
**"浏览器 UA + 平台站内 Referer"** 或 **"完全不带 UA 的请求"**，携带第三方 Referer
（含本页来源）一律 403，因此无法通过前端 `referrerpolicy` 解决，必须由服务端代理转发。

### 触发入口

| 类型 | 路径/接口/命令 | 入口代码 |
|---|---|---|
| HTTP API | `GET /video/download?url=<原始CDN地址>&filename=<可选文件名>` | `web.py:video_download` |
| 前端（下载按钮） | `proxyUrl(data.video_url, '标题.mp4')` | `templates/index.html:proxyUrl()` |
| 前端（`<img>` 内联） | `proxyUrl(item.url)`（不传 filename） | `templates/index.html` |

### 执行链路

1. 校验目标地址：`utils.py:is_downloadable_url()` 仅允许 http/https，拒绝 localhost 与内网地址
2. 生成请求头候选：`utils.py:build_download_header_candidates()` 按优先级返回
   `[浏览器 UA + 平台 Referer]` → `[仅浏览器 UA]` → `[无任何伪装头]`
3. 逐个候选发起上游请求（`httpx` 流式），透传浏览器的 `Range` 头
4. 命中 200/206 即开始按块流式回传；全部候选失败返回 `502`
5. 转发响应头：`Content-Type`、`Content-Length`、`Content-Range`、`Accept-Ranges`、`ETag`、`Last-Modified`
6. 传了 `filename` 时附加 `Content-Disposition: attachment`（含 RFC 5987 编码的中文文件名），
   否则内联返回，供页面 `<img>` 直接引用

### 关键代码

| 类/函数/文件 | 职责 | 来源 |
|---|---|---|
| `video_download()` | 下载代理路由：校验 → 候选重试 → 流式转发 | `web.py` |
| `_iter_upstream()` | 按块转发上游响应体并释放连接 | `web.py` |
| `build_download_header_candidates()` | 生成防盗链请求头候选列表 | `utils.py` |
| `match_download_referer()` | CDN 域名 → 平台 Referer 匹配 | `utils.py` |
| `is_downloadable_url()` | 目标地址合法性/SSRF 防护 | `utils.py` |
| `_DOWNLOAD_REFERER_RULES` | 域名关键字 → Referer 规则表 | `utils.py` |
| `proxyUrl()` | 前端拼接代理地址 | `templates/index.html` |

### 异常处理

- 非 http/https 或内网地址：返回 `{"code": 400, "msg": "不支持的下载地址"}`
- 上游请求异常：返回 `{"code": 502, "msg": "下载失败: <err>"}`
- 所有候选头均被拒（如 403）：返回 `{"code": 502, "msg": "下载失败, 上游返回状态码: 403"}`

### 修改风险

- 新增平台 Referer 规则只需追加 `_DOWNLOAD_REFERER_RULES`，不影响解析链路
- 改动本流程不影响 `VideoInfo` 数据结构与解析器路由

### 未确认事项

- 无

---

## 流程：Basic Auth 鉴权

### 是否已确认

- 状态：已确认
- 证据来源：`web.py:32-61`

### 执行链路

1. 模块加载时调用 `_build_auth_dependency()`
2. 读取 `PARSE_VIDEO_USERNAME` 和 `PARSE_VIDEO_PASSWORD` 环境变量
3. 若两个变量都设置，创建 `HTTPBasic` 验证依赖
4. 使用 `secrets.compare_digest()` 安全比较凭证
5. 所有路由通过 `dependencies=_auth_dependency` 统一应用

### 关键代码

| 函数 | 职责 | 来源 |
|---|---|---|
| `_build_auth_dependency()` | 动态构建鉴权依赖 | `web.py:32` |
| `verify_credentials()` | 凭证安全验证 | `web.py:42` |

### 修改风险

- 使用 `secrets.compare_digest()` 防止时序攻击，不要改为普通字符串比较
- 模块加载时构建一次，修改环境变量后需重启服务

### 未确认事项

- 无

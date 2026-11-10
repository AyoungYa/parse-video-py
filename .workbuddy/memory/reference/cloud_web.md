# cloud_web 云端服务细节（参考）

> 从 `MEMORY.md` 拆出，避免主记忆文件超限被截断。
> OSS 真机联调、PyCharm 调试配置、数据库规范全部细节。
> 2026-11-06 / 2026-11-09 沉淀。

## OSS 真机打通（2026-11-06）

bucket `wuhenjiangtest`，地域 **oss-cn-shenzhen**（★ 桶地域不可改）。
自检 `scripts/check_oss.py`（六项）、`scripts/e2e_oss.py`（真实端到端 7 项，写对象跑完自清）。

★★ **协议 bug（已修）**：`oss2` 的 `_normalize_endpoint` 在 endpoint 不带协议时补 **http://** →
签名 URL 全 http → HTTPS 站点浏览器按混合内容拦掉预览/下载，且**后端签发仍成功**、报错只在
浏览器 console。修法：`oss.py` 的 `endpoint_host()` 剥协议头，`_bucket()` 传 `https://{host}`。
回归 `tests/test_oss_url_scheme.py`（8 例，用真实 oss2 算签名）。

★ endpoint 必须公网，写 `-internal` 浏览器直传必失败。
★ 签名 PUT 必须带 `Content-Type`（`presign_put` 签进签名，缺了报 SignatureDoesNotMatch）。
★ `requests.post(data={...含 tuple})` 不生成 multipart → OSS 回 405，须手工拼（`scripts/_oss_probe.py`）。

## PyCharm 调试配置（2026-11-09）

独立模块 `.idea/cloud_web.iml`（根模块 conda 环境**没装 Django**，`cloud_web` 用自己的 `.venv`）。
★ run configuration 用 `SDK_HOME=$PROJECT_DIR$/cloud_web/.venv/bin/python` **显式指路径**，
不依赖 IDE 注册 SDK；`SDK_NAME`/`jdkName` 填已存在的 `parse-video-py`（填不存在的名字会红）。
8 个配置（dev server / django shell / python console / 单测 / check_oss / e2e_oss / vite / worker×2），
详见 `cloud_web/DEBUG.md`。

★ 单测必须 `config.settings.test_local`：① `dev.py` 强制校验 `MYSQL_PASSWORD` 非空；
② ★★ `base.py` 的 `load_dotenv` 会把 `.env` 里**远程调试库**的地址/账号/密码注入环境变量
→ `test_local.py` 必须**写死**本机 3307 + 强制空密码（**别用 `os.getenv`**，会拿到远程值 → 1045）。
★ `gpu_worker` 用**本地 SQLite**，跑 `run_worker` 前必须先 `migrate`（否则 `no such table: local_task`）。

## 数据库与测试环境

- 调试库 `120.25.1.236:19336/test_db`（MySQL 8.4.9，18 表零漂移）；
  ⚠️ 账号无全局 CREATE DATABASE → 单测走本地 3307（`scripts/run_tests_local.py`）。
- 功能冒烟 `scripts/smoke_debug_db.py`（真实库 + 事务回滚，不建测试库）。单测 39 例全绿。
- ✅ **M3.1 mask 人工确认页**（2026-11-05）：`BoxSelector.vue` + `MaskConfirmView.vue` +
  `needs_manual` 全链路闭环；真浏览器 7 项 + 调试库 13 项冒烟全绿。

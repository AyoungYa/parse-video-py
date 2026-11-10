# 项目长期记忆 — parse-video-py

> 详见 `memory/YYYY-MM-DD.md`；本文件只留长期规则与结论。

## 环境与规范

- 解释器 `~/miniconda3/envs/parse-video-py/bin/python`（**无 pytest**）；跑测试用临时 venv（命令见 daily log）。
- src 布局，跑脚本需 `PYTHONPATH=src`。改代码前必读 `docs/knowledge/99_global_index.md`。
- 格式：black/isort/flake8 `line-length=88`，`.flake8` 加 `extend-ignore=E203`。
  ★ lint-venv 的 black 26.x 风格已变（docstring 后加空行），别对旧文件整跑 black。
- extras `web`/`cli`/`wm`/`dev`，**`all` 不含 `wm`**。
- ★★ **别把要持久化的东西放 `/tmp`**（macOS 会清）。持久位置：`~/Library/Application Support/parse-video-py/`。
- `mysqld` 必须 `run_in_background=true` 起（裸 `&` 随 shell 退出被回收）；
  vite 冷启动 9~14s，期间打请求得 502（别误判成代理配错）。

## ★ cloud_web 数据表四条硬规范（2026-11-05 用户拍板，新建表一律照抄）

① **零外键**：FK 用 `db_constraint=False, db_index=False`，索引在 Meta 显式声明；Django 内置表的 CONSTRAINT
由 `scripts/gen_schema_sql.py` 去掉，级联靠 ORM 应用层模拟。
② **DDL 全中文注释**：`Field(db_comment=...)` + `Meta.db_table_comment`；内置表由生成脚本字典补。
③ **索引名** `idx_<table>_<cols>` / `uniq_<table>_<cols>`（**别用 `unique=True`/`db_index=True`**）；
★ MySQL 索引名 ≤30 字符，长表名要先想（captcha 表因此用短名）。
④ **对外业务号**：每张业务表一个 `xxx_no varchar(32)` 唯一不可变，`<前缀><日期><12位hex>`，见 `apps/bizno.py`
（迁移按路径引用，**不要改名/移动/删函数**）。用户端只用它查/改。

建表 SQL 交付 `cloud_web/deploy/init_mysql.sql`，改模型后重跑 `gen_schema_sql.py`（自带 `--check`）。
★★ 给已有表加带 default 的唯一字段：default 对历史行只调一次 → 同值 → 唯一约束必炸；
须在 AddField 与 AddConstraint 之间插 `RunPython` 回填（现成 `apps.bizno.backfill_unique_nos`）。

## cloud_web 云端服务（2026-11-06/09 已真机打通）

细节见 `cloud_web/DEBUG.md` 与 `reference/cloud_web.md`。

- ✅ **OSS 真机全绿**（bucket `wuhenjiangtest`，oss-cn-shenzhen，桶地域不可改）。
  ★★ 协议 bug（已修）：oss2 endpoint 不带协议补 http → 签名 URL 全 http 被浏览器拦；
  修法 `oss.py` 的 `endpoint_host()` 剥协议头。★ endpoint 必须公网；签名 PUT 必须带 Content-Type。
- ✅ **登录 = 手机短信验证码 + 图形验证码闸门**（2026-11-09）：`auth/captcha` 签发 →
  `auth/sms/send` 必须带 captcha_id/captcha_code（顺序：手机号→图形码→短信限流）。
  图形码**落库不用 cache**（locmem 多 worker 不共享）；表名 `captcha`（短名防索引超长）；
  Pillow 出 PNG（load_default(size) 内置字体）。dev 回显 CAPTCHA_DEV_ECHO/SMS_DEV_ECHO 仅本地，生产必须 0。
  阿里云通道 `sms.py`：超时/AK 校验/错误码映射；体检 `scripts/check_sms.py --to 手机号`（真发计费）。
- ✅ **PyCharm 调试**：独立模块 `.idea/cloud_web.iml`（根模块 conda 没 Django）；
  run configuration 用 `SDK_HOME=$PROJECT_DIR$/cloud_web/.venv/bin/python`（8 个配置见 DEBUG.md）。
  ★ 单测必须 `config.settings.test_local`（写死本机 3307+空密码，防 .env 远程凭据注入）；
  本地 MySQL 数据目录在仓库 `.local/mysql3307/`（重启命令见 run_tests_local.py 文档串）。
- ✅ 文案可配置：`OSS_RETENTION_DAYS`（默认 3 天）+ 公开接口 `GET /site/config`，前端文案跟着走。

## 平台防盗链与品牌

微博/新浪 CDN 只放行「浏览器 UA + 站内 Referer」或「完全无 UA」，第三方 Referer 403；
下载走服务端代理 `GET /video/download?url=&filename=`，别前端直链。

品牌标 = **「帧 · 擦」**（方印+播放键+右上弧形擦口+星芒）；改标跑 `docs/brand/gen_assets.py`。
全站字号由 `tokens.css` 的 `--fs-scale: 1.5` 一变量控制（正文 21px）。
playwright 截图：模糊 role 匹配会点到图形码「换一张」按钮；input[type=text] 先命中图形码框。

## 当前待办

- 🔴 **wuhenjiang.com 被冻结**（未实名）→ 备案拒收，需本人身份证实名；清单见 `docs/product/备案与上线清单.md`。
- ⏳ **阿里云短信待填配置**：AK/SK/已备案签名/模板 → `check_sms.py --to` 验证 → 生产切 `SMS_PROVIDER=aliyun`。
- ⚠️ **主体升级**：接支付前升级个体工商户（个人实名做不了微信支付商户号）。
- ⏳ `gpu_worker/` 仍未切 uv。M1 全绿率需「合成语料」（结论见 `reference/wm-gotchas.md`）。

## 去水印核心结论 → 见 `reference/wm-gotchas.md`

（三条铁律 / flicker 归因法 / 验收口径 / 产品版定位判据 / skill 用法）

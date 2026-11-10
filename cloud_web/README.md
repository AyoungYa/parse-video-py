# 无痕匠 · 云端网页服务（cloud_web）

Django + DRF + MySQL + 阿里云 OSS，前端 Vue 3 + Vite。
**这是唯一对外提供服务的组件**，必须有备案域名。

架构详见 `../docs/product/架构设计.md`。

## 它负责什么

- 登录：**大陆手机号 + 短信验证码**，首次登录自动建号（无密码、无注册页）
- 上传凭证签发（浏览器直传 OSS）
- 任务队列与状态机的**唯一权威**（MySQL）
- Worker 协议 4 个端点（算力机来领任务、报进度、报完成）
- 积分账本、签到、订阅
- 交付页：成片下载 + 验收指标报告

它**不碰算力**：一行推理代码都没有，也不中转视频文件。

## 本地跑起来（5 分钟）

本地依赖由仓库根目录的 **uv** 管理（根目录 `pyproject.toml` + `uv.lock`）。装 uv：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # 已有可跳过
```

```bash
cd parse-video-py
uv sync --locked --extra services --extra web --extra cli --extra wm  # 统一安装到根目录 .venv
cd cloud_web

cp .env.example .env && vim .env     # 必填：MYSQL_HOST/PORT/NAME/USER/PASSWORD
```

**数据库统一走远程 MySQL，没有 SQLite 回退** —— 本地和线上用同一种数据库，
避免 JSON 字段、唯一约束、事务隔离上的「本地过、线上炸」。

配置由 `python-dotenv` 自动从 `cloud_web/.env` 读入（已存在的环境变量优先，
所以 Docker 的 `env_file` 不会被文件覆盖），不用每次 `export`：

```bash
../.venv/bin/python scripts/check_db.py     # 先自检：连通性 / 库 / 字符集 / 权限 / TLS
../.venv/bin/python manage.py migrate
../.venv/bin/python manage.py createsuperuser
../.venv/bin/python manage.py runserver 8000      # http://localhost:8000/healthz
```

`.env` 里没填 MySQL 时不会悄悄退回 SQLite，而是直接报：

```
[配置有误，已中止]
数据库配置缺失：MYSQL_HOST、MYSQL_NAME、MYSQL_USER、MYSQL_PASSWORD
```

远程实例的前置条件（缺一个就会卡在连接阶段）：

1. MySQL 账号允许从你的机器 IP 连接：`CREATE USER 'wuhenjiang'@'%'` 或指定具体 IP；
2. 安全组/防火墙放行 3306（生产环境建议只放行应用服务器 IP，不要对 0.0.0.0/0 开）；
3. 库已建好且字符集 utf8mb4：`CREATE DATABASE wuhenjiang CHARSET utf8mb4 COLLATE utf8mb4_unicode_ci;`

`migrate` 只需 `CREATE/ALTER/INDEX` 权限；测试请使用下方的独立账号和测试库。
例如提前创建测试库并授权：

```sql
GRANT ALL PRIVILEGES ON `test_wuhenjiang`.* TO 'test_runner'@'%';
```

云端 MySQL 测试使用独立的 `TEST_MYSQL_*` 配置，不读取上面的业务库账号。
在 `.env` 填入 `TEST_MYSQL_HOST/PORT/NAME/USER/PASSWORD` 后运行：

```bash
../.venv/bin/python scripts/run_tests_cloud.py
../.venv/bin/python scripts/run_tests_cloud.py tests.test_lease_flow
```

`TEST_MYSQL_NAME` 必须是 `test_` 前缀的独立数据库，需提前创建。
测试库默认保留供后续测试复用。测试账号需要该库的迁移和读写权限，
不应拥有业务库的写权限。

前端另开一个终端：

```bash
cd cloud_web/frontend
npm install
npm run dev          # http://localhost:5173，/api 已代理到 8000
```

### 依赖怎么改

**只改仓库根目录的 `pyproject.toml`**，别直接 `pip install`（改了也不进 lock，换台机器就跑不起来）：

```bash
uv add "some-package>=1.0"     # 自动更新 pyproject + lock
uv lock                        # 手改 pyproject 后重新锁定
uv sync --extra services --extra web --extra cli --extra wm  # 同步到仓库根目录 .venv

# Docker 部署依赖另见 cloud_web/requirements.txt。
```

根目录 `uv.lock` 要提交进 git，供本地统一环境复现。

## 生产部署

```bash
cd cloud_web
cp .env.example .env && vim .env      # 填 MySQL / OSS / 域名
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

`docker compose` 会起五个容器：`db`(MySQL) `web`(gunicorn) `frontend`(构建产物) `nginx` `reaper`(租约回收)。

### 注册第一台算力机

```bash
docker compose exec web python manage.py create_worker --name pc-01 --gpu "RTX 4090" --vram 24
```

输出的 `WORKER_TOKEN` **只显示一次**，立刻写进 `gpu_worker/.env`。

### 定时任务（已内置在 reaper 容器）

| 命令 | 频率 | 作用 |
|------|------|------|
| `reclaim_leases` | 30s | 租约超时打回队列 / 判失败退款 |
| `expire_stale_uploads` | 30s | 只传了一半就走人的任务标记过期 |
| `cleanup_oss` | 建议每小时（另配 cron） | 删 24h 前对象，兑现「处理完即删」 |

`cleanup_oss` 没放进 reaper 是为了可 dry-run 观察，建议单独加一条宿主机 cron：

```cron
0 * * * * docker compose -f /srv/cloud_web/docker-compose.yml exec -T web python manage.py cleanup_oss
```

## 关键配置说明

| 变量 | 为什么重要 |
|------|-----------|
| `OSS_*` | AK/SK **只在这里**。算力机拿到的是按对象签发的临时 URL，泄露面仅一个对象 |
| `OSS_ENDPOINT` | 必须**公网**且与桶地域一致。写错地域 OSS 回 301；写 `-internal` 浏览器直传会失败；代码内部统一按 https 处理 |
| `LEASE_SECONDS=90` | 必须 ≥ 心跳间隔的 6 倍。改小会让正常渲染被误判失联 |
| `MAX_ATTEMPT=3` | 重试上限，防止一个坏任务反复烧算力 |
| `MONTHLY_MINUTES_LIMIT=100` | 公平使用上限。**300 会亏钱**（见成本模型） |
| `WORKER_CLAIM_TIMEOUT=25` | 长轮询秒数。gunicorn worker 数要 > 算力机数 × 2 |
| `SMS_PROVIDER` | `console` 只打日志不真发（本地默认）；生产必须 `aliyun` |
| `SMS_DEV_ECHO` | **生产必须 0**。1 会把验证码回显给前端，等于谁都能登录 |

## 自检脚本

配置填完/改动后按顺序跑：

```bash
../.venv/bin/python scripts/check_db.py        # 数据库连通性
../.venv/bin/python scripts/check_oss.py       # OSS 六项：配置/endpoint 形态/地域/SDK/签名URL/PostObject
../.venv/bin/python scripts/e2e_oss.py         # 真实端到端：浏览器直传→确认→签名下载→worker回传（会写对象，跑完自清）
../.venv/bin/python scripts/run_tests_local.py # 本地 MySQL 跑单测（远程账号缺 CREATE DATABASE 时用这个）
```

`check_oss.py` 与 `e2e_oss.py` 都会在 `uploads/__probe__*/` 下写临时对象并在结束时删除；
`e2e_oss.py` 的数据库侧走外层事务回滚，调试库不留脏数据。
| `SMS_ALIYUN_SIGN_NAME/TEMPLATE_CODE` | 短信签名需已备案、模板需审核通过，否则发送接口返回 `isv.SMS_SIGNATURE_ILLEGAL` |

### 短信登录怎么联调

本地 `config.settings.dev` 默认 `SMS_PROVIDER=console` + `SMS_DEV_ECHO=1`：
点「获取验证码」后，验证码会以 `dev_code` 回显在登录页（后端日志里也有一份），
点「填入」即可继续。**这条只存在于 dev，prod 配置里强制关闭。**

上线前要准备：阿里云短信签名（用备案域名/APP 名申请）+ 验证码模板（变量名 `code`），
审核通过后填进 `.env`，把 `SMS_PROVIDER` 改成 `aliyun`。

## 跑测试

```bash
../.venv/bin/python manage.py test tests -v 2
```

测试会连 `.env` 里的同一个 MySQL 实例，**建一个 `test_` 前缀的库跑完即销毁**
（所以账号需要 `CREATE DATABASE` 权限；见上面「远程实例的前置条件」）。
不碰你的业务库，但会短暂占用实例连接。

覆盖四个「改了会出线上事故」的不变量：并发领取唯一性、租约回收、complete/fail 幂等、失败退款。

## 常见问题

**Q：`claim` 一直 504？**
gunicorn worker 被长轮询占满了。加 worker 数，或把 `WORKER_CLAIM_TIMEOUT` 降到 10。

**Q：任务一直「排队中」？**
算力机没在跑，或 token 不对。看 `docker compose logs web | grep 领取`，以及算力机那边 `run_worker` 的日志。

**Q：用户说下载链接 403？**
签名 URL 默认 15 分钟（下载接口单独给 10 分钟）。过期就刷新页面重新获取。

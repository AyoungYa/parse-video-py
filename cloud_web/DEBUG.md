# cloud_web PyCharm 调试配置

## 通过 uv 准备调试环境

在项目根目录执行一次：

```bash
uv sync --locked --extra services --extra web --extra cli --extra wm
```

然后在 PyCharm 选择 `cloud_web dev server (8000)`，点击 **Debug**。该配置使用
`<项目根目录>/.venv/bin/python`（由 uv 管理）运行 `manage.py`，因此断点会直接命中。
依赖变化后重新执行上面的同步命令。不建议把 `uv run` 作为 PyCharm 的调试进程：
它会再启动 Python 子进程，可能导致断点不生效。

本文档说明 `.idea/` 里为 `cloud_web`（云端 Django 服务）新建的 PyCharm 运行配置，
每个配置对应一个可复现的调试场景。配置全部**真跑验证过**，不是照着文档抄的。

## 统一解释器

项目根目录、`cloud_web` 和 `gpu_worker` 共用根目录 `.venv`。
PyCharm 的运行配置保留各自的工作目录，确保各自读取自己的 `.env`。

| 模块 | 解释器 | 管什么 |
|---|---|---|
| `parse-video-py` | `<项目根目录>/.venv/bin/python` | 解析器 CLI、wm-studio（8010） |
| `cloud_web` | `<项目根目录>/.venv/bin/python` | 云端 Django |
| `gpu_worker` | `<项目根目录>/.venv/bin/python` | 算力节点 |

> run configuration 的解释器应显示 `<项目根目录>/.venv/bin/python`。

## 配置清单

### 后端

| 配置名 | 命令 | 说明 |
|---|---|---|
| `cloud_web dev server (8000)` | `manage.py runserver 127.0.0.1:8000` | 主开发服务器，断点直接命中断言/视图 |
| `cloud_web django shell` | `manage.py shell` | IPython 交互 shell，可直接 `from apps.tasks.models import Task` |
| `cloud_web python console (Django ORM)` | — | PyCharm 的 Python Console 面板，等价于 shell 但带补全 |
| `cloud_web 单测 (本地 MySQL 3307)` | `manage.py test tests --noinput` | 跑全量单测（39 例） |
| `cloud_web OSS 体检 (check_oss)` | `scripts/check_oss.py` | OSS 六项连通性自检 |
| `cloud_web OSS 端到端 (e2e_oss)` | `scripts/e2e_oss.py` | 真 OSS 全链路 7 项（会写对象，跑完自清） |

### 前端

| 配置名 | 命令 | 说明 |
|---|---|---|
| `cloud_web 前端 vite (5173)` | `npm run dev` | Vite 开发服务器 |

### 算力机（gpu_worker）

| 配置名 | 命令 | 说明 |
|---|---|---|
| `gpu_worker migrate (本地 SQLite)` | `manage.py migrate` | **run_worker 前必须先跑这个** |
| `gpu_worker run_worker (算力机主循环)` | `manage.py run_worker` | worker 主循环，断点可挂在流水线里 |

## 三个必须知道的坑

### 1. `DJANGO_SETTINGS_MODULE` 要显式给 `dev`

`cloud_web/config/settings/` 下有三个：`base` / `dev` / `prod`。
`prod.py` 会开 `SECURE_SSL_REDIRECT=1` 和 HSTS，**本地跑会 301 死循环**。
所有 cloud_web 配置都显式设 `DJANGO_SETTINGS_MODULE=config.settings.dev`。

### 2. 单测必须用 `test_local` + 本地 MySQL 3307

远程调试库账号只有库级授权，没有全局 `CREATE DATABASE`，
`manage.py test` 需要建 `test_wuhenjiang` 临时库 → 直接失败。
本地 3307 实例的 root 是免密的，专门跑单测。

> ⚠️ **本地 MySQL 数据目录不要放 `/tmp`** —— macOS 会定期清理 `/tmp`，
> 实测已被清掉两次，导致 `mysqld` 报 `Can't change dir to '/tmp/pvp-mysql/data/'`。
> 现放在持久位置：`~/Library/Application Support/parse-video-py/mysql3307`。

```bash
MYSQL_HOME="$HOME/Library/Application Support/parse-video-py/mysql3307"

# 首次初始化（root 空密码）
mkdir -p "$MYSQL_HOME/data"
/usr/local/mysql/bin/mysqld --initialize-insecure \
  --datadir="$MYSQL_HOME/data" --basedir=/usr/local/mysql

# 启动（注意：必须后台跑，直接 & 会随 shell 退出被回收）
/usr/local/mysql/bin/mysqld --datadir="$MYSQL_HOME/data" --port=3307 \
  --socket="$MYSQL_HOME/mysql.sock" --pid-file="$MYSQL_HOME/mysqld.pid" \
  --bind-address=127.0.0.1 --log-error="$MYSQL_HOME/error.log" &

# 建库
/usr/local/mysql/bin/mysql -h 127.0.0.1 -P 3307 -u root \
  -e "CREATE DATABASE IF NOT EXISTS wuhenjiang CHARACTER SET utf8mb4;"
```

配置里用 `config.settings.test_local`（不是 `dev`）——两个原因：
① `dev.py` 强制校验 `MYSQL_PASSWORD` 非空，而本地 root 是空密码；
② ★ `base.py` 的 `load_dotenv` 会把 `.env` 里**远程调试库**的地址/账号/密码
（`120.25.1.236` / `test_db_rw`）注入环境变量，`test_local.py` 因此**写死**
本机 3307 参数并强制空密码 —— 副作用是好的：本地单测永远不会误连远程库。

### 3. `run_worker` 前必须先 migrate

`gpu_worker` 用的是**本地 SQLite**（`worker.sqlite3`），不是 MySQL。
第一次跑会直接崩：`no such table: local_task`。
所以先跑 `gpu_worker migrate (本地 SQLite)` 配置。

另外 worker 需要 `WORKER_TOKEN`（云端 `create_worker` 生成，只显示一次），
缺了会报清晰的 `RuntimeError: WORKER_TOKEN 未配置…`，把它写进 `gpu_worker/.env`。

## 前后端联调

两个配置一起跑：

- `cloud_web dev server (8000)` → Django API
- `cloud_web 前端 vite (5173)` → 前端页面，访问 `http://127.0.0.1:5173`

Vite 已配好代理：`/api` → `http://127.0.0.1:8000`（见 `frontend/vite.config.ts`），
所以**不需要管 CORS**，前端直接打 5173 就行。

验证过的响应：

| 请求 | 结果 |
|---|---|
| `GET http://127.0.0.1:8000/healthz` | 200 `{"ok": true, "service": "cloud_web"}` |
| `POST /api/v1/uploads/sign`（未登录） | 401（业务响应，说明路由通） |
| `GET http://127.0.0.1:5173/` | 200 首页 HTML |
| `POST http://127.0.0.1:5173/api/v1/uploads/sign` | 401（经 vite 代理，代理链通） |

## 常用排查命令

```bash
cd cloud_web

# 端口占用
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN

# Django 配置自检
DJANGO_SETTINGS_MODULE=config.settings.dev .venv/bin/python manage.py check

# 迁移漂移检查
DJANGO_SETTINGS_MODULE=config.settings.dev .venv/bin/python manage.py makemigrations --check --dry-run

# OSS 体检 / 端到端
.venv/bin/python scripts/check_oss.py
.venv/bin/python scripts/e2e_oss.py
```

## 注意

`.idea/workspace.xml` 被 gitignore（PyCharm 每次都会重写），
但 `.idea/runConfigurations/*.xml` **要提交** —— 这样团队成员 clone 下来就能用同一套调试配置。

改配置后建议实际跑一遍再提交，别只看 YAML/XML 顺眼。

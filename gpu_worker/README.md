# 无痕匠 · 算力机服务（gpu_worker）

Django 服务，跑在**你自己的 PC**（或 Mac）上。它只做一件事：**主动向云端领任务，在本地跑 LaMa，把成片传回去**。

不监听公网端口、不需要公网 IP、不需要内网穿透——所有连接都是**出站**的。

## 它负责什么

- 长轮询云端 `/worker/claim` 领任务（拿到的是签名 URL，不持有 OSS 凭证）
- 从 OSS 下载原片 → 跑 mask/LaMa/verify → 回传成片
- 心跳续租、上报进度与验收指标
- 本地状态面板（`http://127.0.0.1:8040/`），看清「这台机器在干什么」

## 装之前先体检（省你 3 小时）

```bash
nvidia-smi
```

- 有输出且显存 **≥ 6GB** → 可以跑，继续
- 报「不是内部或外部命令」→ 没 N 卡或没装驱动，先确认显卡型号。**A 卡/核显跑不了 CUDA**，别装了

判定标准见 `../docs/product/算力部署方案.md` §7。

## 安装（Windows 原生，推荐）

```powershell
cd gpu_worker
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# torch 必须匹配你的 CUDA 版本，别直接 pip install torch
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

copy .env.example .env
notepad .env      # 填 WORKER_TOKEN / WM_SCRIPTS_DIR / WORKSPACE_DIR
```

`.env` 里 `WM_SCRIPTS_DIR` 指向修复脚本所在目录（skill 的 `scripts/` 或本仓库 `src/skill/`），
里面应有 `lama_apply_maskonly.py`、`build_mask.py`、`verify.py`。

初始化并试跑一次：

```powershell
python manage.py migrate
python manage.py run_worker --dry-run   # 只领任务不真跑，验证连通性
python manage.py run_worker --once      # 处理一条就退出，验证管线
python manage.py run_worker             # 常驻
```

本地面板：另开一个终端 `python manage.py runserver 8040`，访问 http://127.0.0.1:8040/

## 安装（Linux + Docker）

```bash
cd gpu_worker
mkdir -p scripts workspace data
# 把修复脚本放进 scripts/
cp .env.example .env && vim .env
docker compose up -d --build
docker compose logs -f worker
```

需要宿主机装 `nvidia-container-toolkit`，否则容器看不到显卡。

## 开机自启（重要）

本地机器跑生产最怕「人不在家服务就停了」。

**Windows（任务计划程序）**

1. 任务计划程序 → 创建任务
2. 触发器：登录时（勾选「延迟 30 秒」让驱动先就位）
3. 操作：`C:\path\to\gpu_worker\.venv\Scripts\python.exe`，参数 `manage.py run_worker`，起始于 `C:\path\to\gpu_worker`
4. 设置：勾选「如果任务失败，每 1 分钟重启，最多 999 次」

**Linux/macOS（systemd）**

```ini
[Unit]
Description=Wuhenjiang GPU Worker
After=network-online.target

[Service]
WorkingDirectory=/srv/gpu_worker
ExecStart=/srv/gpu_worker/.venv/bin/python manage.py run_worker
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## 处理阶段是怎么定义的

阶段命令写在 `config/settings.py` 的 `WM_STAGES_TEMPLATE`，用占位符拼：

```
{python} {scripts}/lama_apply_maskonly.py --input {input} --mask {mask} \
  --out {out} --bbox {bbox} --pad {pad} --device {device} --keyframe-every 1
```

**不同版本的 skill 脚本参数名可能不同**——改这里适配，不用改代码。
判定成功**只看退出码**（踩坑手册铁律 2），不看日志里有没有「完成」。

## 三条容易踩的坑

| 坑 | 现象 | 处理 |
|----|------|------|
| 心跳被占满 | 渲染时云端判失联、任务被回收 | 心跳是**独立线程**，已处理。别把 `HEARTBEAT_INTERVAL` 调到 > 15 秒 |
| OOM | 4K 大水印任务崩在 `cuda out of memory` | 报错会标 `retryable=false`，不浪费重试次数。降 ROI 或等云端加内存 |
| 上行堵死 | 同时传多个成片，家里网变慢 | `UPLOAD_CONCURRENCY=1`，别调大 |

## 跑测试

```bash
python manage.py test tests -v 2
```

覆盖进度解析、指标归一化（尤其「指标缺失时绝不能报全绿」这条）。

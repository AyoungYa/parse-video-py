# 无痕匠 · 算力机处理服务（gpu_worker）

Django 服务，跑在**你自己的 PC**（或 Mac）上。它只做一件事：**主动向云端领任务，在本地跑 LaMa，把成片传回去**。

不监听公网端口、不需要公网 IP、不需要内网穿透——所有连接都是**出站**的。

## 它负责什么

- 长轮询云端 `/worker/claim` 领任务（拿到的是签名 URL，不持有 OSS 凭证）
- 从 OSS 下载原片 → **自动定位水印 → 建 mask → LaMa 修复 → 自动验收 → 决策树修复** → 回传成片
- 心跳续租、上报进度与验收指标
- 本地状态面板（`http://127.0.0.1:8040/`），看清「这台机器在干什么」

---

## ★ 管线脚本（`scripts/`）——产品代码

这一目录是**产品代码**（不是临时脚本）。与 `video-watermark-removal` skill 的差异：

| 脚本 | 职责 | 与 skill 版的差异 |
|------|------|-----------------|
| `wm_common.py` | 公共工具（ffmpeg 探测、bbox 解析、退出码约定） | 新增，跨平台探测替代 homebrew 硬编码 |
| `build_mask.py` | **自动定位 + 建精确 mask** | skill 版只建 mask 且要求人工给窗口；这里能自动定位 |
| `lama_apply.py` | LaMa 逐帧修复（mask alpha 合成） | skill 版整块 ROI 回贴；这里只贴 mask 内像素（铁律 2） |
| `verify.py` | 四项指标 + 决策树诊断 | skill 版是 CLI 三件套；这里输出结构化 report.json |
| `texture_fix.py` | 补纹理（A 类痕迹）/ 行插值（B 类痕迹） | 只改 CLI 层（`--input/--bbox` 契约） |
| `rowfill.py` | 渐变背景行插值 | 同上，只改 CLI 层 |

### 退出码约定（pipeline.py 侧有对应分支，改一处必须同步另一处）

| 码 | 含义 | 上层动作 |
|----|------|---------|
| 0 | 成功 | 继续下一阶段 |
| 1 | 失败 | 按 `retryable` 决定是否重试 |
| **3** | **定位置信度不足** | **转人工拖框，不重试**（重试没意义） |

### ★ 自动定位的四道判据（`build_mask.auto_locate`）

每条都对应一个真实失败案例：

1. **恒亮度**：min 投影 > 高阈值（≥225）
   - 纯白水印（微博磨砂卡片）min 投影可达 246 ✅
   - 半透明水印（微博视频号 logo，60% 不透明）最高只有 ~217 ❌ 漏掉
2. **值高 + 低波动**：`score = 时域 p90 − 2 × 时域 std`
   - 能抓半透明水印 ✅
   - 但大片静态高亮区（舞台背景 506520px）会抬高相对分数，挤掉真水印 ❌
   - → **两条通道并行跑，取更可信的那个**
3. **位置恒定性**：区内时域 std ÷ 同高度周边带 std < 1.05
   - 水印核心块 0.70~0.80，人像高光 0.91~1.58 ✅ 能区分
   - 阈值不能更紧：0.80 会把真水印的次要块滤掉（只抓到 2 块/宽 105px）
4. **水平块簇**：水印 = 水平排列的多个相邻小块
   - 聚类**只看水平相邻，不要求 y 区间重叠**（微博水印是两行，y 完全不重叠）
   - 单块超过 8000px 一律拒绝（那是画面内容不是水印）

### ★★ 三个必须知道的失效点

1. **片尾黑场会毁掉定位**：54s 样片片尾有 6 帧均值 1.3~2.5，
   时域分位数被拉到 0 → 水印特征消失 → 定位直接失败。
   → 抽帧后**先剔除暗帧**（均亮 < 20）。
2. **触边 ≠ 水印被切**：微博水印就在画面角落，内容必然贴边。
   → 只有「该方向还能往外扩」时触边才需要扩窗；扩窗**只做一次**
   （否则水印横跨窗口时一路扩到画面边缘 → 死循环 → 转人工）。
   → 扩窗后必须在新窗口**重算 mask**（否则 mask 坐标与 bbox 错位）。
3. **精修阈值不能自适应**：扩窗后窗口里混进大片背景高光，
   自适应阈值会随窗口大小漂移（实测 mask 从 5000px 涨到 114699px）。
   → 绝对通道用全局固定阈值，score 通道用绝对下限。

### ★ 闪烁判据为什么不用「÷ 原片水印区地板」

原片水印区是**静止叠加层**，帧间差几乎为 0（实测 0.064）。
成片哪怕只有 0.3 的噪声，除以 0.064 也放大成 4.8 倍 —— 假警报。
→ 改成 **修复区帧间差 ÷ 同帧环带帧间差**（环带是同一画面的真实运动量），
环带静止时退回绝对值口径（上限 1.0）。

### ★ 决策树（`verify.diagnose`）

```
全绿 → clean，直接交付
σ比 < 0.8（过平滑）      → needs_texture   → texture_fix --strength 1.3
Laplacian比 > 1.5（伪纹理）→ needs_rowinterp → texture_fix --lf-mode rowinterp
其他                       → needs_review    → 转人工
```

修复轮跑完会**再验一次**；两轮都不绿时交付**评分更好**的那份
（`_score()`：全绿优先，再看 σ 比是否接近 1.1、Laplacian 与闪烁的超出量）。

---

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

`.env` 里 `WM_SCRIPTS_DIR` 默认指向本仓库 `scripts/`（**自带自动定位能力**，比 skill 版强）。
若指向 skill 的 `scripts/`，只能跑人工给 bbox 的流程。

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
mkdir -p workspace data
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
{python} {scripts}/lama_apply.py --input {input} --mask {mask} \
  --out {out} --bbox {bbox} --pad {pad} --device {device} --keyframe-every 1
```

**不同版本的脚本参数名可能不同**——改这里适配，不用改代码。
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

覆盖进度解析（阶段标记 `[1/3]` 不被误当帧进度）、bbox 回填、指标归一化
（尤其「指标缺失时绝不能报全绿」这条）、两轮成片择优评分。
**不涉及 GPU 与云端**，跑得很快。

# wm-studio · 去水印调参台

`video-watermark-removal` skill 的**图形调参前端**。把 skill 里的 CLI 脚本当库调用，
在浏览器里拖框选水印、调阈值、**秒级看单帧修复效果**、看验收指标，满意后一键跑全片。

## 为什么需要它

skill 原本是一套 CLI 脚本，调一个参数要：改命令 → 跑 → 等渲染 → 抽帧 → 用 verify 看指标。
一轮几分钟，而 `white_th` / `grow` / mask 策略这些参数**必须靠看图才对得准**。
这个工具把这一轮压到**亚秒级**：

| 环节 | CLI | 调参台 |
|---|---|---|
| 调 mask 阈值要看效果 | 重建 mask（40s）+ 渲染 + 抽帧 | **2.8s** 重建 mask，图上直接看红色覆盖 |
| 判断某一帧修得如何 | 抽帧 + 跑 verify | **0.3~0.5s** 点一下 |
| 判断有没有接缝/残留 | 出对比图再打开看 | A/B 拖动滑块 + 三联差值图，浏览器内 |
| 全片渲染 | 记命令行参数 | 页面上点一下，进度/日志/成片链接自动出 |

## 快速开始

```bash
# 1. 装依赖（torch 体积大，单独成组，不影响主项目的轻量定位）
uv pip install -e ".[web,wm]"

# 2. 启动
uv run wm-studio                     # → http://127.0.0.1:8010/wm（前台，Ctrl+C 停）
uv run wm-studio --port 8020         # 换端口
uv run wm-studio --reload            # 改代码自动重载
```

**想让它常驻（关终端也不停）**，用脱离会话的启动器：

```bash
python demo/wm_studio_launch.py start    # 后台启动，日志+PID 落盘
python demo/wm_studio_launch.py status   # 看是否在跑
python demo/wm_studio_launch.py stop     # 停掉
```

日志在 `wm_studio_workspace/server.log`，PID 在 `server.pid`。
用 `start_new_session=True` 起独立进程组，关终端 / 父进程退出都不会回收它。

也可以挂在主服务下：`uv run python -m parse_video_py.main` → `http://localhost:8000/wm`
（没装 `[wm]` extras 时主服务照常启动，只是 `/wm` 不可用，不会报错）。

> 依赖 `ffmpeg` / `ffprobe`（Homebrew 装在 `/opt/homebrew/bin`）。
> 装在别处用环境变量指定：`WM_FFMPEG=/path/to/ffmpeg`。
> skill 不在默认位置时用 `WM_SKILL_DIR=/path/to/video-watermark-removal/scripts`。

### 在 PyCharm 里调试

仓库已自带两个运行配置（`.idea/runConfigurations/`），打开 PyCharm 后
右上角运行配置下拉框里直接选，**打断点即可调试**：

| 配置名 | 入口 | 说明 |
|---|---|---|
| `wm-studio 调试 (8010)` | `src/parse_video_py/wm_studio/server.py` | **首选**。走 argparse 直跑 `main()`，栈最干净，断点直接落在 `api.py` |
| `wm-studio uvicorn 调试 (8010)` | `uvicorn parse_video_py.wm_studio.server:build_app --factory` | 与 `uv run wm-studio` 完全等价，适合排查 uvicorn 层行为 |

两者都已配好：

- Interpreter：项目 SDK `parse-video-py`（= `~/miniconda3/envs/parse-video-py/bin/python`）
- `PYTHONPATH=$PROJECT_DIR$/src`（src 布局）
- `PYDEVD_DISABLE_FILE_VALIDATION=1`（少一堆 `sys.settrace` 警告）
- `WM_SKILL_DIR` / `WM_WORKSPACE` 显式钉死，IDE 里不受 shell 环境污染

> ⚠️ **调试模式不要开 `--reload`**：reloader 会把代码跑在子进程里，
> PyCharm 的断点挂不上父进程。改代码重载请用命令行 `uv run wm-studio --reload`。

> ⚠️ **调试前先停掉常驻实例**（`python demo/wm_studio_launch.py stop`），
> 否则 8010 端口被占，PyCharm 里启动会直接报 `Address already in use`。

## 使用流程

1. **载入**：填视频路径 → 点「载入」。同时会预热 LaMa（约 3.5s，只做一次）。
2. **框选**：在图上直接拖框，或改 X/Y/宽/高四个数字。
3. **建 mask**：
   - 先点「按背景自动定阈值」看背景多亮（亮背景必须上调 `white_th`，否则整个背景会被吃进 mask）；
   - 再点「重建 mask」，图上会显示红色覆盖区 + 右上角放大插图。
4. **预览调参**：切「成片 / A/B / 三联+差值」，拖帧号看不同亮暗场景，调参数再点预览。
   - **A/B**：左原片右成片，拖中间分隔线，最能看出「有没有痕」；
   - **三联+差值**：差值图放大 6 倍，残留一目了然。
5. **看指标**：右侧四项绿/红标识，订的是同一套判据（见下）。
6. **全片渲染**：点「开始全片渲染」，进度条 + 实时日志，完成后给成片链接。

## 四项指标（对应 `verify.py`，但取样改成本地单帧）

| 指标 | 判据 | 防什么 |
|---|---|---|
| 笔画残留降幅 | ≥ 90% | 水印没修干净 |
| 修复区最亮值 | ≤ 原片同区 | LaMa 把旁边亮内容「延续」进笔画位，重绘成亮块 |
| 边界梯度比 | ≤ 1.5（理想 ≈ 1.0） | 修复区与周边有接缝，看得出方框 |
| 纹理匹配 σ 比 | 0.8 ~ 1.6 | < 0.8 发糊（需 `texture_fix`）；> 1.6 是伪纹理 |

**「笔画残留降幅」用的是相对背景口径**（模板位 − mask 外环带），
不是「> 150 的像素数」——后者在亮背景片（背景 ~205）会把整片背景算成残留，
报出"降幅 15%"的假警报（踩坑手册坑 43）。

> 指标是**当前这一帧**的。它用于调参取舍；最终交付仍建议跑 `verify.py` 做全片验收
> （时域中值 + 帧完整性 + mask 外零干扰）。

## 架构

```
src/parse_video_py/wm_studio/
├── paths.py     skill 脚本定位、工作区目录、ffmpeg 探测
├── video.py     探测 / 精确抽帧（输入级 seek，4K 长片 0.3s）
├── engine.py    ★ LaMa 单例 + 按 mask alpha 合成 + 稀疏→全卡片 mask
├── masks.py     调 build_mask.py（含 3s 快速路径）
├── metrics.py   四项指标
├── preview.py   单帧预览管线
├── jobs.py      全片渲染任务（子进程 + 日志轮询 + 停止）
├── api.py       FastAPI 路由
├── server.py    独立启动入口
└── templates/wm_index.html   单页界面（原生 JS，无框架）
```

### 参数速查（页面上每个参数右边都有 `?`，点开看详解）

四个参数分属三个层次，**别四个一起动**：

| 参数 | 管什么 | 层 | 默认 | 建议范围 |
|---|---|---|---|---|
| **白字阈值** | 「多亮才算水印白字」的灰度门槛 | 建 mask | 168 | 暗 150~180 / 亮 215~230 |
| **膨胀** | 白字 mask 外围扩多少，补半透明柔边 | 建 mask | 21 | 15~25 |
| **pad** | LaMa 上下文画布（ROI 外扩 `pad×3`）+ mask 额外膨胀 `pad//3` | 修复 | 10 | 8~14 |
| **crf** | x264 编码质量，**不参与修复** | 编码 | 17 | 16~20 |

调参顺序：`白字阈值 → 膨胀 → pad → crf`。前三步在页面上都是亚秒级反馈；
**crf 只在全片渲染时生效，定了就别再动**（改它要重渲一遍）。

### 几个关键设计

**预览走进程内常驻模型，全片走子进程。**
`lama_apply.py` 每次启动要加载 big-lama.pt（约 2s），调参时每改一个滑块重启一次完全不可接受
→ 预览用进程内单例，模型加载一次常驻。但全片渲染（7239 帧要 52 分钟）会占住 GIL/MPS
拖死 Web 服务 → 走 `subprocess.Popen(..., start_new_session=True)`，关掉浏览器也不中断。

**取帧必须输入级 seek。**
`-ss` 放在 `-i` 之前才是关键帧跳转（毫秒级）；放在 `-i` 之后要先解码再丢弃前面所有帧，
4K 长片取第 7000 帧实测要 **30 秒**。改前置后 0.3 秒，且与全片精确挑帧逐像素一致。

**任务成败看退出码，不看日志有没有「完成」。**
shell 里 `echo '=== ALL DONE ==='` 只要脚本跑到末尾就会打印，中间命令全崩也照打。
现在的判据是：`set -o pipefail` + 每阶段检查 `$?` + 结尾写 `=== EXIT rc=N ===`，
并且 rc=0 时还要**确认成片文件真的存在**（实测踩过：任务报 done 但成片根本不存在）。

**mask 重建有两档速度。**
`build_mask.py` 的 `sample_stack` 是**每帧启一个 ffmpeg**，200 帧 = 200 次进程启动，
8 秒 1080P 片要 40 秒。默认走本地快速路径（单次解码批量抽帧，2.8s，与脚本 mask 的 IoU 0.957）；
取消「快速建 mask」才回脚本，那档会向窗口外探测、确认没水印漏在框外。

**★★ 抽帧必须按内存分流，否则长片会 OOM。**
老的 `grab_frames_many` 一律「整区间解码」——`select='between(n,lo,hi)'` 把 `[lo,hi]`
**所有帧**的 rawvideo 用 `capture_output=True` 一次性读进内存，再挑需要的那 120 帧。

| 片 | 区间帧数 | 单帧 BGR | 峰值（整区间） | 真正需要 |
|---|---|---|---|---|
| 8s 1080P | 166 | 5.9 MB | **1.7 GB** | 738 MB |
| 152s 1440P | 3739 | 7.9 MB | **29 GB** 💥 | 984 MB |
| 290s 2560×1440 | 7177 | 10.5 MB | **77 GB** 💥 | 1.3 GB |

浪费 **2~60 倍**，而且 4K 长片在 32GB 机器上必崩。现在两条路径按 `max_bytes` 自动选：

- 跨度 × 单帧 ≤ 上限（默认 `BATCH_DECODE_LIMIT = 1 GiB`）→ 整区间解码，快；
- 超限 → **逐帧输入级 seek**，内存恒定 = 单帧（4K 也才 24 MB），与片长无关。

同时采样帧数从 120 降到 **60**（min 投影对面积的估计 40~60 帧已收敛，
实测同 bbox 下 mask **IoU = 1.0000、逐像素 100% 相同**）。

实测效果：152s 1440P 片 **30 GB → 637 MB**；290s 4K 片 **77 GB → 782 MB**。
回归测试见 `tests/test_wm_studio.py::TestBatchDecodeMemoryGuard`。

## 工作区

默认 `wm_studio_workspace/`（项目根目录下），结构：

```
wm_studio_workspace/
├── jobs.json                    任务表（Web 重启后能读回）
├── masks/<片名>_<bbox>_t<阈值>_g<膨胀>.png
└── <任务ID>/
    ├── run.sh                   实际执行的阶段脚本
    ├── run.log                  完整日志（增量轮询）
    └── <片名>_clean.mp4         成片
```

用 `WM_WORKSPACE=/elsewhere` 可以换目录。

## 常见问题

**「按背景自动定阈值」建议值和默认的 168 差很多？**
正常。它按背景中位亮度算（背景 207 → 建议 222）。168 只适合暗背景片。

**预览很快但全片很慢？**
正常。预览只跑单个小 ROI；全片是逐帧推理 × 总帧数（261021 片 240 帧约 84s，
4K 7239 帧约 52 分钟）。看日志里的 `x.xx s/frame` 估总时长。

**指标全绿但还是看着有痕？**
指标有盲区：它测的是数值残留，测不到「LaMa 把纹理抹成平涂」这类结构问题。
这种情况看 σ 比 + 差值图；仍不满意就跑 `texture_fix.py`（`texture_mode=on`）。

**渲染中途关掉浏览器会怎样？**
任务继续跑（已脱离会话）。重开页面任务状态还在（jobs.json），但页面不会自动接上日志流。

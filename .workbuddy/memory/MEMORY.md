# 项目长期记忆 — parse-video-py

## 环境

- 本项目的解释器：`~/miniconda3/envs/parse-video-py/bin/python`（**未安装 pytest**）。
  需要跑测试时用临时环境：`uv venv /tmp/pvp-test-venv --python 3.12 && uv pip install --python /tmp/pvp-test-venv/bin/python -e ".[all,dev]"`，
  再 `PYTHONPATH=src /tmp/pvp-test-venv/bin/python -m pytest tests/ -q`。
- 项目使用 src 布局，直接跑脚本需带 `PYTHONPATH=src`。

## 规范

- 修改代码前必读 `docs/knowledge/99_global_index.md` 定位文档（见 AGENTS.md）。
- 代码格式：black/isort `--line-length=88`，flake8 `--max-line-length=88`（配置在 .pre-commit-config.yaml，pyproject 的 black 段同为 88）。`.flake8` 里 `extend-ignore=E203`（black 切片空格冲突的官方推荐）。
- 新增 API 路由放 `src/parse_video_py/web.py`；工具函数一律放 `src/parse_video_py/utils.py`。
- `pyproject.toml` 的 extras：`web`（FastAPI 系）/ `cli` / `wm`（torch 系，约 2GB）/ `dev`。
  **`all` 刻意不含 `wm`**——别把"装全部"变成拉 2GB。

## ★ 去水印调参台 wm-studio（2026-10-27 新建）

做去水印时**先用它调参**，别再在命令行里盲调（一轮几分钟 → 亚秒级）。
代码 `src/parse_video_py/wm_studio/`，文档见其 `README.md`。

- 启动 `uv run wm-studio` → `http://127.0.0.1:8010/wm`
  （或用 `--reload`；也可挂在主服务 `/wm` 下）
- 单帧预览 **约 300~500ms**（MPS）、建 mask **2.2s**、全片 240 帧 **84s**
- 页面上直接出四项指标（降幅/区max/边界梯度比/σ比，绿红标识，判据同 verify.py）
- 工作区 `wm_studio_workspace/`（已 gitignore）

**PyCharm 调试**：`.idea/runConfigurations/wm_studio.xml`（首选，直跑 `server.py` 的 `main()`）
和 `wm_studio_uvicorn.xml`（`uvicorn ...:build_app --factory`）。**调试别开 `--reload`**（断点挂不上子进程）；
调试前先 `wm_studio_launch.py stop` 腾出 8010。
启动器 `demo/wm_studio_launch.py` 用 `sys.executable`，要用装了依赖的 conda 解释器去跑它。

**四条硬教训（都在代码里固化了，改它之前先读）**：1. **`-ss` 必须在 `-i` 之前**才是关键帧跳转；放之后要先解码再丢前置帧，4K 取第 7000 帧要 30s。
2. **任务成败看退出码，不看日志里有没有「完成」**——`echo 'ALL DONE'` 是 shell 回显，
   中间命令全崩也照打（实测把崩溃报成成功）。现用 `=== EXIT rc=N ===` + 确认成片存在。
3. **子进程 cwd 换了就必须传绝对路径**（相对路径在那里解不开 → ffprobe 返回空 → 崩）。
4. **`build_mask.py` 的 `sample_stack` 是每帧启一个 ffmpeg**（200 帧 40s）；
   调参台内置单次解码批量抽帧的快速路径（2.8s，IoU 0.957）。
5. **★★ 抽帧必须按内存分流**：`subprocess.run(capture_output=True)` 拿 rawvideo 时
   内存 = **输出字节数**。老实现整区间解码 `[lo,hi]`，152s 1440P 片要 **29GB**、
   4K 290s 片要 **77GB**（真正需要的只有 ~1GB，浪费 30~60 倍）。
   现按 `BATCH_DECODE_LIMIT = 1 GiB` 分流：超限走**逐帧输入级 seek**（内存恒定=单帧）；
   采样帧数 120→60（实测同 bbox 下 mask **IoU 1.0000、逐像素 100% 相同**）。
   实测 30GB→637MB、77GB→782MB。回归测试 `TestBatchDecodeMemoryGuard`。

## ★★ 验收口径警告：中值口径会掩盖稀疏 mask 的残留

`demo/weibo-261021_clean.mp4`（根目录那个）实测**白像素残留 3719、只降 51%**，
但 `verify.py` 的**时域中值口径**报 94% 达标 —— 中值把残留糊掉了。

→ **交付前除了 `verify.py`，再加一道「逐帧相对口径」抽查**：
`模板位均值 − mask 外 11px 环带均值`，|Δ| < 3 且 mask 内 `>235` 白像素接近 0。
干净的版本参考：`demo/wm_261021/weibo-261021_clean_v2.mp4`（Δ −0.3，白像素 0）。


## 平台知识（防盗链）

- 微博/新浪 CDN（`f.video.weibocdn.com`、`sinaimg.cn`）防盗链规则：只放行「浏览器 UA + 微博站内 Referer」
  或「完全不带 UA 的请求」；带第三方 Referer 一律 403。
- 下载类功能不要用前端直链，走 `GET /video/download?url=&filename=` 服务端代理（自动匹配平台 Referer + UA）。

## ★★★★ 去水印三条铁律（2026-10-26 三轮返修换来的，务必先读）

**完整参数记录见 `demo/wm_wcj/参数记录.md`（含可复制的完整命令与实测指标）。**

### 铁律 1：mask 必须含「半透明柔边层」，用 min>100 而不是 white-th 168

`build_mask.py --white-th 168`（默认）只抓够白的像素，**漏掉水印的半透明柔边（灰度 100~168）**。
后果：mask 外漏 571px，成片比邻域亮 **+100**，肉眼可见白色残影（本项目 wcj 片真实踩坑）。

**正解**（替代 build_mask.py，直接写）：
```python
F  = np.stack([每帧灰度 for t in 1..N])   # 1fps 足够
mn = F.min(axis=0)                        # 时域最小投影：水印恒定亮
m  = (mn > 100).astype(np.uint8)          # ★ 100 而非 168 —— 这是关键
m  = cv2.morphologyEx(m, MORPH_CLOSE, np.ones((5,5),np.uint8))
m  = cv2.dilate(m, np.ones((3,3),np.uint8), 1)   # 盖抗锯齿边
# 去掉 <20px 碎噪连通块
```
自检：内容距 bbox 各边 > 3px（否则扩窗口）。

### 铁律 2：技能原版 `lama_apply.py` 整块回贴 ROI，稀疏 mask 会发糊

原版第 94 行 `result[y0:y1, x0:x1] = cv2.cvtColor(out_arr, ...)`
—— **ROI（bbox + pad×3）整块被 LaMa 重建值覆盖，mask 外真实像素也被换掉**，
水印周边一圈发糊（本项目实测真实像素高频只剩 39%）。
**必须改成按 mask alpha 合成**（副本 `demo/wm_wcj/lama_apply_maskonly.py`）：
```python
a = (mask > 127).astype(np.float32); a = cv2.GaussianBlur(a,(0,0),1.0)
base = frame_bgr[y0:y1, x0:x1].astype(np.float32)
result[y0:y1, x0:x1] = (base*(1-a[...,None]) + inp*a[...,None]).astype(np.uint8)
```

### 铁律 3：验收阈值必须 ≥ 建 mask 的阈值，否则判据自证干净

只验 min>180 的白字模板，**永远看不到半透明层残留**（本项目连续两轮误报"干净"）。
**用 `demo/wm_wcj/accept_all.py` 做四层：min>200 / >180 / >140 / >100**，
判据「成片该位 − 同帧 11px 邻域中值」，|中值|<5 且 |最差帧|<15。

### 判「发糊」的唯一可靠指标：梯度 p50

`细节比/σ比` 对**结构塌陷不敏感**（全卡片方案细节比 1.01 却烂，因为梯度 p50 = **0.0**，
一半像素梯度为零=被抹成平涂）。**测 mask 内梯度 p50，与周边真实区域对齐才算不糊**（本片 4.0 vs 4.0）。

### wcj 片（微博横屏 1906×1080@30fps）现成参数

- bbox `1472 42 388 130`；水印实范围 x1484..1848 y53..161；mask_soft.png = 15653px
- `lama_apply_maskonly.py ... --pad 10 --keyframe-every 1 --device mps` → 0.21s/帧，4428 帧 913s
- `texture_fix.py --strength 1.25 --tile 48 --seed 7` → 404s
- **`--start 0` 产物无音轨**，结尾必须 `-map 1:a:0 -c:a copy` 从原片补
- 实测：四层残留均 <0.1、细节比 0.95、帧间差比 1.13、verify ① +0.4、③ 2.61

## 去水印（已有正式 skill，先读它）

- **正式 skill**：`~/.workbuddy/skills/video-watermark-removal/`（**v0.2.8**，agent_created）。
  做去水印任务**先加载它**：`SKILL.md`（五步流程 + 坑 42/43/44 决策树）+ `scripts/`
  （`locate_watermark.py` 定位 / `build_mask.py` 提精确 mask / `lama_apply.py` LaMa 逐帧修复 /
  `verify.py` 三件套验收 / `texture_fix.py` 痕迹修复 / `rowfill.py` 渐变背景行插值 /
  `patch_fill.py` 拷贝填充备用）
  + `references/踩坑手册.md`（**44 条坑**，改代码前必读）。
- **★ 修复方案按背景类型选（坑 42/44，别把 LaMa 当万能锤）**：
  - 暗背景（环 <60）→ 稀疏 mask + LaMa + rowinterp；
  - 亮/高动态背景（环 >100）→ **稀疏 mask（含柔边）+ 纯 LaMa + maskonly 合成**
    （~~旧结论「全卡片 mask」已作废~~：全卡片会把水印卡片内 3 万+ 透出的**真实像素**
    一起重绘，实测细节只剩 33~39%，用户直接反馈"那块发糊"。见上方铁律 1/2）；
  - **纯平/渐变背景（环 detail-σ <0.5）→ 跳过 LaMa，直接 `rowfill.py` 行插值**
    （LaMa 在纯平区会发明伪噪点：区内 Laplacian 达周边 2~4 倍 + 逐帧闪烁，指标全绿但有痕）。
- **★ 用户说有痕但指标全绿时的诊断口诀（坑 44）**：测「区内 vs 周边」两项——
  Laplacian 能量比和帧间差。**>1.5× 周边 = LaMa 发明伪纹理 → rowfill；
  <0.85× 周边 = 过平滑 → texture_fix 补纹理**。
- **★ 亮背景/低对比水印片判据要换口径（坑 43）**：残留判据用
  「模板位 − mask 内背景参照」差值（|Δ|<3 即干净），`--white-th` 按背景亮度上调
  （背景 205 → 215~225）；verify ① 基线偏低（+8.5 而非 +70）属正常。
- 项目内归档：`src/skill/`（SKILL.md + lama_remove.py + `watermark_mask_1080p.png`）。
- 微博横屏 1080P 现成结论：安全 bbox `x=1484 y=44 w=392 h=136 --pad 10`，
  复用 mask `assets/mask_weibo_1080p_landscape.png`（392×136，含平台 logo + 两行文字）。
- **关键性能参数**：编码器必须 `-threads 1`（否则 x264 抢 CPU，1.65s/帧 → 4.5s/帧）。
- **★ 开 MPS 加速**：`lama_apply.py` 支持 `--device auto|cpu|mps|cuda`（auto 优先 cuda>mps>cpu）。
  4K ROI 下 CPU 10s/帧（全片 88h 不可行）；MPS **~1.2s/帧**。
  **720P 小 ROI（330×130）MPS 仅 0.2s/帧，1648 帧全片 5.5 分钟一次跑完，不必拆半。**
  LaMa 耗时随 ROI 面积超线性增长，ROI 别开过大。
- **定位铁律**：白字判据用**时域 min 投影（lo>150）**。
  `locate_watermark.py` 自动扫描可能给 low confidence（sharpness 与背景几乎相等），**不可信**；
  高 std 基线片（4K 舞台/运动片）默认 `card-std=30` 会把动态背景误判成水印卡，按片调高（4K 用 42）。
  亮背景低对比片四个角全报 low/WARN → 人工 `min 投影 >232` + 连通块分析。
- **verify.py 已修多轮**（坑 23-36 都在代码里）：
  - 坑 23-26：mask 绝对坐标切片 / ③ 基准 / 帧边界取样 / 偏移符号。
  - 坑 27-30：4K 支持（分辨率/帧率不再写死；探测 ROI 不再硬编码 1080P）。
  - 坑 31：**4K 长片打样验收必给 `--start-hint`**（= lama_apply 的 `--start`），
    否则全片粗搜 1~2 小时；给了 <2 分钟。
  - 坑 32：长任务日志必用 **`python -u`**（块缓冲会让日志长时间 0 字节）。
  - 坑 33：**水印贴边的片**（右上/右下角）ROI=mask+60px 留白会捅出画面 → 崩；已自动夹紧 ROI。
  - 坑 34：**③ 报 7+ 而 ① 干净**时，先测"噪声地板"（原片相邻帧 mask 外差异）+ 水印窗口外
    全画面差异（~1.4 即正常）→ 是 1 帧偏移 + 高运动放大的**假警报**，别返工。
  - 坑 35-36：**指标全绿但肉眼有痕**（LaMa 过度平滑）→ `texture_fix.py`；及其 ROI 坐标系混用。
  - 坑 37：补纹理**自测σ比必须用稀疏 mask PNG**（实心矩形会稀释，把好片判坏）。
- **★ 帧完整性最硬判据**：② 报跳变时，用 `ffmpeg -vf select=eq(n,<k>) -frames:v 1`
  **按绝对帧号**取帧直比（不做 `-ss` 搜索）。窗口外均值 ~0.0x 即证明帧序 100% 完整。
- **★★ 指标有盲区：三项全绿 ≠ 肉眼无痕（坑 35）**。① 只看字形残留、③ 只看区外改动，
  **都看不见修复区内部纹理是否自然**。LaMa 在平坦/暗背景上会把修复区抹成"雾面奶油块"，
  肉眼可见但指标全绿。**交付前务必抽暗场帧目视**。
  - 定量判据：`σ(detail[mask]) / σ(detail[ring])`（detail = 灰度 - GaussianBlur(σ=1.2)）。
    **1.0 = 纹理匹配；< 0.8 就该补**。但**先看周边环 σ**——周边也是 0（纯黑场）就不用补。
  - 解法：`texture_fix.py`——**分块平铺（`--tile 48`）真实周边细节层**进修复区，
    归一化到 `target = σ_ring * 1.3`，边缘羽化叠加。**别用逐像素随机采样**（白噪声，无效）。
    已支持 `--audio-from 原片` 一键混音；全片 1648 帧仅 ~56s。
    实测 σ 比 median **0.60 → 1.11**，视觉痕迹消失。**固定 `--strength 1.3` 就是最优，
    别做自适应**（连改 4 版都更差，median 只有 0.81）。
  - **★★★ 坑 37（本轮最大的坑）**：测 σ 比**必须用与修复一致的那张稀疏 mask PNG**。
    若手写实心矩形当 mask（mask 17743px → 矩形 42900px），mask 内 LaMa 正常重绘的
    空白区被算进去 → σ 被稀释，把 **0.99 测成 0.59** → 误判"补不够"→ 越修越糟。
    **自查：715P 微博片 mask 应是 17743px，若是 4 万多就用了矩形，读数作废。**
  - 补完**必须重跑 verify**（羽化边会让 ③ 区外均值从 0.8 升到 ~1.9，仍达标 <2）。
  - 坑 36：`texture_fix.py` 的 ROI 是**整帧坐标**、mask 是**相对 ROI 局部坐标**，
    两者不是同一个值，**别互相抄**；交叉核对 `验收/report.json` 的 `roi` 字段。
- **★★★ 痕迹有三类，补颗粒只治一类（坑 35/38/39）**：
  - A 类·平滑：σ比 < 0.8 → 补纹理（固定 `--strength 1.3`）。
  - B 类·**低频结构性**（坑 38）：条带/光带在修复区**断裂、发明亮斑**，补颗粒无效。
    → `texture_fix.py --lf-mode rowinterp`：低频用 bbox 左右无水印区**逐行插值**
    替换（条带延续、永不复制物体），高频保留 LaMa。诊断只能**目视最差帧**
    （"mask vs 环低频差"是坏指标，平移对照读数一样大）。
  - C 类·**颗粒时间闪烁**（坑 39）：平铺块位置逐帧重随机 → 静态场景沙沙抖动
    （实测 p90 3.4 vs 地板 0.2）。已修：**每帧用相同种子重建 rng**（块位置全片固定），
    闪烁 p90 3.4→0.98。判据：静态段 mask 区相邻帧差。
  - 备用：`patch_fill.py`（左移拷贝+Poisson）条带场景极佳但复杂场景复制物体；
    seamlessClone 的 center 必须用 **mask 外接矩形中心**（不是 PNG 几何中心）。
- **verify.py 读数自查口诀**：原片基线应报 **+70 上下**（1080P）。若报 +17~+20 或 ③ 报 12+，
  是"尺子坏了"（坑 23-26），**不是成片没修好**，别急着返工。
- **本机 ffmpeg 不认 `-vsync`**，抽帧降采样用 `-vf fps=N`。
- 通用脱离会话启动器模板：`demo/wm_demo1/runbg.py <tag> -- <cmd...>`（日志+PID 落盘）。

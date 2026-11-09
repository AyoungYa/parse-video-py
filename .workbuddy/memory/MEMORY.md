# 项目长期记忆 — parse-video-py

## 环境与规范

- 解释器 `~/miniconda3/envs/parse-video-py/bin/python`（**无 pytest**）。跑测试：
  `uv venv /tmp/pvp-test-venv --python 3.12 && uv pip install --python /tmp/pvp-test-venv/bin/python -e ".[all,dev]"`
  → `PYTHONPATH=src /tmp/pvp-test-venv/bin/python -m pytest tests/ -q`。
- src 布局，跑脚本需 `PYTHONPATH=src`。改代码前必读 `docs/knowledge/99_global_index.md`（AGENTS.md 要求）。
- 格式：black/isort/flake8 统一 `line-length=88`，`.flake8` 加 `extend-ignore=E203`。
- 新路由放 `src/parse_video_py/web.py`；工具函数放 `utils.py`。
- extras：`web`/`cli`/`wm`(torch ~2GB)/`dev`。**`all` 刻意不含 `wm`**。

## ★ cloud_web 数据表四条硬规范（2026-11-05 用户拍板，新建表一律照抄）

① **零外键**：FK 用 `db_constraint=False, db_index=False`，索引在 Meta 显式声明；Django 内置表的 CONSTRAINT
由 `cloud_web/scripts/gen_schema_sql.py` 去掉。级联靠 ORM 应用层模拟，已验证 JOIN/related_name 不受影响。
② **DDL 全中文注释**：业务表 `Field(db_comment=...)` + `Meta.db_table_comment`；内置表由生成脚本字典补。
③ **索引名**：`idx_<table>_<cols>` / `uniq_<table>_<cols>`，全部显式命名（**别用 `unique=True`、
`db_index=True`，它们会生成自动哈希名**）。
④ **对外业务号**：每张业务表一个 `xxx_no varchar(32)` 唯一不可变，格式 `<前缀><日期><12位hex>`，
见 `cloud_web/apps/bizno.py`（迁移按路径引用，**不要改名/移动/删函数**）。用户端只用它查/改，不暴露内部 ID。

建表 SQL 交付 `cloud_web/deploy/init_mysql.sql`，改模型后重跑 `gen_schema_sql.py`（自带 `--check` 自检）。
★★ **给已有表加带 default 的唯一字段**：Django 的 default 对历史行只调用一次 → 旧行同值 → 建唯一约束
必炸；必须在 AddField 与 AddConstraint 之间插 `RunPython` 逐行回填（现成 `apps.bizno.backfill_unique_nos`）。

## 去水印调参台 wm-studio

先调参再做全片。代码 `src/parse_video_py/wm_studio/`，见其 README。
`uv run wm-studio` → `http://127.0.0.1:8010/wm`；单帧 300~500ms(MPS)、建 mask 2.2s、240 帧全片 84s。
四项指标判据同 verify.py；工作区 `wm_studio_workspace/`（gitignore）。
**PyCharm 调试**用 `.idea/runConfigurations/wm_studio.xml`（直跑 server.py main），**别开 `--reload`**（断点挂不上子进程）；
调试前 `wm_studio_launch.py stop` 腾 8010。启动器用 `sys.executable`，要拿装了依赖的 conda 解释器跑。

**五条硬教训**：① `-ss` 必须在 `-i` **之前**才是关键帧跳转；② 成败看**退出码**不信日志「完成」
（`echo` 只是回显），用 `=== EXIT rc=N ===` + 确认成片存在；③ 子进程换 cwd 必须传**绝对路径**；
④ `build_mask.py` 的 `sample_stack` 每帧启一个 ffmpeg（200 帧 40s），调参台内置单次解码批量抽帧（2.8s，IoU 0.957）；
⑤ ★★ **抽帧按内存分流**：`capture_output` 内存 = 输出字节数，整区间解码 152s 1440P 要 29GB、4K 290s 要 77GB
（实际只需 ~1GB）。现按 `BATCH_DECODE_LIMIT=1GiB` 分流，超限走逐帧输入级 seek（内存=单帧），采样 120→60 帧
（mask IoU 1.0000）。实测 30GB→637MB、77GB→782MB。回归测试 `TestBatchDecodeMemoryGuard`。

## ★★ flicker 高值的归因法（2026-11-06 第四轮，推翻过一次错判）

`verify.py` 的 `flicker_p90` 分母已是**同帧 11px 环带帧间差**，全局运动早被抵消
→ **不能凭「这片运动大」就判假警报**。第三轮把 aerial 2.43 归为「航拍运动」是错的：
那轮只试空间域后处理（lf_s00 / 补纹理），降不动 0.13 只能说明**药不对症**。
LaMa 逐帧独立重绘修复区 = 真时间闪烁，只有时域手段能治。

四组归因（`demo/wm_corpus/diagnose_flicker.py`，可复用）：
① 原片同位置比 ratio（aerial src 0.796 vs clean 2.433）；
② **平移对照**：同一 mask 挪到无水印处，多处 ratio 都 <1 则原位离群 = 修复伪影
（还要测实心矩形，排除稀疏字形 mask 挑中高频笔画的几何效应）；
③ **帧序 best_lag 扫描**（±2 帧），高运动片 1 帧错位会把运动算成抖动；
④ **恒等分解 `D_clean = D_src + D_fix`**（`delta=clean−src`，
`D_fix=delta_t−delta_{t−1}`），逐像素精确；扣掉环带 `D_fix`（重编码基线）后
若净抖动 ≥ 场景运动 → 真闪烁。

**修法**（`demo/wm_corpus/aerial_fix_flicker.py`）：`delta` 沿时间轴 **7 帧中值**，
`new = clean − (delta − sm)`，只在 mask 内回贴 + 1px 羽化；场景运动留在 src 原样不动。
中值优于高斯（高斯会软掉空间细节，grad p50 掉到 0.936）；
med7 把 aerial **2.32→1.34，verify 全绿 clean**，σ/lap/res 几乎不变，
卡帧检查静止帧 1/59 与 base 相同。产物 `regression/aerial_flicker/med7_fixed.mp4`。
⏳ 只在 1 片验证，其余 9 片未测；推广前先做产品阶段 + 全量回归
（快速平移片慎用 7 帧窗口，可能拖影）。

## ★★ 验收口径警告：中值口径会掩盖稀疏 mask 残留

`demo/weibo-261021_clean.mp4` 白像素残留 3719（只降 51%），verify.py **时域中值**口径却报 94% 达标。
→ 交付前加**逐帧相对口径**抽查：「模板位均值 − mask 外 11px 环带均值」，|Δ|<3 且 mask 内 `>235` 白像素接近 0。
干净参考 `demo/wm_261021/weibo-261021_clean_v2.mp4`（Δ −0.3，白像素 0）。

## 平台防盗链

微博/新浪 CDN（`f.video.weibocdn.com`、`sinaimg.cn`）只放行「浏览器 UA + 微博站内 Referer」或「完全无 UA」，
带第三方 Referer 一律 403。下载走服务端代理 `GET /video/download?url=&filename=`，别前端直链。

## 当前待办（2026-11-05）

- 🔴 **wuhenjiang.com 被冻结**：whois `clientHold + serverHold`（未实名）→ 备案拒收。豆豆需本人身份证实名（2~24h）。
  清单见 `docs/product/备案与上线清单.md`。
- ⚠️ **主体升级待定**：个人实名做不了微信支付商户号、个人备案不能有经营性功能 → 接支付前升级个体工商户，
  否则重走 10~20 天备案。
- ✅ cloud_web `.env` 已指向远程调试库 `120.25.1.236:19336/test_db`（MySQL 8.4.9，18 张表零漂移）。
  ⚠️ 账号无全局 CREATE DATABASE → 单测走本地 3307 实例（`scripts/run_tests_local.py`，39 例全绿）。
  功能冒烟用 `cloud_web/scripts/smoke_debug_db.py`（真实库 + 事务回滚，不建测试库）。
- ✅ **OSS 已真机打通**（2026-11-06）：bucket `wuhenjiangtest`，地域 **oss-cn-shenzhen**
  （★ 桶地域不可改，endpoint 必须填对，否则 OSS 回 301「must be addressed using the specified endpoint」）。
  浏览器直传 / 签名下载 / 算力机回传三条链路真机全绿。自检：`scripts/check_oss.py`（六项体检）、
  `scripts/e2e_oss.py`（真实端到端 7 项，会写对象跑完自清）。
  ★★ **`oss.py` 签名 URL 协议 bug（已修）**：`oss2` 的 `_normalize_endpoint` 在 endpoint
  不带协议时补 **http://** → 签名 URL 全 http → HTTPS 站点浏览器按混合内容拦掉预览/下载，
  且后端签发仍成功、报错只在浏览器 console，极难定位。修法：`endpoint_host()` 剥协议头，
  `_bucket()` 传 `https://{host}`。回归测试 `tests/test_oss_url_scheme.py`（8 例）。
  ★ **endpoint 必须公网**，写 `-internal` 浏览器直传必失败。
  ★ **签名 PUT 必须带 `Content-Type`**（`presign_put` 把它签进签名，缺了报 SignatureDoesNotMatch）。
  ★ `requests.post(data={...含 tuple})` 不生成 multipart → OSS 回 405，须手工拼（`scripts/_oss_probe.py`）。
- ✅ **M3.1 mask 人工确认页已完成**（2026-11-05）：`BoxSelector.vue` + `MaskConfirmView.vue` +
  `needs_manual` 全链路闭环；真浏览器 7 项 + 调试库 13 项冒烟全绿。详见今日日志。
- ⏳ `gpu_worker/scripts` 6 个脚本是产品代码（已从 .gitignore 放行）；`gpu_worker/` 仍未切 uv。
- ✅ **语料回归两轮已跑**（2026-11-05）：素材库 `demo/wm_corpus/`（17 片），
  报告 `regression/REPORT.md`（含第二轮章节）。首轮找到 3 个真 bug，第二轮已修并复测。
  ★ 首轮「0/5 全绿」是**测试口径错**：水印深色描边+半透明（亮背景上更暗），按
  「delta 最正」挑 bbox → y 贴偏 ±30px。**固定 bbox (135,192,324,55) + 模板 mask
  后 10/10 水印真去掉**（差分图形状=字样；局部中值口径 10/10 干净）。
  三处产品改动：`verify.py` flicker 兜底只在环带静止时抬值、`build_mask.py`
  新增 `--template` 模板通道 + `LOCATE_EXPAND_MAX_RATIO=6.0` 膨胀上限。
  ★ `texture_fix --strength 1.3` 是 1906×1080 大 ROI 标定，**324×55 小 ROI 会过补**
  （σ 7/7 改善但 4/4 片发明亮斑/噪点）→ 不采用，成片用 LaMa 版。
  ⛔ **网找素材没精确 GT，算不出可信全绿率** → 冲 M1 验收线 20~30 条 / ≥85%
  仍需走「合成语料」（干净视频 + EXR alpha 可控叠加，天然得 mask）。
  新 SOP：`run_fixed.py` / `check_residual.py` / `postprocess.py`，
  模板 `assets/shutterstock_596x336.png`。
- ⏳ 第二片残影：半透明水印延伸到 x1241 而 bbox 只到 1149 → residual 5.24，needs_review。

## 产品版管线（gpu_worker/scripts）

比 skill 版多：**自动定位水印**、只贴 mask 内像素（铁律 2）、输出 report.json + 决策树。
**退出码 3 = 定位置信度不足 → 转人工，不重试**。

**自动定位四道判据**：① 恒亮度 min>225（纯白 ✅/半透明 ❌ 最高 217）；② 值高+低波动 `score=p90−2×std`
（半透明 ✅/舞台巨块 ❌，两通道并行取更可信）；③ 位置恒定性 区内std÷周边std <1.05（水印 0.70~0.80 vs 人像 0.91~1.58）；
④ 水平块簇（只看水平相邻不要求 y 重叠——微博两行 y 完全不重叠；单块 >8000px 拒绝）。

**三个致命失效点**：① 片尾黑场毁定位 → 剔除暗帧（均亮<20）；② 触边≠被切 → 只有该方向还能扩才告警，
**扩窗只做一次**且扩后**重算 mask**；③ 精修阈值不能自适应（扩窗后 mask 5000→114699px）。
**闪烁判据**：不能用「÷原片水印区地板」（静止叠加层地板仅 0.064 → 0.3 噪声放大成 4.8 倍假警报），
改用「÷同帧环带帧间差」，环带静止时退回绝对值（上限 1.0）。

## ★★★★ 去水印三条铁律（三轮返修换来）

完整参数见 `demo/wm_wcj/参数记录.md`。

**铁律 1：mask 必须含半透明柔边层，用 min>100 而非 white-th 168。**
`--white-th 168` 只抓够白像素，漏掉柔边（灰度 100~168）→ mask 外漏 571px、成片比邻域亮 +100，肉眼白残影。
```python
mn = np.stack([每帧灰度]).min(axis=0)   # 时域最小投影，1fps 足够
m  = (mn > 100).astype(np.uint8)        # ★ 100 是关键
m  = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5,5),np.uint8))
m  = cv2.dilate(m, np.ones((3,3),np.uint8), 1)
# 去掉 <20px 碎噪连通块
```
自检：内容距 bbox 各边 >3px（否则扩窗）。

**铁律 2：原版 `lama_apply.py` 整块回贴 ROI，稀疏 mask 会发糊。**
第 94 行整块覆盖 → mask 外真实像素也被换（高频只剩 39%）。必须按 mask alpha 合成（副本 `demo/wm_wcj/lama_apply_maskonly.py`）：
```python
a = (mask > 127).astype(np.float32); a = cv2.GaussianBlur(a,(0,0),1.0)
result[y0:y1, x0:x1] = (base*(1-a[...,None]) + inp*a[...,None]).astype(np.uint8)
```

**铁律 3：验收阈值必须 ≥ 建 mask 的阈值**，否则判据自证干净。用 `demo/wm_wcj/accept_all.py` 四层
min>200/>180/>140/>100，判据「成片该位 − 同帧 11px 邻域中值」，|中值|<5 且 |最差帧|<15。

**判「发糊」唯一可靠指标：梯度 p50。** 细节比/σ比对结构塌陷不敏感（全卡片方案细节比 1.01 却烂，p50=0.0）。
测 mask 内梯度 p50 对齐周边（本片 4.0 vs 4.0）。

**wcj 片（微博横屏 1906×1080@30fps）**：bbox `1472 42 388 130`（实范围 x1484..1848 y53..161），mask_soft 15653px；
`lama_apply_maskonly.py --pad 10 --keyframe-every 1 --device mps` 0.21s/帧、4428 帧 913s；
`texture_fix.py --strength 1.25 --tile 48 --seed 7` 404s。**`--start 0` 产物无音轨，结尾补 `-map 1:a:0 -c:a copy`**。
实测四层残留 <0.1、细节比 0.95、帧间差比 1.13、verify ① +0.4 ③ 2.61。

## 去水印 skill（做任务先加载）

`~/.workbuddy/skills/video-watermark-removal/`（v0.2.8）。`SKILL.md`（五步 + 坑 42/43/44 决策树）+
`scripts/`（locate_watermark / build_mask / lama_apply / verify / texture_fix / rowfill / patch_fill）+
`references/踩坑手册.md`（**44 条，改代码前必读**）。项目内归档 `src/skill/`。

**按背景选方案（坑 42/44，别把 LaMa 当万能锤）**：
- 暗背景（环 <60）→ 稀疏 mask + LaMa + rowinterp。
- 亮/高动态（环 >100）→ 稀疏 mask（含柔边）+ 纯 LaMa + maskonly 合成。~~全卡片 mask 已作废~~
  （会把卡片内 3 万+ 透出的真实像素一起重绘，细节只剩 33~39%，用户反馈"发糊"）。
- 纯平/渐变（环 detail-σ <0.5）→ **跳过 LaMa，直接 rowfill.py 行插值**（LaMa 在纯平区发明伪噪点：
  区内 Laplacian 达周边 2~4 倍 + 逐帧闪烁，指标全绿但有痕）。

**说有痕但指标全绿（坑 44）**：测 Laplacian 能量比 + 帧间差。**>1.5× → rowfill；<0.85× → texture_fix**。
**亮背景低对比（坑 43）**：判据改「模板位 − mask 内背景参照」，|Δ|<3 即干净；`--white-th` 按背景上调
（背景 205 → 215~225）；verify ① 基线 +8.5 属正常。

**定位**：白字判据用**时域 min 投影（lo>150）**。`locate_watermark.py` 自动扫描可能 low confidence 不可信；
高 std 基线片 `card-std=30` 会误判，4K 调 42；亮背景低对比四角全 WARN → 人工 min 投影 >232 + 连通块。
微博横屏 1080P：安全 bbox `x=1484 y=44 w=392 h=136 --pad 10`，复用 `assets/mask_weibo_1080p_landscape.png`。

**性能**：编码器必须 `-threads 1`（否则 1.65→4.5s/帧）。`--device auto|cpu|mps|cuda`。4K ROI CPU 10s/帧（不可行）、
MPS ~1.2s/帧；720P 小 ROI（330×130）MPS 0.2s/帧，1648 帧 5.5 分钟跑完。LaMa 耗时随 ROI 面积超线性，ROI 别开大。

**verify.py 已修的坑（23-37 全部在代码里，细节见踩坑手册）**：4K 支持、mask 绝对坐标、③基准、帧边界、
`--start-hint`（4K 长片打样必给，否则粗搜 1~2h）、`python -u`、贴边 ROI 自动夹紧。
**读数自查**：原片基线应 +70 上下（1080P）。报 +17~+20 或 ③ 报 12+ 是"尺子坏了"，别返工；
**③ 报 7+ 而 ① 干净**先测噪声地板（多半是 1 帧偏移 + 高运动假警报）。

**指标有盲区（坑 35）**：三项全绿 ≠ 肉眼无痕（LaMa 在暗场抹成"雾面奶油块"，①③ 看不见）。交付前抽暗场帧目视。
判据 `σ(detail[mask])/σ(detail[ring])`（detail = 灰度 − GaussianBlur σ=1.2），1.0 匹配、<0.8 该补；
周边环 σ=0（纯黑场）不用补。解法 `texture_fix.py` 分块平铺（`--tile 48`）真实周边细节层，
归一化到 `σ_ring*1.3` 边缘羽化；**别用逐像素随机采样**（白噪声无效）。`--audio-from` 混音；1648 帧 ~56s；
σ比 median 0.60→1.11。**固定 `--strength 1.3` 最优，别做自适应**（4 版都更差）。
**坑 37**：手写实心矩形（17743→42900px）会把 0.99 测成 0.59 → 越修越糟。
**自查：715P 微博片 mask 应 17743px，4 万多就是用了矩形。** 补完必须重跑 verify（③ 0.8→~1.9，仍 <2）。
**坑 36**：texture_fix 的 ROI 是整帧坐标、mask 是相对 ROI 局部坐标，别互抄；核对 `report.json` 的 `roi`。

**痕迹三类，补颗粒只治 A（坑 35/38/39）**：
- A 平滑：σ比 <0.8 → 补纹理（`--strength 1.3`）。
- B **低频结构性**（坑 38）：条带/光带断裂、发明亮斑，补颗粒无效 → `texture_fix.py --lf-mode rowinterp`
  （低频用 bbox 左右无水印区逐行插值，高频保留 LaMa）。只能目视最差帧诊断。
- C **颗粒时间闪烁**（坑 39）：平铺块逐帧重随机 → 沙沙抖（p90 3.4 vs 地板 0.2）。已修：每帧同种子重建 rng
  （块位置全片固定），p90 3.4→0.98。判据：静态段 mask 区相邻帧差。
- 备用 `patch_fill.py`（左移拷贝+Poisson）：条带场景极佳但复杂场景复制物体；
  seamlessClone 的 center 用 **mask 外接矩形中心**（非 PNG 几何中心）。

**帧完整性最硬判据**：② 报跳变时用 `ffmpeg -vf select=eq(n,<k>) -frames:v 1` **按绝对帧号**取帧直比。
窗口外均值 ~0.0x 即帧序完整。

**其他**：本机 ffmpeg 不认 `-vsync`，降采样用 `-vf fps=N`。脱离会话启动器模板
`demo/wm_demo1/runbg.py <tag> -- <cmd...>`（日志+PID 落盘）。

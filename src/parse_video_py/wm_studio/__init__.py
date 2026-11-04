# -*- coding: utf-8 -*-
"""wm-studio：video-watermark-removal skill 的 Web 调参台。

把 skill 里的 CLI 脚本当作**库**来用：

  - `lama_apply.py` 的 `inpaint_region` → 单帧秒级预览（不落临时帧）
  - `build_mask.py` 的 mask 提取 → 可视化调阈值
  - `verify.py` 的指标函数 → 页面上直接看是否达标

设计约束（见 README）：
  - 预览走**进程内单例 LaMa**（MPS 约 0.2s/帧）
  - 全片渲染走**子进程**（`lama_apply.py` 独立跑，不阻塞 Web 进程）
"""

from parse_video_py.wm_studio.paths import skill_scripts_dir, workspace_root

__all__ = ["skill_scripts_dir", "workspace_root"]

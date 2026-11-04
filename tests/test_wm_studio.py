# -*- coding: utf-8 -*-
"""wm-studio 单元测试。

只测**不依赖 torch / 真实视频**的纯逻辑：mask 策略、指标计算、bbox/ROI 换算、
任务日志解析。这些正是踩过坑的地方（坐标混用、判据口径、状态误判），
固化成测试防止回归。
"""

import numpy as np
import pytest

pytest.importorskip("cv2")
cv2 = pytest.importorskip("cv2")

from parse_video_py.wm_studio.jobs import (  # noqa: E402
    _count_stage_marks,
    _parse_exit_mark,
    _parse_failed_stage,
    _parse_progress,
)
from parse_video_py.wm_studio.metrics import (  # noqa: E402
    compute_metrics,
    place_mask,
    ring_mask,
)


class TestPlaceMask:
    """mask 从 bbox 局部坐标放到整帧画布——坐标混用是踩过的坑，重点测。"""

    def test_basic_placement(self):
        frame_shape = (100, 200, 3)
        m = np.zeros((20, 30), np.uint8)
        m[5:10, 5:10] = 255
        full = place_mask(m, frame_shape, (50, 20, 30, 20))
        assert full.shape == (100, 200)
        assert full[25:30, 55:60].all()
        assert full.sum() == 25

    def test_out_of_bounds_clipped(self):
        """bbox 超出画面右下时不能崩，只保留可见部分。"""
        frame_shape = (100, 200, 3)
        m = np.full((40, 40), 255, np.uint8)
        full = place_mask(m, frame_shape, (180, 80, 40, 40))
        assert full.shape == (100, 200)
        assert full[80:100, 180:200].all()
        assert full.sum() == 20 * 20

    def test_shape_mismatch_resized(self):
        """mask 尺寸与 bbox 不符时自动 resize，不报错。"""
        m = np.zeros((10, 10), np.uint8)
        m[2:8, 2:8] = 255
        full = place_mask(m, (100, 200, 3), (0, 0, 40, 40))
        assert full.sum() > 0


class TestRingMask:
    def test_ring_is_outside(self):
        m = np.zeros((100, 100), bool)
        m[40:60, 40:60] = True
        ring = ring_mask(m, 5)
        assert not (ring & m).any(), "环带不能与 mask 重叠"
        assert ring.any(), "环带应有像素"

    def test_empty_mask(self):
        m = np.zeros((50, 50), bool)
        assert not ring_mask(m, 3).any()


class TestComputeMetrics:
    """指标四件套。用合成图验证判据方向，不依赖真实视频。"""

    def _setup(self):
        rng = np.random.default_rng(7)
        # 亮背景 + 一块"水印白字"
        src = np.full((200, 300, 3), 205, np.uint8)
        src = np.clip(src.astype(int) + rng.normal(0, 2, src.shape), 0, 255).astype(
            np.uint8
        )
        mask = np.zeros((60, 80), np.uint8)
        mask[20:40, 20:60] = 255
        src[70:90, 70:110] = 245  # bbox 在 (50,50)，故全图坐标 +50
        return src, mask, (50, 50, 80, 60)

    def test_clean_version_scores_better(self):
        src, mask, bbox = self._setup()
        # 完美修复 = 用周边背景填满 mask 区
        clean = src.copy()
        clean[70:90, 70:110] = 205

        rep = compute_metrics(src, clean, mask, bbox)
        by_key = {m.key: m for m in rep.items}
        assert by_key["glyph_drop"].value > 90
        assert by_key["glyph_drop"].ok is True
        assert by_key["region_max"].ok is True

    def test_unfixed_scores_bad(self):
        """完全没修（成片=原片）时，残留降幅应接近 0% 并判失败。"""
        src, mask, bbox = self._setup()
        rep = compute_metrics(src, src.copy(), mask, bbox)
        by_key = {m.key: m for m in rep.items}
        assert by_key["glyph_drop"].value is not None
        assert by_key["glyph_drop"].value < 10
        assert by_key["glyph_drop"].ok is False

    def test_invented_bright_block_detected(self):
        """成片比原片更亮 = LaMa 发明亮块，region_max 必须报失败。"""
        src, mask, bbox = self._setup()
        clean = src.copy()
        clean[70:90, 70:110] = 255  # 比原片 245 更亮
        rep = compute_metrics(src, clean, mask, bbox)
        by_key = {m.key: m for m in rep.items}
        assert by_key["region_max"].ok is False

    def test_relative_background_not_absolute(self):
        """★ 坑 43：亮背景片不能把背景算成残留。

        整片背景 205 时，如果用「>150 像素数」当判据会判成满屏残留。
        这里验证：完美修复后 glyph_drop 要接近 100%（而不是被背景拉低）。
        """
        src, mask, bbox = self._setup()
        clean = src.copy()
        clean[70:90, 70:110] = 205
        rep = compute_metrics(src, clean, mask, bbox)
        g = {m.key: m for m in rep.items}["glyph_drop"]
        assert g.value > 95, f"相对口径下完美修复应接近 100%，实测 {g.value}"

    def test_empty_mask_returns_na(self):
        """mask 全空时不应崩，指标标为不适用。"""
        src = np.full((100, 100, 3), 128, np.uint8)
        mask = np.zeros((40, 40), np.uint8)
        rep = compute_metrics(src, src.copy(), mask, (10, 10, 40, 40))
        g = {m.key: m for m in rep.items}["glyph_drop"]
        assert g.ok is None
        assert g.value is None


class TestClearMaskStrategy:
    """稀疏 → 全卡片。坑 42 的正解，必须行为正确。"""

    def test_card_covers_content_bbox(self):
        from parse_video_py.wm_studio.mask_ops import card_mask_from_sparse

        m = np.zeros((100, 200), np.uint8)
        m[10:20, 30:50] = 255  # 左上块
        m[70:80, 150:180] = 255  # 右下块
        card = card_mask_from_sparse(m, dilate=0)
        # 全卡片应把两块之间的空白也覆盖
        assert card[45, 100] == 255
        assert card[10:80, 30:180].min() == 255
        assert card.sum() > m.sum()

    def test_removes_tiny_specks(self):
        from parse_video_py.wm_studio.mask_ops import card_mask_from_sparse

        m = np.zeros((100, 100), np.uint8)
        m[40:60, 40:60] = 255
        m[5, 5] = 255  # 单像素噪点
        card = card_mask_from_sparse(m, dilate=0)
        assert card[5, 5] == 0, "碎噪不应影响卡片范围"

    def test_empty_input_safe(self):
        from parse_video_py.wm_studio.mask_ops import card_mask_from_sparse

        m = np.zeros((50, 50), np.uint8)
        out = card_mask_from_sparse(m)
        assert out.shape == m.shape


class TestJobLogParsing:
    """任务状态判定。★ 踩过的坑：看 '=== ALL DONE ===' 会把崩溃报成成功。"""

    def test_exit_mark_success(self):
        text = "=== stage: lama_apply ===\noutput...\n=== EXIT rc=0 ===\n"
        assert _parse_exit_mark(text) == 0

    def test_exit_mark_failure(self):
        text = "=== STAGE_FAILED lama_apply rc=1 ===\n=== EXIT rc=1 ===\n"
        assert _parse_exit_mark(text) == 1
        assert _parse_failed_stage(text) == "lama_apply"

    def test_exit_mark_absent(self):
        """进程被 kill 时没有 EXIT 标记，要返回 None（交给进程状态兜底）。"""
        assert _parse_exit_mark("正在渲染...\n50 frames 17s\n") is None

    def test_echo_all_done_alone_is_not_success(self):
        """只有 'ALL DONE' 没有 EXIT 标记 → 不能当成功。"""
        text = "=== stage: lama_apply ===\nValueError: boom\n=== ALL DONE ===\n"
        assert _parse_exit_mark(text) is None

    def test_progress_parsing(self):
        text = "    50 frames  17s  0.33s/frame\n   100 frames  31s  0.31s/frame\n"
        p = _parse_progress(text)
        assert p == {"frames": 100, "seconds": 31, "sec_per_frame": 0.31}

    def test_progress_absent(self):
        assert _parse_progress("loading model...") is None

    def test_stage_count(self):
        text = "=== stage: lama_apply ===\n...\n=== stage: texture_fix ===\n"
        assert _count_stage_marks(text) == 1


class TestFrameToJpeg:
    def test_encode(self):
        from parse_video_py.wm_studio.video import frame_to_jpeg_bytes

        frame = np.zeros((60, 80, 3), np.uint8)
        frame[:] = (10, 200, 30)
        data = frame_to_jpeg_bytes(frame, 80)
        assert data[:2] == b"\xff\xd8"  # JPEG SOI
        assert len(data) > 100


class TestCropRoi:
    def test_clamped(self):
        from parse_video_py.wm_studio.video import crop_roi

        f = np.zeros((100, 100, 3), np.uint8)
        out = crop_roi(f, 90, 90, 50, 50)  # 越界
        assert out.shape[:2] == (10, 10)

    def test_normal(self):
        from parse_video_py.wm_studio.video import crop_roi

        f = np.zeros((100, 100, 3), np.uint8)
        assert crop_roi(f, 10, 20, 30, 40).shape[:2] == (40, 30)


class TestBatchDecodeMemoryGuard:
    """★ 回归防护：批量抽帧不能因为片长了就吃爆内存。

    老实现一律「整区间解码」，`capture_output=True` 会把 `[lo,hi]` 全部帧的
    rawvideo 读进内存。150s 的 1440P 片要 29GB、4K 289s 片要 77GB。
    现在按 `max_bytes` 分流：超限走逐帧 seek，内存恒定 = 单帧。
    """

    def test_limit_constants(self):
        from parse_video_py.wm_studio import video as V

        # 默认上限必须是「单帧量级 × 几十」，不是「整片量级」
        assert V.BATCH_DECODE_LIMIT == 1 << 30

    def test_span_exceeds_limit_picks_per_frame_path(self, monkeypatch):
        from parse_video_py.wm_studio import video as V

        calls = {"batch": 0, "single": 0}

        monkeypatch.setattr(
            V,
            "probe_video",
            lambda p: V.VideoInfo(
                path=p,
                width=3840,
                height=2160,
                fps=25.0,
                frames=7200,
                duration=288.0,
                has_audio=False,
            ),
        )

        def fake_single(path, t, info):
            calls["single"] += 1
            return np.zeros((info.height, info.width, 3), np.uint8)

        def fake_batch(*a, **kw):  # 整区间路径不该被走到
            calls["batch"] += 1
            return None

        import subprocess as sp

        monkeypatch.setattr(V, "_grab_at_ts", fake_single)
        monkeypatch.setattr(sp, "run", fake_batch)

        # 4K 单帧 24.9MB，给 100MB 上限 → 跨度 4 帧就超了，必走逐帧
        got = V.grab_frames_many("x.mp4", [100, 200, 300], max_bytes=100 << 20)
        assert calls["batch"] == 0
        assert calls["single"] == 3
        assert len(got) == 3

    def test_small_span_keeps_batch_path(self, monkeypatch):
        """短区间仍走整区间解码（保持原来的速度，不退化成逐帧）。"""
        from parse_video_py.wm_studio import video as V

        info = V.VideoInfo(
            path="x.mp4",
            width=160,
            height=120,
            fps=10.0,
            frames=100,
            duration=10.0,
            has_audio=False,
        )
        monkeypatch.setattr(V, "probe_video", lambda p: info)

        fsz = info.width * info.height * 3
        # 造 50 帧的假 rawvideo（区间 10..59）
        blob = b"\x01" * (50 * fsz)

        class FakeProc:
            stdout = blob

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeProc())
        got = V.grab_frames_many("x.mp4", [10, 20, 59], max_bytes=10 << 20)
        assert [n for n, _ in got] == [10, 20, 59]
        assert all(f.shape == (info.height, info.width, 3) for _, f in got)

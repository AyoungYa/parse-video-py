"""管线纯函数测试（不依赖 GPU、不依赖云端）。

跑法（在 gpu_worker/ 下）：
    python manage.py test tests -v 2
"""

from django.test import SimpleTestCase

from core.pipeline import Pipeline, _normalize_metrics, _parse_progress


class ProgressParseTest(SimpleTestCase):
    def test_frame_counter(self):
        self.assertAlmostEqual(_parse_progress("frame 1843/4428 ..."), 0.4162, places=3)

    def test_percent(self):
        self.assertAlmostEqual(_parse_progress("progress: 42.5%"), 0.425, places=3)

    def test_clamped(self):
        self.assertEqual(_parse_progress("9999/100"), 1.0)

    def test_no_progress(self):
        self.assertIsNone(_parse_progress("Loading model weights..."))

    def test_stage_marker_is_not_frame_progress(self):
        # ★ 回归：lama_apply 打 "[1/3] 加载模型"，被当成 1/3 帧会让进度条乱跳
        self.assertIsNone(_parse_progress("[1/3] 加载 LaMa 模型(首次运行自动下载)..."))
        self.assertIsNone(_parse_progress("[2/3] 流式渲染(解码 -> LaMa -> 编码)..."))

    def test_frame_progress_survives_bracket_stripping(self):
        # 行内有阶段标记但也有真帧号时，取帧号
        self.assertAlmostEqual(_parse_progress("[2/3] 240/240 frames  3.0s"), 1.0, places=3)


class BboxBackfillTest(SimpleTestCase):
    """build_mask 自动定位后回填 bbox（后续阶段都靠它）。"""

    def test_parse_bbox_result_line(self):
        out = "[1/3] 自动定位...\nBBOX_RESULT: 1485 46 388 126\n[3/3] mask 写出"
        self.assertEqual(Pipeline._bbox_from_output(out), "1485 46 388 126")

    def test_missing_bbox_line_returns_none(self):
        self.assertIsNone(Pipeline._bbox_from_output("[1/3] 什么都没输出"))

    def test_only_first_bbox_line_used(self):
        out = "BBOX_RESULT: 1 2 3 4\nBBOX_RESULT: 5 6 7 8"
        self.assertEqual(Pipeline._bbox_from_output(out), "1 2 3 4")


class RepairScoreTest(SimpleTestCase):
    """两轮成片择优的评分（修复轮可能反而变差，要能挑出更好的那份）。"""

    def test_green_beats_non_green(self):
        green = _normalize_metrics({"all_green": True, "residual_abs": 0.9})
        bad = _normalize_metrics({"residual_abs": 0.9, "sigma_ratio": {"median": 0.5}})
        self.assertGreater(Pipeline._score(green), Pipeline._score(bad))

    def test_closer_sigma_is_better(self):
        a = _normalize_metrics({"residual_abs": 0.9, "sigma_ratio": {"median": 1.05}})
        b = _normalize_metrics({"residual_abs": 0.9, "sigma_ratio": {"median": 0.55}})
        self.assertGreater(Pipeline._score(a), Pipeline._score(b))

    def test_missing_metrics_scores_lowest(self):
        self.assertLess(Pipeline._score(None), Pipeline._score(_normalize_metrics({})))


class MetricsNormalizeTest(SimpleTestCase):
    def test_all_green_when_three_metrics_pass(self):
        m = _normalize_metrics(
            {"residual_max": 1.8, "gradient_p50_ratio": 1.02, "flicker_p90": 0.4}
        )
        self.assertTrue(m["all_green"])
        self.assertAlmostEqual(m["residual_max"], 1.8)

    def test_not_green_when_residual_high(self):
        m = _normalize_metrics(
            {"residual_max": 9.0, "gradient_p50_ratio": 1.02, "flicker_p90": 0.4}
        )
        self.assertFalse(m["all_green"])

    def test_not_green_when_texture_collapsed(self):
        # 细节比 0.5 = 修复区被抹平，属于必须返工的情况
        m = _normalize_metrics(
            {"residual_max": 1.0, "gradient_p50_ratio": 0.5, "flicker_p90": 0.4}
        )
        self.assertFalse(m["all_green"])

    def test_never_claims_green_when_metrics_missing(self):
        # ★ 关键：没测出指标时绝不能报"全部通过"
        m = _normalize_metrics({})
        self.assertFalse(m["all_green"])
        self.assertIsNone(m["residual_max"])

    def test_aliases_and_summary_nesting(self):
        m = _normalize_metrics({"summary": {"detail_ratio": 0.95, "residual": 2.0}})
        self.assertAlmostEqual(m["gradient_p50_ratio"], 0.95)
        self.assertAlmostEqual(m["residual_max"], 2.0)
        self.assertFalse(m["all_green"])  # flicker 缺失 → 不能判全绿


class BboxStrTest(SimpleTestCase):
    def test_list_bbox(self):
        self.assertEqual(Pipeline._bbox_str({"bbox": [1472, 42, 388, 130]}), "1472 42 388 130")

    def test_missing_bbox_returns_empty_for_auto_locate(self):
        self.assertEqual(Pipeline._bbox_str({}), "")

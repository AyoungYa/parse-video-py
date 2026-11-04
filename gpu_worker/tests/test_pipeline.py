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

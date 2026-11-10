"""管线纯函数测试（不依赖 GPU、不依赖云端）。

跑法（在 gpu_worker/ 下）：
    python manage.py test tests -v 2
"""

from django.test import SimpleTestCase
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from core.pipeline import Pipeline, StageResult, _normalize_metrics, _parse_progress


class ConfirmationBoundaryTest(SimpleTestCase):
    def test_prepare_stops_after_keyframe_and_location(self):
        with TemporaryDirectory() as tmp:
            pipeline = Pipeline(scripts_dir=Path(tmp), workspace=Path(tmp))
            payload = {"task_id": "example", "video_url": "https://example/video.mp4",
                       "meta": {"duration_ms": 10000}}

            def fake_ffmpeg(_cmd, **_kwargs):
                Path(_cmd[-1]).write_bytes(b"jpeg")
                return SimpleNamespace(returncode=0, stderr="")

            uploaded = []
            def on_frame(index, frame):
                uploaded.append((index, frame.read_bytes()))

            with patch.object(pipeline, "_download"), patch("core.pipeline.subprocess.run", side_effect=fake_ffmpeg), \
                 patch.object(pipeline, "_run_stage", return_value=StageResult(
                     "build_mask", 0, 1.0, "BBOX_RESULT: 10 20 100 50")) as run_stage:
                frames, bbox, _ = pipeline.prepare(payload, lambda _stage, _progress: None, on_frame)

            self.assertEqual(len(frames), 3)
            self.assertTrue(all(frame.exists() for frame in frames))
            self.assertEqual(bbox, [10, 20, 100, 50])
            self.assertEqual(uploaded, [(0, b"jpeg"), (1, b"jpeg"), (2, b"jpeg")])
            self.assertEqual(run_stage.call_count, 1)
            self.assertEqual(run_stage.call_args.args[0]["name"], "build_mask")

    def test_prepare_reuses_keyframes_from_previous_worker(self):
        with TemporaryDirectory() as tmp:
            pipeline = Pipeline(scripts_dir=Path(tmp), workspace=Path(tmp))
            payload = {
                "task_id": "resume-frames", "video_url": "video-url",
                "meta": {"duration_ms": 10000},
                "keyframe_resume_urls": {str(i): f"frame-{i}" for i in range(3)},
            }
            def download(_url, dest):
                dest.write_bytes(b"saved frame")
            with patch.object(pipeline, "_download", side_effect=download), \
                 patch("core.pipeline.subprocess.run") as ffmpeg, \
                 patch.object(pipeline, "_run_stage", return_value=StageResult(
                     "build_mask", 0, 1.0, "BBOX_RESULT: 10 20 100 50")):
                frames, bbox, _ = pipeline.prepare(payload, lambda *_: None)
            ffmpeg.assert_not_called()
            self.assertEqual(bbox, [10, 20, 100, 50])
            self.assertTrue(all(frame.read_bytes() == b"saved frame" for frame in frames))

    def test_other_worker_resumes_after_report_checkpoint(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            artifacts = {}
            metrics = {"residual_max": 1.0, "gradient_p50_ratio": 1.0, "flicker_p90": 0.1}
            payload = {
                "task_id": "resume-render", "video_url": "video-url",
                "params": {"bbox_confirmed": True, "bbox": [10, 20, 100, 50]},
            }
            first = Pipeline(scripts_dir=workspace, workspace=workspace / "first")
            def first_download(_url, dest):
                dest.write_bytes(b"source video")
            def run_stage(stage, ctx, _progress):
                target = {"build_mask": "mask", "lama_apply": "out", "verify": "report"}[stage["name"]]
                Path(ctx[target]).write_bytes(
                    json.dumps(metrics).encode() if target == "report" else stage["name"].encode()
                )
                return StageResult(stage["name"], 0, 1.0, "")
            with patch.object(first, "_download", side_effect=first_download), \
                 patch.object(first, "_run_stage", side_effect=run_stage):
                first.run(payload, lambda *_: None,
                          lambda stage, path, _metrics, _result: artifacts.update({stage: path.read_bytes()}))
            self.assertEqual(set(artifacts), {"build_mask", "lama_apply", "verify"})

            second = Pipeline(scripts_dir=workspace, workspace=workspace / "second")
            resumed = {**payload, "checkpoint_get_urls": {stage: stage for stage in artifacts}}
            def second_download(url, dest):
                dest.write_bytes(artifacts.get(url, b"source video"))
            with patch.object(second, "_download", side_effect=second_download), \
                 patch.object(second, "_run_stage") as run_again:
                outcome = second.run(resumed, lambda *_: None)
            run_again.assert_not_called()
            self.assertEqual(outcome.out_path.read_bytes(), b"lama_apply")
            self.assertTrue(outcome.metrics["all_green"])

    def test_resume_from_mask_runs_only_remaining_stages(self):
        with TemporaryDirectory() as tmp:
            pipeline = Pipeline(scripts_dir=Path(tmp), workspace=Path(tmp))
            payload = {
                "task_id": "mask-only", "video_url": "video-url",
                "params": {"bbox_confirmed": True, "bbox": [10, 20, 100, 50]},
                "checkpoint_get_urls": {"build_mask": "saved-mask"},
            }
            metrics = {"residual_max": 1.0, "gradient_p50_ratio": 1.0, "flicker_p90": 0.1}
            stages = []
            def download(url, dest):
                dest.write_bytes(b"mask" if url == "saved-mask" else b"source")
            def run_stage(stage, ctx, _progress):
                stages.append(stage["name"])
                target = "out" if stage["name"] == "lama_apply" else "report"
                Path(ctx[target]).write_bytes(json.dumps(metrics).encode() if target == "report" else b"result")
                return StageResult(stage["name"], 0, 1.0, "")
            with patch.object(pipeline, "_download", side_effect=download), \
                 patch.object(pipeline, "_run_stage", side_effect=run_stage):
                pipeline.run(payload, lambda *_: None)
            self.assertEqual(stages, ["lama_apply", "verify"])

    def test_resume_after_final_verification_skips_all_scripts(self):
        with TemporaryDirectory() as tmp:
            pipeline = Pipeline(scripts_dir=Path(tmp), workspace=Path(tmp))
            first_metrics = {"residual_max": 4.0, "gradient_p50_ratio": 0.6,
                             "flicker_p90": 0.1, "diagnosis": "needs_texture"}
            final_metrics = {"residual_max": 1.0, "gradient_p50_ratio": 1.0,
                             "flicker_p90": 0.1, "diagnosis": "ok"}
            payload = {
                "task_id": "final-verify", "video_url": "video-url",
                "params": {"bbox_confirmed": True, "bbox": [10, 20, 100, 50]},
                "checkpoint_get_urls": {stage: stage for stage in
                    ("build_mask", "lama_apply", "verify", "texture_fix", "verify_final")},
            }
            content = {
                "build_mask": b"mask", "lama_apply": b"first-output",
                "verify": json.dumps(first_metrics).encode(),
                "texture_fix": b"better-output",
                "verify_final": json.dumps(final_metrics).encode(),
            }
            def download(url, dest):
                dest.write_bytes(content.get(url, b"source"))
            with patch.object(pipeline, "_download", side_effect=download), \
                 patch.object(pipeline, "_run_stage") as run_stage:
                outcome = pipeline.run(payload, lambda *_: None)
            run_stage.assert_not_called()
            self.assertEqual(outcome.out_path.read_bytes(), b"better-output")
            self.assertTrue(outcome.metrics["all_green"])


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

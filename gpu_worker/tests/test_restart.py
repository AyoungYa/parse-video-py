"""进程重启时的本地归还行为，不依赖云端数据库。"""

from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from core.pipeline import Aborted, Pipeline
from core.runner import Runner


class RestartTest(SimpleTestCase):
    def test_prepare_reports_each_uploaded_frame(self):
        client = MagicMock()
        client.claim.return_value = {
            "task_id": "task-frames", "phase": "prepare", "meta": {"duration_ms": 1000},
            "keyframe_upload_urls": ["put-0", "put-1", "put-2"],
        }
        pipeline = MagicMock()
        pipeline.abort_event = Event()
        def prepare(_payload, _progress, on_frame):
            for index in range(3):
                on_frame(index, f"frame-{index}")
            return [], [10, 20, 30, 40], []
        pipeline.prepare.side_effect = prepare
        runner = Runner(client, pipeline)
        with patch.object(runner, "capabilities", return_value={}), \
             patch("core.runner.LocalTask.objects.update_or_create", return_value=(SimpleNamespace(pk=1, duration_seconds=1.0), True)), \
             patch("core.runner.LocalTask.objects.filter"):
            runner.run_once()
        self.assertEqual(client.frame.call_count, 3)
        client.frame.assert_any_call("task-frames", 0)
        client.prepared.assert_called_once_with("task-frames", [10, 20, 30, 40], [])

    def test_shutdown_after_claim_returns_task_before_processing(self):
        client = MagicMock()
        client.claim.return_value = {"task_id": "task-1"}
        pipeline = MagicMock()
        runner = Runner(client, pipeline)
        runner._running = False
        with patch.object(runner, "capabilities", return_value={}):
            runner.run_once()
        client.release.assert_called_once_with("task-1")
        pipeline.prepare.assert_not_called()

    def test_abort_releases_active_task(self):
        client = MagicMock()
        client.claim.return_value = {
            "task_id": "task-1", "phase": "prepare", "meta": {"duration_ms": 1000},
            "keyframe_upload_urls": [],
        }
        pipeline = MagicMock()
        pipeline.abort_event = Event()
        pipeline.prepare.side_effect = Aborted("restart")
        runner = Runner(client, pipeline)
        with patch.object(runner, "capabilities", return_value={}), \
             patch("core.runner.LocalTask.objects.update_or_create", return_value=(SimpleNamespace(pk=1, duration_seconds=1.0), True)), \
             patch("core.runner.LocalTask.objects.filter"):
            runner.run_once()
        client.release.assert_called_once_with("task-1")
        pipeline.cleanup.assert_called_once_with("task-1")

    def test_abort_stops_active_child_process(self):
        pipeline = Pipeline()
        proc = MagicMock()
        proc.poll.return_value = None
        pipeline._active_proc = proc
        with patch("core.pipeline._kill") as kill:
            pipeline.abort()
        self.assertTrue(pipeline.abort_event.is_set())
        kill.assert_called_once_with(proc)

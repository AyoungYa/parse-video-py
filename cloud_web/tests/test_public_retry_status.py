from django.test import SimpleTestCase

from apps.tasks.models import Task, TaskStatus
from apps.tasks.serializers import TaskDetailSerializer, TaskListSerializer


class PublicRetryStatusTest(SimpleTestCase):
    def test_retry_keeps_previous_stage_and_hides_internal_error(self):
        for internal_status in (TaskStatus.PENDING, TaskStatus.LEASED):
            task = Task(
                status=internal_status,
                progress=0.42,
                checkpoints={"_retry_display_status": TaskStatus.REMOVING_WATERMARK},
                error_code="STAGE_FAILED",
                error_message="Traceback: private filesystem path",
            )
            detail = TaskDetailSerializer(task).data
            listing = TaskListSerializer(task).data
            self.assertEqual(detail["status"], TaskStatus.REMOVING_WATERMARK)
            self.assertEqual(listing["status"], TaskStatus.REMOVING_WATERMARK)
            self.assertEqual(detail["progress"], 0.42)
            self.assertEqual(detail["error_code"], "")
            self.assertEqual(detail["error_message"], "")

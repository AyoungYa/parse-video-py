"""按合规承诺清理 OSS：已结束任务的对象保留 OSS_RETENTION_DAYS 天（默认 3 天）后删除。

建议每小时跑一次。只删已结束的任务对象，在途任务绝不碰。
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tasks.models import Task
from apps.tasks.services import oss

TERMINAL = ("succeeded", "failed", "cancelled", "expired")


class Command(BaseCommand):
    help = "删除超过保留期的任务对象（原片/成片/报告）"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=settings.OSS_RETENTION_DAYS)
        qs = Task.objects.filter(status__in=TERMINAL, finished_at__lt=cutoff).exclude(
            original_key="", result_key=""
        )
        deleted = 0
        for task in qs.iterator(chunk_size=100):
            if options["dry_run"]:
                self.stdout.write(f"[dry-run] 将删除 {task.id}")
                continue
            oss.delete_task_objects(task)
            Task.objects.filter(pk=task.pk).update(
                original_key="", result_key="", mask_key="", report_key=""
            )
            deleted += 1
        self.stdout.write(self.style.SUCCESS(f"清理 {deleted} 个任务的对象"))

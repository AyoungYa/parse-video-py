"""把长时间停在「待上传」的任务标记为过期（用户传了一半就走人）。

建议每 5 分钟跑一次。
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.tasks.models import Task, TaskStatus


class Command(BaseCommand):
    help = "过期未完成上传的任务"

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(minutes=settings.STALE_UPLOAD_MINUTES)
        qs = Task.objects.filter(status=TaskStatus.AWAITING_UPLOAD, created_at__lt=cutoff)
        count = qs.update(status=TaskStatus.EXPIRED, finished_at=timezone.now())
        self.stdout.write(self.style.SUCCESS(f"过期 {count} 个未上传任务"))

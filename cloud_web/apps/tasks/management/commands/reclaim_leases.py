"""回收超时租约。建议 cron 每 30 秒跑一次：

    * * * * * cd /srv/cloud_web && python manage.py reclaim_leases >> /var/log/reaper.log 2>&1
"""

from django.core.management.base import BaseCommand

from apps.tasks.services.lease import reclaim_expired_leases


class Command(BaseCommand):
    help = "把租约超时的在途任务打回队列（超过重试上限则判失败并退款）"

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=100)

    def handle(self, *args, **options):
        stats = reclaim_expired_leases(batch=options["batch"])
        self.stdout.write(
            self.style.SUCCESS(f"回收 {stats['reclaimed']} 个，判失败 {stats['failed']} 个")
        )

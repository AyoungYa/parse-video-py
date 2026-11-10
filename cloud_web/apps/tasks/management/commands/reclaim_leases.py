"""回收超时租约。建议 cron 每 30 秒跑一次：

    * * * * * cd /srv/cloud_web && python manage.py reclaim_leases >> /var/log/reaper.log 2>&1
"""

from django.core.management.base import BaseCommand

from apps.tasks.services.lease import mark_offline_workers, reclaim_expired_leases


class Command(BaseCommand):
    help = "把租约超时的在途任务打回队列（超过重试上限则判失败并退款）"

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=100)

    def handle(self, *args, **options):
        offline = mark_offline_workers(batch=options["batch"])
        stats = reclaim_expired_leases(batch=options["batch"])
        self.stdout.write(
            self.style.SUCCESS(f"节点离线 {offline['offline']} 个，接力任务 {offline['released']} 个；回收 {stats['reclaimed']} 个，判失败 {stats['failed']} 个")
        )

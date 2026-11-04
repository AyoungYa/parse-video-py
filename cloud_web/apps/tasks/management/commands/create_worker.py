"""注册一台算力节点，输出明文 token（只显示这一次，请立刻写进 gpu_worker/.env）。

用法：
    python manage.py create_worker --name pc-01 --gpu "RTX 4090" --vram 24
"""

from django.core.management.base import BaseCommand

from apps.tasks.models import WorkerNode
from apps.tasks.services.lease import make_worker


class Command(BaseCommand):
    help = "创建算力节点并打印一次性 token"

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--gpu", default="")
        parser.add_argument("--vram", type=int, default=0)
        parser.add_argument("--concurrency", type=int, default=1)

    def handle(self, *args, **options):
        if WorkerNode.objects.filter(name=options["name"]).exists():
            self.stderr.write(self.style.ERROR(f"节点 {options['name']} 已存在"))
            return

        worker, raw = make_worker(options["name"])
        worker.capabilities = {"gpu": options["gpu"], "vram_gb": options["vram"]}
        worker.max_concurrency = options["concurrency"]
        worker.save(update_fields=["capabilities", "max_concurrency"])

        self.stdout.write(self.style.SUCCESS("节点已创建，把下面这行写进 gpu_worker/.env："))
        self.stdout.write("")
        self.stdout.write(f"WORKER_ID={worker.name}")
        self.stdout.write(f"WORKER_TOKEN={raw}")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("token 只显示这一次，丢失只能重建节点。"))

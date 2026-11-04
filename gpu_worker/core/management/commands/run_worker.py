"""常驻主循环。生产环境用进程守护拉起，不要手动跑。

    python manage.py run_worker

Windows：注册为「任务计划程序」→ 触发器「登录时」→ 操作执行
    <venv>\\Scripts\\python.exe manage.py run_worker
并勾选「无论用户是否登录都运行」以外的默认项即可（需要能访问显卡驱动）。

Linux/macOS：systemd / launchd，Restart=always。
"""

import logging
import signal

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import LocalTask
from core.pipeline import Pipeline
from core.runner import Runner, build_client

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "启动算力节点主循环（长轮询领任务并处理）"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="只处理一轮（调试用）")
        parser.add_argument("--device", default=None, help="覆盖 TORCH_DEVICE")
        parser.add_argument("--dry-run", action="store_true", help="只领任务并回报失败，不真跑")

    def handle(self, *args, **options):
        # 上一轮如果被强杀，本地镜像会停在 running，启动时先标成 aborted
        stale = LocalTask.objects.filter(status=LocalTask.Status.RUNNING)
        if stale.exists():
            count = stale.update(
                status=LocalTask.Status.ABORTED,
                stage="aborted",
                error_message="进程重启，上一轮未正常结束",
                finished_at=timezone.now(),
            )
            self.stdout.write(self.style.WARNING(f"清理了 {count} 条上次残留的 running 记录"))

        client = build_client()
        pipeline = Pipeline(device=options.get("device"))
        runner = Runner(client, pipeline)

        def _graceful(signum, _frame):
            self.stdout.write(self.style.WARNING(f"收到信号 {signum}，准备退出…"))
            runner._running = False
            pipeline.abort()

        signal.signal(signal.SIGINT, _graceful)
        signal.signal(signal.SIGTERM, _graceful)

        if options["dry_run"]:
            payload = client.claim(runner.capabilities())
            if not payload:
                self.stdout.write("当前没有待处理任务")
                return
            self.stdout.write(f"领到任务 {payload['task_id']}，dry-run 直接上报失败")
            client.fail(payload["task_id"], "DRY_RUN", "dry-run 模式不实际处理", retryable=True)
            return

        if options["once"]:
            runner.run_once()
            return

        self.stdout.write(self.style.SUCCESS("主循环启动，Ctrl+C 退出"))
        runner.run_forever()

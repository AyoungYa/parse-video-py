from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tasks", "0007_task_attempt_stage_comment")]

    operations = [
        migrations.AlterField(
            model_name="workernode",
            name="last_heartbeat_at",
            field=models.DateTimeField(
                null=True, blank=True,
                db_comment="最近进程级 alive 上报时间；超过 60 秒判离线",
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="status",
            field=models.CharField(
                max_length=24,
                default="awaiting_upload",
                db_comment="任务状态；worker_offline 为内部接力状态，用户端保持原阶段",
                choices=[
                    ("awaiting_upload", "待上传"), ("pending", "待处理"),
                    ("worker_offline", "处理节点离线"), ("leased", "已领取"),
                    ("extracting_frames", "提取关键帧"), ("locating_watermark", "定位水印"),
                    ("awaiting_confirmation", "待确认水印"),
                    ("removing_watermark", "去水印中"), ("running", "处理中"),
                    ("verifying", "验收中"), ("succeeded", "已完成"),
                    ("failed", "失败"), ("cancelled", "已取消"),
                    ("expired", "已过期"), ("needs_manual", "待框选"),
                ],
            ),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tasks", "0003_alter_task_table_comment_and_more")]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="status",
            field=models.CharField(
                max_length=24,
                default="awaiting_upload",
                db_comment="任务状态；定位后等待用户确认再去水印",
                choices=[
                    ("awaiting_upload", "待上传"), ("pending", "待处理"),
                    ("leased", "已领取"), ("extracting_frames", "提取关键帧"),
                    ("locating_watermark", "定位水印"),
                    ("awaiting_confirmation", "待确认水印"),
                    ("removing_watermark", "去水印中"), ("running", "处理中"),
                    ("verifying", "验收中"), ("succeeded", "已完成"),
                    ("failed", "失败"), ("cancelled", "已取消"),
                    ("expired", "已过期"), ("needs_manual", "待框选"),
                ],
            ),
        ),
    ]

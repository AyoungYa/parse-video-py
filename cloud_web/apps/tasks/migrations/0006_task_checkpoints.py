from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tasks", "0005_alter_taskevent_status_lengths")]

    operations = [
        migrations.AddField(
            model_name="task",
            name="checkpoints",
            field=models.JSONField(
                verbose_name="阶段检查点", default=dict, blank=True,
                db_comment="已上报的处理阶段及 OSS 产物；接力 worker 按此恢复",
            ),
        ),
    ]

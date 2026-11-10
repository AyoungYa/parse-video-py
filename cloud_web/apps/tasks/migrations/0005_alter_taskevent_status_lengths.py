from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tasks", "0004_task_confirmation_states")]

    operations = [
        migrations.AlterField(
            model_name="taskevent",
            name="from_status",
            field=models.CharField(
                max_length=24, blank=True, default="",
                db_comment="变更前状态；首次创建为空",
            ),
        ),
        migrations.AlterField(
            model_name="taskevent",
            name="to_status",
            field=models.CharField(max_length=24, db_comment="变更后状态"),
        ),
    ]

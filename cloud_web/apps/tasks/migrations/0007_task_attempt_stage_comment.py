from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tasks", "0006_task_checkpoints")]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="attempt",
            field=models.IntegerField(
                verbose_name="当前阶段尝试次数", default=0,
                db_comment="当前阶段连续领取次数；阶段检查点成功后重置",
            ),
        ),
    ]

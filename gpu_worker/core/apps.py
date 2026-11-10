from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "算力节点"

    def ready(self):
        # Django 没有迁移记录表名的设置项；在应用就绪后调整仅本服务的记录模型。
        from django.db.migrations.recorder import MigrationRecorder
        from django.contrib.contenttypes.models import ContentType

        MigrationRecorder.Migration._meta.db_table = "worker_django_migrations"
        ContentType._meta.db_table = "worker_django_content_type"

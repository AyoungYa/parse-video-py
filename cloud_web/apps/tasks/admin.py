from django.contrib import admin

from .models import Task, TaskEvent, WorkerNode


@admin.register(WorkerNode)
class WorkerNodeAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "status", "last_heartbeat_at", "max_concurrency")
    list_filter = ("status",)
    readonly_fields = ("token_hash",)


class TaskEventInline(admin.TabularInline):
    model = TaskEvent
    extra = 0
    readonly_fields = ("from_status", "to_status", "actor", "detail", "created_at")
    can_delete = False


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "status",
        "duration_ms",
        "attempt",
        "points_cost",
        "worker",
        "created_at",
        "finished_at",
    )
    list_filter = ("status", "worker")
    search_fields = ("id", "user__phone")
    readonly_fields = ("id", "created_at", "lease_expires_at")
    inlines = [TaskEventInline]
    date_hierarchy = "created_at"


@admin.register(TaskEvent)
class TaskEventAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "from_status", "to_status", "actor", "created_at")
    list_filter = ("to_status", "actor")
    search_fields = ("task__id",)

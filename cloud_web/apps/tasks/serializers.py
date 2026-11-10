"""序列化：只暴露前端需要的字段，绝不返回 OSS 内部键以外的敏感信息。"""

from __future__ import annotations

from rest_framework import serializers

from .models import Task, TaskStatus, WorkerNode


def _public_status(task: Task) -> str:
    if task.status in (TaskStatus.WORKER_OFFLINE, TaskStatus.PENDING, TaskStatus.LEASED):
        checkpoints = task.checkpoints or {}
        previous = checkpoints.get("_retry_display_status") or checkpoints.get("_offline_display_status")
        if previous:
            return previous
    return task.status


class TaskListSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    duration_seconds = serializers.FloatField(read_only=True)
    bbox_confirmed = serializers.SerializerMethodField()

    def get_bbox_confirmed(self, obj: Task) -> bool:
        return bool((obj.params or {}).get("bbox_confirmed"))

    def get_status(self, obj: Task) -> str:
        return _public_status(obj)

    class Meta:
        model = Task
        fields = (
            "id",
            "task_no",
            "status",
            "progress",
            "bbox_confirmed",
            "duration_seconds",
            "width",
            "height",
            "created_at",
            "finished_at",
        )


class TaskDetailSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    can_retry = serializers.SerializerMethodField()
    error_code = serializers.SerializerMethodField()
    error_message = serializers.SerializerMethodField()
    duration_seconds = serializers.FloatField(read_only=True)
    all_green = serializers.SerializerMethodField()

    def get_status(self, obj: Task) -> str:
        return _public_status(obj)

    def get_can_retry(self, obj: Task) -> bool:
        return obj.status == TaskStatus.FAILED and bool(obj.original_key)

    def get_error_code(self, obj: Task) -> str:
        return ""

    def get_error_message(self, obj: Task) -> str:
        if obj.status == TaskStatus.FAILED:
            return "系统原因处理失败。"
        if obj.status not in (TaskStatus.CANCELLED, TaskStatus.EXPIRED):
            return ""
        return obj.error_message

    class Meta:
        model = Task
        fields = (
            "id",
            "task_no",
            "status",
            "can_retry",
            "progress",
            "duration_seconds",
            "size_bytes",
            "width",
            "height",
            "fps",
            "params",
            "metrics",
            "all_green",
            "points_cost",
            "error_code",
            "error_message",
            "created_at",
            "started_at",
            "finished_at",
        )

    def get_all_green(self, obj: Task) -> bool:
        return bool((obj.metrics or {}).get("all_green"))


class WorkerNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkerNode
        fields = ("id", "name", "status", "capabilities", "max_concurrency", "last_heartbeat_at")

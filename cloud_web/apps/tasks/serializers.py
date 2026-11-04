"""序列化：只暴露前端需要的字段，绝不返回 OSS 内部键以外的敏感信息。"""

from __future__ import annotations

from rest_framework import serializers

from .models import Task, WorkerNode


class TaskListSerializer(serializers.ModelSerializer):
    duration_seconds = serializers.FloatField(read_only=True)

    class Meta:
        model = Task
        fields = (
            "id",
            "status",
            "progress",
            "duration_seconds",
            "width",
            "height",
            "created_at",
            "finished_at",
        )


class TaskDetailSerializer(serializers.ModelSerializer):
    duration_seconds = serializers.FloatField(read_only=True)
    all_green = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = (
            "id",
            "status",
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

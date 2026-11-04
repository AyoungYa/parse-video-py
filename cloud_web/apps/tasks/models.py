"""任务队列模型。

状态机定义见 docs/product/架构设计.md §四。核心约束：
  · 一个任务同一时刻只能被一个 worker 持有（靠 lease_expires_at + 原子领取）；
  · 所有状态变更都留一条 TaskEvent，便于排查「这单为什么卡住」。
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class TaskStatus(models.TextChoices):
    AWAITING_UPLOAD = "awaiting_upload", "待上传"
    PENDING = "pending", "待处理"
    LEASED = "leased", "已领取"
    RUNNING = "running", "处理中"
    VERIFYING = "verifying", "验收中"
    SUCCEEDED = "succeeded", "已完成"
    FAILED = "failed", "失败"
    CANCELLED = "cancelled", "已取消"
    EXPIRED = "expired", "已过期"


#: 可被回收的「在途」状态
IN_FLIGHT = (TaskStatus.LEASED, TaskStatus.RUNNING, TaskStatus.VERIFYING)


class WorkerNode(models.Model):
    """一台算力机。token 只存哈希，明文由后台生成时展示一次。"""

    class Status(models.TextChoices):
        ONLINE = "online", "在线"
        OFFLINE = "offline", "离线"
        DISABLED = "disabled", "已禁用"

    name = models.CharField("名称", max_length=64, unique=True)
    token_hash = models.CharField(max_length=64, unique=True)
    capabilities = models.JSONField("能力", default=dict, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OFFLINE)
    max_concurrency = models.IntegerField("并发上限", default=1)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "worker_node"
        verbose_name = "算力节点"
        verbose_name_plural = "算力节点"

    def __str__(self) -> str:
        return f"{self.name}({self.status})"

    @staticmethod
    def hash_token(raw: str) -> str:
        import hashlib

        return hashlib.sha256(raw.encode()).hexdigest()


class Task(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="tasks")
    status = models.CharField(
        max_length=20, choices=TaskStatus.choices, default=TaskStatus.AWAITING_UPLOAD
    )

    # OSS 对象键（不含 bucket）
    original_key = models.CharField(max_length=255, blank=True, default="")
    mask_key = models.CharField(max_length=255, blank=True, default="")
    result_key = models.CharField(max_length=255, blank=True, default="")
    report_key = models.CharField(max_length=255, blank=True, default="")

    # 探测到的素材元数据
    duration_ms = models.BigIntegerField(default=0)
    size_bytes = models.BigIntegerField(default=0)
    width = models.IntegerField(default=0)
    height = models.IntegerField(default=0)
    fps = models.FloatField(default=0)

    params = models.JSONField("处理参数", default=dict, blank=True)
    metrics = models.JSONField("验收指标", default=dict, blank=True)

    worker = models.ForeignKey(
        WorkerNode, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks"
    )
    lease_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    attempt = models.IntegerField("已领取次数", default=0)
    progress = models.FloatField("进度 0~1", default=0)

    points_cost = models.IntegerField("预扣积分", default=0)
    refunded_at = models.DateTimeField("退还时间", null=True, blank=True)

    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "task"
        verbose_name = "任务"
        verbose_name_plural = "任务"
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["user", "-created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.id} {self.status}"

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        )

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000

    def can_afford_limit(self) -> bool:
        """是否超出包月的月度分钟上限。"""
        return self.duration_seconds / 60 <= settings.MONTHLY_MINUTES_LIMIT


class TaskEvent(models.Model):
    """状态变更审计。排查线上问题全靠它。"""

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=20, blank=True, default="")
    to_status = models.CharField(max_length=20)
    actor = models.CharField("触发者", max_length=64, blank=True, default="system")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "task_event"
        verbose_name = "任务事件"
        verbose_name_plural = "任务事件"
        ordering = ["-id"]

"""任务队列模型。

状态机定义见 docs/product/架构设计.md §四。核心约束：
  · 一个任务同一时刻只能被一个 worker 持有（靠 lease_expires_at + 原子领取）；
  · 所有状态变更都留一条 TaskEvent，便于排查「这单为什么卡住」。

数据库规范：无 DB 外键、字段/表带中文 comment、索引 idx_/uniq_ 显式命名、
每张表一个对外唯一号（见 apps/bizno.py）。
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from apps.bizno import (
    NO_MAX_LENGTH,
    gen_task_event_no,
    gen_task_no,
    gen_worker_no,
)


class TaskStatus(models.TextChoices):
    AWAITING_UPLOAD = "awaiting_upload", "待上传"
    PENDING = "pending", "待处理"
    LEASED = "leased", "已领取"
    EXTRACTING_FRAMES = "extracting_frames", "提取关键帧"
    LOCATING_WATERMARK = "locating_watermark", "定位水印"
    AWAITING_CONFIRMATION = "awaiting_confirmation", "待确认水印"
    REMOVING_WATERMARK = "removing_watermark", "去水印中"
    RUNNING = "running", "处理中"
    VERIFYING = "verifying", "验收中"
    SUCCEEDED = "succeeded", "已完成"
    FAILED = "failed", "失败"
    CANCELLED = "cancelled", "已取消"
    EXPIRED = "expired", "已过期"
    #: 自动定位不可信，等用户手动框选水印位置。
    #: ★ 它不是终态：用户提交 bbox 后会重新入队（见 views_user.submit_bbox）。
    NEEDS_MANUAL = "needs_manual", "待框选"


#: 可被回收的「在途」状态
IN_FLIGHT = (TaskStatus.LEASED, TaskStatus.EXTRACTING_FRAMES, TaskStatus.LOCATING_WATERMARK,
             TaskStatus.REMOVING_WATERMARK, TaskStatus.RUNNING, TaskStatus.VERIFYING)


class WorkerNode(models.Model):
    """一台算力机。token 只存哈希，明文由后台生成时展示一次。"""

    class Status(models.TextChoices):
        ONLINE = "online", "在线"
        OFFLINE = "offline", "离线"
        DISABLED = "disabled", "已禁用"

    worker_no = models.CharField(
        "节点号", max_length=NO_MAX_LENGTH, default=gen_worker_no, editable=False,
        db_comment="对外节点号 W+日期+随机",
    )
    name = models.CharField(
        "名称", max_length=64,
        db_comment="节点唯一名称，便于运维识别",
    )
    token_hash = models.CharField(
        max_length=64,
        db_comment="节点令牌 SHA-256，明文仅创建时展示一次",
    )
    capabilities = models.JSONField(
        "能力", default=dict, blank=True,
        db_comment="能力声明：设备、显存、支持的分辨率等",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OFFLINE,
        db_comment="节点状态：online / offline / disabled",
    )
    max_concurrency = models.IntegerField(
        "并发上限", default=1,
        db_comment="同时可领取的任务数上限",
    )
    last_heartbeat_at = models.DateTimeField(
        null=True, blank=True,
        db_comment="最近心跳时间；超时由 reclaim_leases 回收租约",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_comment="节点创建时间",
    )

    class Meta:
        db_table = "worker_node"
        db_table_comment = "算力节点：出站领取任务的 worker，令牌只存哈希"
        verbose_name = "算力节点"
        verbose_name_plural = "算力节点"
        constraints = [
            models.UniqueConstraint(fields=["worker_no"], name="uniq_worker_node_no"),
            models.UniqueConstraint(fields=["name"], name="uniq_worker_node_name"),
            models.UniqueConstraint(fields=["token_hash"], name="uniq_worker_node_token_hash"),
        ]

    def __str__(self) -> str:
        return f"{self.name}({self.status})"

    @staticmethod
    def hash_token(raw: str) -> str:
        import hashlib

        return hashlib.sha256(raw.encode()).hexdigest()


class Task(models.Model):
    id = models.UUIDField(
        primary_key=True, default=uuid.uuid4, editable=False,
        db_comment="内部主键 UUID；对外请用 task_no",
    )
    task_no = models.CharField(
        "任务号", max_length=NO_MAX_LENGTH, default=gen_task_no, editable=False,
        db_comment="对外任务号 T+日期+随机；用户端查询/读写一律用此值",
    )
    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="tasks",
        db_constraint=False, db_index=False,
        db_comment="所属用户 ID（逻辑外键，不建 DB 约束）",
    )
    status = models.CharField(
        max_length=24, choices=TaskStatus.choices, default=TaskStatus.AWAITING_UPLOAD,
        db_comment="任务状态；定位后等待用户确认再去水印",
    )

    # OSS 对象键（不含 bucket）
    original_key = models.CharField(
        max_length=255, blank=True, default="",
        db_comment="原片 OSS 对象键",
    )
    mask_key = models.CharField(
        max_length=255, blank=True, default="",
        db_comment="mask 图 OSS 对象键",
    )
    result_key = models.CharField(
        max_length=255, blank=True, default="",
        db_comment="成片 OSS 对象键",
    )
    report_key = models.CharField(
        max_length=255, blank=True, default="",
        db_comment="验收报告 report.json 的 OSS 对象键",
    )

    # 探测到的素材元数据
    duration_ms = models.BigIntegerField(default=0, db_comment="视频时长（毫秒）")
    size_bytes = models.BigIntegerField(default=0, db_comment="原片字节数")
    width = models.IntegerField(default=0, db_comment="画面宽度（像素）")
    height = models.IntegerField(default=0, db_comment="画面高度（像素）")
    fps = models.FloatField(default=0, db_comment="帧率")

    params = models.JSONField(
        "处理参数", default=dict, blank=True,
        db_comment="处理参数；自动定位失败时存 bbox 与候选框",
    )
    metrics = models.JSONField(
        "验收指标", default=dict, blank=True,
        db_comment="验收指标：残留/纹理/闪烁三项及 all_green",
    )

    worker = models.ForeignKey(
        WorkerNode, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks",
        db_constraint=False, db_index=False,
        db_comment="当前持有者节点 ID（逻辑外键），未领取为 NULL",
    )
    lease_expires_at = models.DateTimeField(
        null=True, blank=True,
        db_comment="租约到期时间；超时由 reclaim_leases 打回队列",
    )
    attempt = models.IntegerField("已领取次数", default=0, db_comment="已被 worker 领取的次数，上限见 MAX_ATTEMPT")
    progress = models.FloatField("进度 0~1", default=0, db_comment="处理进度 0~1")

    points_cost = models.IntegerField("预扣积分", default=0, db_comment="本单预扣积分数")
    refunded_at = models.DateTimeField("退还时间", null=True, blank=True, db_comment="积分退还时间；非空表示已退款")

    error_code = models.CharField(max_length=64, blank=True, default="", db_comment="错误码，成功为空")
    error_message = models.TextField(blank=True, default="", db_comment="错误详情，含阶段输出尾部")

    created_at = models.DateTimeField(auto_now_add=True, db_comment="创建时间")
    started_at = models.DateTimeField(null=True, blank=True, db_comment="首次开始处理时间")
    finished_at = models.DateTimeField(null=True, blank=True, db_comment="终态时间")

    class Meta:
        db_table = "task"
        db_table_comment = "处理任务：一次去水印作业的全生命周期与验收结果"
        verbose_name = "任务"
        verbose_name_plural = "任务"
        constraints = [
            models.UniqueConstraint(fields=["task_no"], name="uniq_task_no"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"], name="idx_task_status_created"),
            models.Index(fields=["user", "-created_at"], name="idx_task_user_created"),
            models.Index(fields=["worker"], name="idx_task_worker"),
            models.Index(fields=["lease_expires_at"], name="idx_task_lease_expires_at"),
            models.Index(fields=["created_at"], name="idx_task_created_at"),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.task_no} {self.status}"

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

    event_no = models.CharField(
        "事件号", max_length=NO_MAX_LENGTH, default=gen_task_event_no, editable=False,
        db_comment="对外事件号 E+日期+随机",
    )
    task = models.ForeignKey(
        Task, on_delete=models.CASCADE, related_name="events",
        db_constraint=False, db_index=False,
        db_comment="所属任务 ID（逻辑外键，不建 DB 约束）",
    )
    from_status = models.CharField(
        max_length=20, blank=True, default="",
        db_comment="变更前状态；首次创建为空",
    )
    to_status = models.CharField(
        max_length=20,
        db_comment="变更后状态",
    )
    actor = models.CharField(
        "触发者", max_length=64, blank=True, default="system",
        db_comment="触发者：system / 用户名 / worker 名",
    )
    detail = models.JSONField(
        default=dict, blank=True,
        db_comment="附加信息：错误码、重试标记、耗时等",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_comment="事件发生时间",
    )

    class Meta:
        db_table = "task_event"
        db_table_comment = "任务状态变更审计流水：排查线上卡单全靠它"
        verbose_name = "任务事件"
        verbose_name_plural = "任务事件"
        constraints = [
            models.UniqueConstraint(fields=["event_no"], name="uniq_task_event_no"),
        ]
        indexes = [
            models.Index(fields=["task", "-created_at"], name="idx_task_event_task_created"),
            models.Index(fields=["created_at"], name="idx_task_event_created_at"),
        ]
        ordering = ["-id"]

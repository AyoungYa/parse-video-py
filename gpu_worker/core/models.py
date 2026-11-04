"""本地镜像表。

**权威状态在云端**，这里只是为了让「人在这台机器前」时能看清它干了什么：
哪单跑过、跑了多久、卡在哪个阶段。丢了不影响业务。
"""

from __future__ import annotations

from django.db import models


class LocalTask(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "处理中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        ABORTED = "aborted", "已中止"

    task_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    duration_seconds = models.FloatField(default=0)
    stage = models.CharField(max_length=32, blank=True, default="")
    progress = models.FloatField(default=0)
    attempts = models.IntegerField(default=1)
    metrics = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "local_task"
        ordering = ["-started_at"]
        verbose_name = "本地任务"
        verbose_name_plural = "本地任务"


class StageLog(models.Model):
    """每个阶段的退出码与耗时。排查「为什么这单失败」先看它。"""

    task_id = models.CharField(max_length=64, db_index=True)
    stage = models.CharField(max_length=32)
    exit_code = models.IntegerField()
    elapsed_seconds = models.FloatField()
    tail = models.TextField("输出尾部 200 行", blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stage_log"
        ordering = ["-id"]
        verbose_name = "阶段日志"
        verbose_name_plural = "阶段日志"

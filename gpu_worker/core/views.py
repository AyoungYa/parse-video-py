"""本地状态面板：人在机器前时看一眼「它现在在干什么、最近跑成什么样」。

刻意极简——这台机器不对外，不做登录、不做管理后台。
"""

from __future__ import annotations

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render

from .models import LocalTask, StageLog


def dashboard(request):
    tasks = LocalTask.objects.all()[:30]
    logs = StageLog.objects.all()[:20]
    return render(
        request,
        "core/dashboard.html",
        {
            "tasks": tasks,
            "logs": logs,
            "settings": settings,
        },
    )


def status_json(_request):
    latest = LocalTask.objects.first()
    return JsonResponse(
        {
            "worker_id": settings.WORKER_ID,
            "cloud": settings.CLOUD_BASE_URL,
            "device": settings.TORCH_DEVICE,
            "latest_task": (
                {
                    "task_id": latest.task_id,
                    "status": latest.status,
                    "stage": latest.stage,
                    "progress": latest.progress,
                    "started_at": latest.started_at,
                }
                if latest
                else None
            ),
        }
    )

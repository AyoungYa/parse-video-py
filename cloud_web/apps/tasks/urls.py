from django.urls import path

from . import views_user, views_worker

urlpatterns = [
    # 用户侧
    path("uploads/sign", views_user.sign_upload, name="uploads-sign"),
    path("tasks", views_user.list_tasks, name="task-list"),
    path("tasks/<str:task_id>", views_user.task_detail, name="task-detail"),
    path("tasks/<str:task_id>/confirm", views_user.confirm_upload, name="task-confirm"),
    path("tasks/<str:task_id>/bbox", views_user.submit_bbox, name="task-submit-bbox"),
    path("tasks/<str:task_id>/cancel", views_user.cancel_task, name="task-cancel"),
    # Worker 协议（算力机调用）
    path("worker/claim", views_worker.claim, name="worker-claim"),
    path("worker/heartbeat", views_worker.heartbeat, name="worker-heartbeat"),
    path("worker/complete", views_worker.complete, name="worker-complete"),
    path("worker/fail", views_worker.fail, name="worker-fail"),
]

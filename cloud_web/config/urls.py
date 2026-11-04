"""无痕匠云端路由总表。

约定：
  /api/v1/...        面向浏览器的用户 API
  /api/v1/worker/... 面向算力机的 worker 协议（X-Worker-Token 鉴权）
  /admin/            Django 后台（运营用）
"""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def healthz(_request):
    return JsonResponse({"ok": True, "service": "cloud_web"})


urlpatterns = [
    path("healthz", healthz),
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/v1/", include("apps.tasks.urls")),
    path("api/v1/", include("apps.billing.urls")),
]

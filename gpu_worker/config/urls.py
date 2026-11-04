from django.http import JsonResponse
from django.urls import include, path


def healthz(_request):
    return JsonResponse({"ok": True, "service": "gpu_worker"})


urlpatterns = [
    path("healthz", healthz),
    path("", include("core.urls")),
]

from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("status.json", views.status_json, name="status-json"),
]

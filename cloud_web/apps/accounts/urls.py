from django.urls import path

from . import views

urlpatterns = [
    path("auth/sms/send", views.sms_send, name="auth-sms-send"),
    path("auth/sms/login", views.sms_login, name="auth-sms-login"),
    path("auth/logout", views.logout, name="auth-logout"),
    path("auth/me", views.me, name="auth-me"),
]

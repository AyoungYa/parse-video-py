from django.urls import path

from . import views

urlpatterns = [
    path("site/config", views.site_config, name="site-config"),
    path("auth/captcha", views.captcha, name="auth-captcha"),
    path("auth/sms/send", views.sms_send, name="auth-sms-send"),
    path("auth/sms/login", views.sms_login, name="auth-sms-login"),
    path("auth/logout", views.logout, name="auth-logout"),
    path("auth/me", views.me, name="auth-me"),
]

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import AuthToken, User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("id", "phone", "nickname", "is_active", "date_joined")
    search_fields = ("phone", "nickname", "wx_openid")
    fieldsets = UserAdmin.fieldsets + (("扩展信息", {"fields": ("phone", "wx_openid", "nickname", "avatar")}),)


@admin.register(AuthToken)
class AuthTokenAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at", "expires_at", "last_used_at")
    readonly_fields = ("key_hash",)

from django.contrib import admin

from .models import PointLedger, SignInRecord, Subscription


@admin.register(PointLedger)
class PointLedgerAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "delta", "balance_after", "reason", "ref_id", "created_at")
    list_filter = ("reason",)
    search_fields = ("user__phone", "ref_id")


@admin.register(SignInRecord)
class SignInRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "sign_date", "points", "streak")
    search_fields = ("user__phone",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "plan", "status", "started_at", "expires_at", "minutes_used")
    list_filter = ("plan", "status")

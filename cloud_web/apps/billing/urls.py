from django.urls import path

from . import views

urlpatterns = [
    path("billing/sign-in", views.do_sign_in, name="billing-sign-in"),
    path("billing/points", views.my_points, name="billing-points"),
    path("billing/membership/orders", views.create_membership_order, name="billing-membership-create"),
    path("billing/membership/orders/<str:order_no>", views.membership_order, name="billing-membership-order"),
    path("billing/wechat/notify", views.wechat_pay_notify, name="billing-wechat-notify"),
]

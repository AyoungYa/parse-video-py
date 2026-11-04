from django.urls import path

from . import views

urlpatterns = [
    path("billing/sign-in", views.do_sign_in, name="billing-sign-in"),
    path("billing/points", views.my_points, name="billing-points"),
]

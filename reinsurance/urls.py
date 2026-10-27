from django.urls import path

from . import views

urlpatterns = [
    path("", views.treaty_list, name="treaty_list"),
    path("new/", views.treaty_create, name="treaty_create"),
    path("fac/", views.fac_queue, name="fac_queue"),
    path("fac/<int:pk>/confirm/", views.fac_confirm, name="fac_confirm"),
    path("installments/<int:pk>/pay/", views.installment_pay, name="installment_pay"),
    path("<int:pk>/xol-adjust/", views.xol_adjust, name="xol_adjust"),
    path("<int:pk>/xol/", views.xol_analysis, name="xol_analysis"),
    path("<int:pk>/xol/post/", views.xol_post, name="xol_post"),
    path("xol/<int:pk>/receive/", views.xol_receive, name="xol_receive"),
    path("<int:pk>/xol/apply-rate/", views.xol_apply_rate, name="xol_apply_rate"),
    path("<int:pk>/", views.treaty_detail, name="treaty_detail"),
    path("<int:pk>/modify/", views.treaty_modify, name="treaty_modify"),
]

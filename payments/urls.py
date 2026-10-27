from django.urls import path

from . import views

urlpatterns = [
    path("payments/", views.payment_list, name="payment_list"),
    path("payments/policy/<int:policy_pk>/receipt/", views.record_receipt, name="record_receipt"),
    path("payments/policy/<int:policy_pk>/request/", views.request_payment, name="request_payment"),
    path("payments/<int:pk>/cancel/", views.cancel_request, name="cancel_request"),
    path("payments/<int:pk>/receipt.pdf", views.receipt_download, name="receipt_download"),
    path("payments/<int:pk>/share/", views.payment_share, name="payment_share"),
    path("pay/<str:token>/", views.pay_online, name="pay_online"),
    path("pay/<str:token>/receipt.pdf", views.pay_receipt, name="pay_receipt"),
]

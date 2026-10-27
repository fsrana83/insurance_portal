from django.urls import path

from . import views

urlpatterns = [
    path("accounting/", views.transactions, name="acc_transactions"),
    path("accounting/trial-balance/", views.trial_balance, name="acc_tb"),
    path("accounting/ifrs17/", views.ifrs17, name="acc_ifrs17"),
    path("accounting/ifrs17/run/", views.run_revenue, name="acc_run_revenue"),
    path("accounting/documents/", views.fin_documents, name="acc_documents"),
    path("accounting/documents/<int:pk>.pdf", views.document_pdf, name="fin_document_pdf"),
]

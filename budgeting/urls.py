from django.urls import path

from . import views

urlpatterns = [
    path("budget/", views.budget_list, name="budget_list"),
    path("budget/new/", views.budget_new, name="budget_new"),
    path("budget/<int:pk>/", views.budget_edit, name="budget_edit"),
    path("budget/<int:pk>/<str:action>/", views.budget_action, name="budget_action"),
    path("budget-analytics/", views.analytics, name="budget_analytics"),
    path("budget-forecast/", views.forecast_view, name="budget_forecast"),
    path("budget-outliers/", views.outlier_view, name="budget_outliers"),
]

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core import views as core_views
from motor import views_ext

admin.site.site_header = "Advance Motor Insurance Solution — Back office"
admin.site.site_title = "Advance Motor Insurance Solution"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(template_name="registration/password_reset.html"),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="registration/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(template_name="registration/password_reset_confirm.html"),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(template_name="registration/password_reset_complete.html"),
        name="password_reset_complete",
    ),
    path("confirmations/", core_views.confirmations, name="confirmations"),
    path("confirmations/<int:pk>/<str:decision>/", core_views.confirmation_review, name="confirmation_review"),
    path("help/", core_views.help_page, name="help"),
    path("change-log/", core_views.changelog, name="changelog"),
    path("company/", core_views.company_setup, name="company_setup"),
    path("demo-data/", core_views.demo_data, name="demo_data"),
    path("demo-data/status/<int:pk>/", core_views.demo_status, name="demo_status"),
    path("flex-fields/", core_views.flex_list, name="flex_list"),
    path("flex-fields/new/", core_views.flex_edit, name="flex_new"),
    path("flex-fields/<int:pk>/", core_views.flex_edit, name="flex_edit"),
    path("flex-fields/<int:pk>/toggle/", core_views.flex_toggle, name="flex_toggle"),
    path("d/<str:token>/", views_ext.share_view, name="share_view"),
    path("d/<str:token>/<str:key>.pdf", views_ext.share_download, name="share_download"),
    path("", include("payments.urls")),
    path("", include("accounting.urls")),
    path("", include("budgeting.urls")),
    path("claims/", include("claims.urls")),
    path("pnc/", include("pnc.urls")),
    path("reinsurance/", include("reinsurance.urls")),
    path("", include("motor.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

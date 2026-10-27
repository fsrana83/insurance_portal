from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import AuditLog, Currency, DocumentSequence, UserProfile


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "symbol", "symbol_fallback", "decimals", "rate_to_base", "is_base", "active")
    list_editable = ("rate_to_base", "active")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "user", "action", "model", "object_id", "object_repr")
    list_filter = ("action", "model")
    search_fields = ("object_repr", "object_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ProfileInline(admin.StackedInline):
    model = UserProfile
    fk_name = "user"
    can_delete = False


admin.site.unregister(User)


@admin.register(User)
class ProfiledUserAdmin(UserAdmin):
    inlines = [ProfileInline]


admin.site.register(DocumentSequence)

from django.contrib import admin

from . import models as m


class ProductCoverInline(admin.TabularInline):
    model = m.ProductCover
    extra = 0


@admin.register(m.Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "includes_od", "is_tariff_regulated", "expense_ratio", "standard_excess", "tp_limit", "active")
    inlines = [ProductCoverInline]


@admin.register(m.Cover)
class CoverAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "kind", "rate_pct", "flat_amount")


@admin.register(m.RateTable)
class RateTableAdmin(admin.ModelAdmin):
    list_display = ("cover", "vehicle_type", "usage", "cc_min", "cc_max", "age_min", "age_max", "rate_pct", "flat_premium")
    list_filter = ("cover", "vehicle_type", "usage")


admin.site.register([m.Discount, m.Fee, m.ApprovalLimit, m.CommissionRule])


@admin.register(m.Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "customer_type", "civil_id", "cr_number", "phone")
    search_fields = ("name", "civil_id", "cr_number")


@admin.register(m.Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("__str__", "chassis_no", "vehicle_type", "usage", "customer")
    search_fields = ("chassis_no", "plate_number")


class RiskInline(admin.TabularInline):
    model = m.PolicyRisk
    extra = 0
    fields = ("plate", "make", "model", "sum_insured", "net_premium")
    readonly_fields = fields


@admin.register(m.Policy)
class PolicyAdmin(admin.ModelAdmin):
    list_display = ("policy_no", "customer", "product", "currency", "status", "start_date", "end_date", "total_payable")
    list_filter = ("status", "product", "currency")
    search_fields = ("policy_no", "customer__name")
    inlines = [RiskInline]


admin.site.register([m.Proposal, m.Endorsement, m.Commission])

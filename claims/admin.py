from django.contrib import admin

from . import models as m


@admin.register(m.Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("claim_no", "policy", "status", "date_of_loss", "loss_type", "reserve", "net_payable")
    list_filter = ("status", "loss_type")
    search_fields = ("claim_no", "policy__policy_no")


@admin.register(m.Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "provider_type", "preferred", "governorate", "average_score", "active")
    list_filter = ("provider_type", "preferred")


admin.site.register([m.Subrogation, m.ProviderRating, m.ClaimPayment, m.Recovery, m.RepairEstimate, m.LetterOfGuarantee])

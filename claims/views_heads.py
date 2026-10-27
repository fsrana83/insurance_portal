from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import audit
from core.models import user_role
from core.permissions import role_required

from . import heads, services
from .models import Claim, ClaimHead, PaymentRelease
from .views import claim_scope

ROLES = ("CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE")


@role_required(*ROLES)
def claim_heads(request, pk):
    claim = get_object_or_404(claim_scope(request.user), pk=pk)
    role = user_role(request.user)
    return render(request, "claims/heads.html", {
        "c": claim, "cur": claim.policy.currency, "heads": claim.heads.all(), "head_choices": [h for h in ClaimHead.HEADS if h[0] != "OD"],
        "releases": PaymentRelease.objects.filter(head__claim=claim), "limits": heads.settings.CLAIM_HEAD_LIMITS,
        "cash_limit": heads.cash_limit(claim), "can_claims": role in ("ADMIN", "CLAIMS_HANDLER", "CLAIMS_MANAGER"),
        "can_pay": role in ("ADMIN", "FINANCE"), "od_net": heads.od_net(claim)})


@role_required("CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE")
@require_POST
def head_action(request, pk, action):
    claim = get_object_or_404(claim_scope(request.user), pk=pk)
    P = request.POST
    try:
        with audit.acting(request.user):
            if action == "add":
                heads.add_head(claim, P.get("head"), P.get("estimate"), P.get("description", ""), P.get("excess") or 0, P.get("recovery") or 0, P.get("payee", ""))
                messages.success(request, "Head added.")
            elif action in ("approve", "reject", "release"):
                h = get_object_or_404(ClaimHead, pk=P.get("head_id"), claim=claim)
                if action == "approve":
                    heads.approve_head(h, P.get("amount"), request.user)
                    messages.success(request, "Head approved" + (f" (capped at the limit {h.limit_applied})." if h.limit_applied else "."))
                elif action == "reject":
                    heads.reject_head(h, request.user)
                else:
                    r = heads.issue_release(h, P.get("amount"), request.user)
                    messages.success(request, f"Credit note {r.number} sent to Accounts.")
            elif action == "disburse":
                r = get_object_or_404(PaymentRelease, pk=P.get("release_id"), head__claim=claim)
                heads.disburse(r, request.user, P.get("reference", ""))
                messages.success(request, f"{r.number} paid.")
    except services.ClaimError as e:
        messages.error(request, str(e))
    return redirect("claim_heads", pk=claim.pk)

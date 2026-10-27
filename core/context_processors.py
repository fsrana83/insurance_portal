import json

from django.conf import settings

from .models import Company, Currency, ROLE_LABELS, user_role


def app_context(request):
    role = user_role(request.user)
    cur = list(Currency.objects.filter(active=True))
    ctx = {
        "role": role,
        "role_label": ROLE_LABELS.get(role, ""),
        "base_currency": settings.BASE_CURRENCY,
        "app_name": settings.APP_NAME,
        "company": Company.get(),
        "all_currencies": cur,
        "currency_json": {c.code: {"symbol": c.symbol, "fallback": c.symbol_fallback or c.code, "decimals": c.decimals} for c in cur},
    }
    if role:
        a = role == "ADMIN"
        ctx["nav"] = {
            "quotes": a or role in ("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER"),
            "new_quote": a or role in ("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER"),
            "approvals": a or role in ("PRODUCTION_MANAGER", "UNDERWRITER"),
            "customers": role != "CUSTOMER",
            "reinsurance": a or role in ("UNDERWRITER", "FINANCE"),
            "providers": a or role in ("CLAIMS_HANDLER", "CLAIMS_MANAGER", "UNDERWRITER", "PRODUCTION_MANAGER", "FINANCE"),
            "reports": a or role in ("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER", "CREDIT_CONTROL"),
            "admin": request.user.is_staff,
            "vouchers": a or role in ("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER"),
            "payments": a or role in ("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CREDIT_CONTROL"),
            "accounting": a or role in ("FINANCE", "PRODUCTION_MANAGER", "UNDERWRITER"),
            "ai": a or role in ("CLAIMS_HANDLER", "CLAIMS_MANAGER", "UNDERWRITER", "PRODUCTION_MANAGER", "FINANCE"),
            "budget": a or role in ("FINANCE", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_MANAGER"),
            "confirm": a or role in ("PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE", "AGENT", "BROKER"),
            "changelog": a or role in ("PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE"),
            "credit": a or role in ("CREDIT_CONTROL", "FINANCE"),
            "pnc": a or role in ("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE", "CREDIT_CONTROL"),
            "setup": a, "flex": a or role in ("PRODUCTION_MANAGER", "UNDERWRITER"),
        }
        if ctx["nav"]["confirm"]:
            from .confirm import CHECKERS
            from .models import PendingAction
            mine = {k for k, v in CHECKERS.items() if a or role in v}
            ctx["pending_confirmations"] = PendingAction.objects.filter(status="PENDING", kind__in=mine).exclude(requested_by=request.user).count() if mine else 0
        if ctx["nav"]["credit"]:
            from motor.models import CreditApproval
            ctx["pending_credit"] = CreditApproval.objects.filter(status="PENDING").count()
        if ctx["nav"]["approvals"]:
            from motor.models import Proposal
            ctx["pending_approvals"] = Proposal.objects.filter(status="PENDING").count()
        if ctx["nav"]["reinsurance"]:
            from reinsurance.models import Cession
            ctx["fac_pending"] = Cession.objects.filter(is_facultative=True, fac_confirmed=False).count()
    return ctx

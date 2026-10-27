from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import Currency, convert, user_role
from core.permissions import CAN_CLAIM, SEES_ALL_BUSINESS, role_required, scope
from motor.models import Policy

from core import flex
from core.models import FlexField

from . import ai
from . import forms as f
from . import services
from .models import CatEvent, Claim, ClaimDocument, LetterOfGuarantee, MedicalBill, Provider, Subrogation

HANDLERS = ("CLAIMS_HANDLER", "CLAIMS_MANAGER")


def claim_scope(user, qs=None):
    qs = Claim.objects.all() if qs is None else qs
    role = user_role(user)
    if role in SEES_ALL_BUSINESS:
        return qs
    return qs.filter(policy__in=scope(user, Policy.objects.all()))


@login_required
def claim_list(request):
    qs = claim_scope(request.user).select_related("policy__customer", "policy__currency", "risk")
    status, q = request.GET.get("status"), request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(claim_no__icontains=q) | Q(policy__policy_no__icontains=q) | Q(policy__customer__name__icontains=q))
    counts = {s: claim_scope(request.user).filter(status=s).count() for s, _ in Claim.STATUSES}
    return render(request, "claims/claim_list.html", {"claims": qs[:200], "statuses": Claim.STATUSES, "status": status, "q": q, "counts": counts})


@role_required(*CAN_CLAIM)
def claim_new(request):
    policies = scope(request.user, Policy.objects.exclude(status="PENDING_FAC"))
    initial = {}
    pid = request.GET.get("policy")
    if pid and policies.filter(pk=pid).exists():
        pol = policies.get(pk=pid)
        initial = {"policy": pol, "risk": pol.risks.first()}
    form = f.ClaimForm(request.POST or None, request.FILES or None, policies=policies, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            claim = services.register_claim(
                d["policy"], d["risk"], request.user, date_of_loss=d["date_of_loss"], loss_type=d["loss_type"],
                cause_of_loss=d["cause_of_loss"], location=d["location"], governorate=d["governorate"],
                police_report_no=d["police_report_no"], estimated_damage=d["estimated_damage"], driver_age=d["driver_age"],
                young_driver=d["young_driver"], undisclosed_driver=d["undisclosed_driver"], third_party_liable=d["third_party_liable"],
                driver=d.get("driver"), is_total_loss=d["is_total_loss"], damage_zones=d["zones"], damage_notes=d["damage_notes"],
                ai_estimate=d["ai"] if isinstance(d["ai"], dict) else {}, flex=flex.extract(form, FlexField.CLAIM),
                liable_party=d["liable_party"], liable_insurer=d["liable_insurer"], liable_insurer_email=d["liable_insurer_email"],
                their_policy_no=d["their_policy_no"], their_vehicle=d["their_vehicle"], their_claim_ref=d["their_claim_ref"])
        except services.ClaimError as e:
            form.add_error(None, str(e))
        else:
            for file in d["photos"]:
                ClaimDocument.objects.create(claim=claim, kind="PHOTO", file=file)
            for file in d["police_report"]:
                ClaimDocument.objects.create(claim=claim, kind="POLICE", file=file)
            msg = f"Claim {claim.claim_no} registered. Excess applicable: {claim.excess_applied} {claim.currency.code}."
            if claim.is_total_loss:
                msg += f" Total loss: depreciated market value {claim.depreciated_value} {claim.currency.code}."
            messages.success(request, msg)
            return redirect("claim_detail", pk=claim.pk)
    return render(request, "claims/claim_form.html", {"form": form, "currency_symbols": _cur_json(), "zones": ai.ZONES, "severity": ai.SEVERITY,
                                                      "ollama": form.ollama})


def _cur_json():
    return {c.code: {"symbol": c.symbol, "fallback": c.symbol_fallback or c.code, "decimals": c.decimals}
            for c in Currency.objects.filter(active=True)}


@login_required
def claim_detail(request, pk):
    claim = get_object_or_404(claim_scope(request.user).select_related("policy__customer", "policy__currency", "risk", "garage", "surveyor"), pk=pk)
    role = user_role(request.user)
    pos = services.financial_position(claim)
    from django.conf import settings

    ctx = {
        "c": claim, "cur": claim.policy.currency, "pos": pos, "is_handler": role in HANDLERS or role == "ADMIN",
        "can_manage": role in HANDLERS or role == "ADMIN",
        "locked": claim.locked, "can_edit": (role in HANDLERS or role == "ADMIN") and not claim.locked, "blockers": services.close_blockers(claim) if claim.status in ("APPROVED", "SETTLED") else [],
        "injuries": claim.injuries.select_related("hospital"), "legal_actions": claim.legal_actions.select_related("counsel"), "notices": claim.notices.all(),
        "treaty_recoveries": claim.treaty_recoveries.all(), "auto_lines": (claim.auto_estimate_detail or {}).get("lines", []),
        "deaths": claim.injuries.filter(severity="DEATH").count(), "n_injured": claim.injuries.count(),
        "pending": __import__("core.models", fromlist=["PendingAction"]).PendingAction.objects.filter(kind="claim", object_id=claim.pk, status="PENDING"),
        "history": __import__("core.models", fromlist=["ChangeLog"]).ChangeLog.objects.filter(ref=claim.claim_no).select_related("user", "confirmed_by")[:60],
        "close_preview": services.closing_preview(claim) if claim.status in ("APPROVED", "SETTLED") else None, "adjustments": claim.adjustments.select_related("user"),
        "payments": claim.payments.all(), "recoveries": claim.recoveries.all(), "estimates": claim.estimates.select_related("garage"),
        "documents": claim.documents.all(), "guarantees": claim.guarantees.prefetch_related("bills"),
        "ratings": claim.ratings.select_related("provider"), "sla_days": settings.REPAIR_SLA_DAYS,
        "interim_pct": settings.REPAIR_INTERIM_PCT, "manager_limit": settings.CLAIM_MANAGER_LIMIT,
        "forms": {
            "estimate": f.EstimateForm(), "reopen": f.ReopenForm(), "invoice": f.InvoiceForm(claim), "settle_est": f.SettleEstimateForm(claim),
            "excess": f.ExcessForm(initial={"amount": claim.excess_applied}), "legal_action": f.LegalActionForm(), "injury": f.InjuryForm(), "reserve": f.ReserveForm(initial={"reserve": claim.reserve}),
            "approve": f.ApproveForm(initial={"approved_cost": claim.approved_repair_cost or (claim.depreciated_value if claim.is_total_loss else claim.estimated_damage),
                                              "total_loss": claim.is_total_loss}),
            "pay": f.PayForm(initial={"payee": claim.garage.name if claim.garage else claim.policy.customer.name}),
            "repair": f.RepairForm(), "recovery": f.RecoveryForm(), "surveyor": f.SurveyorForm(initial={"surveyor": claim.surveyor}),
            "subro": f.SubrogationForm(initial={"liable_party": getattr(getattr(claim, "subrogation", None), "liable_party", ""),
                                                  "recovery_status": getattr(getattr(claim, "subrogation", None), "recovery_status", "DEMAND_SENT")}),
            "cat": f.CatFlagForm(initial={"cat_event": claim.cat_event}), "subro_recover": f.SubroRecoverForm(), "guarantee": f.GuaranteeForm(), "bill": f.MedicalBillForm(claim),
        },
        "subrogation": getattr(claim, "subrogation", None) if hasattr(claim, "subrogation") else None,
        "excess_parts": claim.excess_breakdown, "zones": ai.ZONES, "severity": ai.SEVERITY, "zone_data": claim.damage_zones, "zone_json": __import__("json").dumps(claim.damage_zones or {}), "flex_rows": flex.display(FlexField.CLAIM, claim.flex),
        "ai": claim.ai_estimate, "ollama": ai.list_models() if (role in HANDLERS or role == "ADMIN") else None,
        "flex_rows": flex.display(FlexField.CLAIM, claim.flex), "driver": claim.driver,
        "fsa_deadline": (claim.file_finalised_on and claim.file_finalised_on.fromordinal(claim.file_finalised_on.toordinal() + settings.REPAIR_SLA_DAYS)),
    }
    return render(request, "claims/claim_detail.html", ctx)


@role_required(*HANDLERS)
@require_POST
def claim_action(request, pk, action):
    from core import confirm as dual
    from . import actions

    claim = get_object_or_404(Claim, pk=pk)
    if action not in actions.ACTIONS:
        raise Http404
    P = request.POST
    err = actions.prepare(claim, action, P)
    if err:
        messages.error(request, err)
        return _back(request, claim)
    if dual.enabled():
        reason = P.get("reason", "") if action == "reopen" else ""
        pa = dual.submit(request.user, "claim", claim.pk, claim.claim_no, action, f"{claim.claim_no}: {actions.LABELS[action]}", {k: P.getlist(k) for k in P}, reason)
        messages.info(request, f"{pa.number} submitted: a second person must confirm this change before it is applied.")
        return _back(request, claim)
    try:
        from core import audit
        with audit.acting(request.user, reason=P.get("reason", "")):
            for lvl, text in actions.apply(claim, action, P, request.user):
                getattr(messages, lvl)(request, text)
    except services.ClaimError as e:
        messages.error(request, str(e))
    return _back(request, claim)


def _back(request, claim):
    return redirect(request.POST.get("next") or "claim_detail", **({} if request.POST.get("next") else {"pk": claim.pk}))


@login_required
def demand_letter(request, pk):
    claim = get_object_or_404(claim_scope(request.user), pk=pk)
    sub = getattr(claim, "subrogation", None)
    if not sub:
        raise Http404
    return render(request, "claims/demand_letter.html", {"c": claim, "sub": sub, "cur": claim.policy.currency, "today": date.today()})


@login_required
def guarantee_letter(request, pk):
    g = get_object_or_404(LetterOfGuarantee.objects.filter(claim__in=claim_scope(request.user)), pk=pk)
    return render(request, "claims/guarantee_letter.html", {"g": g, "c": g.claim, "cur": g.claim.policy.currency})


@role_required("CLAIMS_MANAGER", "CLAIMS_HANDLER", "UNDERWRITER", "PRODUCTION_MANAGER", "FINANCE")
def provider_list(request):
    ptype = request.GET.get("type", "GARAGE")
    providers = Provider.objects.filter(provider_type=ptype).order_by("-average_score", "name")
    form = f.ProviderForm(request.POST or None, initial={"provider_type": ptype})
    if request.method == "POST":
        if user_role(request.user) not in ("CLAIMS_MANAGER", "ADMIN"):
            raise PermissionDenied
        if form.is_valid():
            form.save()
            messages.success(request, "Provider added.")
            return redirect(f"/claims/providers/?type={form.cleaned_data['provider_type']}")
    return render(request, "claims/provider_list.html", {"providers": providers, "ptype": ptype, "types": Provider.TYPES, "form": form,
                                                           "can_edit": user_role(request.user) in ("CLAIMS_MANAGER", "ADMIN")})


# ---------------------------------------------------------------------------
# AI / Ollama
# ---------------------------------------------------------------------------
@role_required(*CAN_CLAIM, "UNDERWRITER")
def ollama_models(request):
    from django.http import JsonResponse
    return JsonResponse(ai.list_models())


@role_required(*CAN_CLAIM, "UNDERWRITER")
def risk_info(request, risk_id):
    """Drivers, vehicle type and the depreciated value for a policy risk (used by the claim form)."""
    from django.http import JsonResponse
    from motor.models import PolicyRisk

    risk = get_object_or_404(PolicyRisk.objects.filter(policy__in=scope(request.user, Policy.objects.all())), pk=risk_id)
    try:
        dol = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        dol = date.today()
    pol = risk.policy
    val = services.total_loss_value(risk, pol, dol)
    return JsonResponse({
        "vehicle_type": risk.vehicle_type, "make": risk.make, "model": risk.model, "year": risk.year, "sum_insured": str(risk.sum_insured),
        "currency": pol.currency.code, "depreciated_value": str(val), "depreciation_pct": str(pol.product.depreciation_pct),
        "drivers": [{"id": d.pk, "label": f"{d.name} ({d.licence_no}), age {d.age_on(dol)}", "age": d.age_on(dol)} for d in risk.drivers.all()],
        "excess": {"standard": str(pol.product.standard_excess)},
    })


@role_required(*CAN_CLAIM, "UNDERWRITER")
@require_POST
def claim_estimate(request):
    """POST JSON {risk, zones, loss_type, notes, model[, claim]} -> estimate. Saves it on the claim when ``claim`` is given."""
    import json
    from django.http import JsonResponse
    from motor.models import PolicyRisk

    try:
        body = json.loads(request.body.decode() or "{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "Bad request"}, status=400)
    risk = get_object_or_404(PolicyRisk.objects.filter(policy__in=scope(request.user, Policy.objects.all())), pk=body.get("risk") or 0)
    claim = None
    if body.get("claim"):
        claim = get_object_or_404(claim_scope(request.user), pk=body["claim"])
        if user_role(request.user) not in HANDLERS and user_role(request.user) != "ADMIN":
            raise PermissionDenied
    res = ai.estimate(risk=risk, zones=body.get("zones"), loss_type=body.get("loss_type") or "COLLISION", notes=body.get("notes", ""),
                      model=body.get("model", ""), claim_id=claim.pk if claim else None)
    if claim:
        claim.damage_zones, claim.ai_estimate = res["zones"], res
        claim.save(update_fields=["damage_zones", "ai_estimate"])
    return JsonResponse({"ok": True, **res})


@role_required("CLAIMS_MANAGER", "CLAIMS_HANDLER", "UNDERWRITER", "PRODUCTION_MANAGER", "FINANCE")
def ai_models(request):
    """List every installed Ollama model; the Claims Manager / Admin can set the default."""
    from core.models import Company

    company = Company.get()
    if request.method == "POST":
        if user_role(request.user) not in ("CLAIMS_MANAGER", "ADMIN"):
            raise PermissionDenied
        company.ollama_model = request.POST.get("model", "")
        company.save(update_fields=["ollama_model"])
        messages.success(request, f"Default AI model set to {company.ollama_model or 'none (statistical only)'}.")
        return redirect("ai_models")
    info = ai.list_models()
    n_hist = Claim.objects.filter(approved_repair_cost__gt=0, is_total_loss=False).exclude(damage_zones={}).count()
    return render(request, "claims/ai_models.html", {"info": info, "default": company.ollama_model, "n_hist": n_hist,
                                                     "can_set": user_role(request.user) in ("CLAIMS_MANAGER", "ADMIN")})


def _policy_card(p):
    live = p.status in ("ACTIVE", "PENDING_FAC") and p.end_date >= date.today()
    return {"id": p.pk, "policy_no": p.policy_no, "customer": p.customer.name, "product": p.product.name, "status": p.get_status_display(), "in_force": live,
            "period": f"{p.start_date:%d %b %Y} - {(p.cancelled_on or p.end_date):%d %b %Y}", "currency": p.currency.code}


@role_required(*CAN_CLAIM, "UNDERWRITER")
def policy_search(request):
    """AJAX: find the policy a claim is for by policy no., insured (name / ID / mobile), plate or chassis."""
    from django.db.models import Q
    from django.http import JsonResponse

    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    qs = scope(request.user, Policy.objects.exclude(status="PENDING_FAC").select_related("customer", "product", "currency"))
    for tok in q.split():
        qs = qs.filter(Q(policy_no__icontains=tok) | Q(customer__name__icontains=tok) | Q(customer__civil_id__icontains=tok) | Q(customer__cr_number__icontains=tok)
                       | Q(customer__phone__icontains=tok) | Q(risks__plate__icontains=tok) | Q(risks__chassis_no__icontains=tok) | Q(risks__make__icontains=tok)
                       | Q(risks__model__icontains=tok))
    out = []
    for p in qs.distinct().order_by("-start_date")[:10]:
        c = _policy_card(p)
        c["vehicles"] = ", ".join(f"{r.make} {r.model} {r.plate or r.chassis_no[-6:]}" for r in p.risks.all()[:3])
        out.append(c)
    return JsonResponse({"results": out})


@role_required(*CAN_CLAIM, "UNDERWRITER")
def policy_vehicles(request, policy_id):
    """AJAX: the vehicle(s) insured under a policy, with their drivers, so the claim form can fill itself in."""
    from django.http import JsonResponse

    p = get_object_or_404(scope(request.user, Policy.objects.exclude(status="PENDING_FAC").select_related("customer", "product", "currency")), pk=policy_id)
    risks = []
    for r in p.risks.filter(active=True).prefetch_related("drivers"):
        risks.append({"id": r.pk, "label": f"{r.make} {r.model} {r.year} \u00b7 {r.plate or r.chassis_no}", "make": r.make, "model": r.model, "year": r.year,
                      "plate": r.plate, "chassis_no": r.chassis_no, "colour": r.color, "type": r.get_vehicle_type_display(), "usage": r.get_usage_display(),
                      "sum_insured": str(r.sum_insured), "vehicle_id": r.vehicle_id,
                      "drivers": [{"id": d.pk, "label": f"{d.name} ({d.licence_no}), age {d.age}"} for d in r.drivers.all()]})
    return JsonResponse({"policy": _policy_card(p), "risks": risks})


@role_required(*HANDLERS, "UNDERWRITER", "PRODUCTION_MANAGER", "FINANCE")
def legal_list(request):
    """Claims in legal process with their latest action and next date."""
    qs = claim_scope(request.user).filter(is_legal=True).select_related("policy__customer", "policy__currency").prefetch_related("legal_actions")
    rows = []
    for c in qs:
        acts = list(c.legal_actions.all())
        last = acts[-1] if acts else None
        nxt = min((a.next_date for a in acts if a.next_date and a.next_date >= date.today()), default=None)
        rows.append({"c": c, "last": last, "next": nxt, "cost": sum((a.legal_cost for a in acts), Decimal("0")), "n": len(acts)})
    return render(request, "claims/legal_list.html", {"rows": rows})


@login_required
def notice_view(request, pk):
    from .models import SubroNotice
    n = get_object_or_404(SubroNotice.objects.filter(claim__in=claim_scope(request.user)), pk=pk)
    return render(request, "claims/notice.html", {"n": n, "c": n.claim})


@role_required("CLAIMS_MANAGER", "CLAIMS_HANDLER", "UNDERWRITER", "PRODUCTION_MANAGER", "FINANCE")
def cat_list(request):
    from django.db.models import Count, Sum
    events = CatEvent.objects.annotate(n=Count("claims"), gross=Sum("claims__net_payable"), paid=Sum("claims__payments__amount")).order_by("-date_from")
    return render(request, "claims/cat_list.html", {"events": events})


@role_required("CLAIMS_MANAGER", "CLAIMS_HANDLER", "UNDERWRITER", "PRODUCTION_MANAGER", "FINANCE")
def cat_detail(request, pk):
    from reinsurance import xol
    from reinsurance.models import Treaty
    ev = get_object_or_404(CatEvent, pk=pk)
    claims = ev.claims.select_related("policy__currency", "policy__customer", "risk")
    treaties = Treaty.objects.filter(business="MOTOR", treaty_type="XOL", xol_kind="CAT", superseded_by__isnull=True, effective_from__lte=ev.date_from, effective_to__gte=ev.date_from)
    rows = [(t, xol.cat_position(t, ev)) for t in treaties]
    total = sum((services.incurred_base(c) for c in claims), 0)
    retained = sum((services.retained_loss(c) for c in claims), 0)
    from pnc import services as ps
    pnc_claims = list(ev.pnc_claims.select_related("policy__currency", "policy__customer", "risk"))
    pnc_rows = [(c, ps.incurred_base(c), ps.retained_loss(c)) for c in pnc_claims]
    pt = Treaty.objects.filter(business="PNC", treaty_type="XOL", xol_kind="CAT", superseded_by__isnull=True, effective_from__lte=ev.date_from, effective_to__gte=ev.date_from)
    rows += [(t, xol.cat_position(t, ev)) for t in pt]
    total += sum((r[1] for r in pnc_rows), 0)
    retained += sum((r[2] for r in pnc_rows), 0)
    return render(request, "claims/cat_detail.html", {"ev": ev, "claims": claims, "positions": rows, "total": total, "retained": retained, "pnc_rows": pnc_rows})

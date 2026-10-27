"""Vehicle profile, vehicle-level search and ownership transfer."""
from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from claims.models import Claim
from core import clock
from core.models import Currency, user_role
from core.permissions import PRODUCERS, role_required

from . import lossratio, vehicles
from .models import Customer, PolicyRisk, Vehicle

ROLES = ("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_HANDLER", "CLAIMS_MANAGER")
TRANSFER_ROLES = ("PRODUCTION_MANAGER", "UNDERWRITER", "AGENT", "BROKER")


def _visible(user, qs):
    if user_role(user) in PRODUCERS:
        return qs.filter(Q(risks_on_policies__policy__producer=user) | Q(customer__policies__producer=user)).distinct()
    return qs


@role_required(*ROLES)
def vehicle_list(request):
    q = request.GET.get("q", "").strip()
    found = vehicles.search(q, limit=200)
    ids = [v.pk for v in found]
    rows = _visible(request.user, Vehicle.objects.filter(pk__in=ids).select_related("customer").prefetch_related("risks_on_policies__policy"))
    order = {pk: i for i, pk in enumerate(ids)}
    rows = sorted(rows, key=lambda v: order.get(v.pk, 0))
    out = []
    for v in rows:
        pols = {r.policy for r in v.risks_on_policies.all()}
        live = [p for p in pols if p.status in ("ACTIVE", "PENDING_FAC") and p.end_date >= date.today()]
        out.append({"v": v, "n_policies": len(pols), "live": live[0] if live else None})
    return render(request, "motor/vehicle_list.html", {"rows": out, "q": q})


@role_required(*ROLES)
def vehicle_detail(request, pk):
    v = get_object_or_404(_visible(request.user, Vehicle.objects.select_related("customer")), pk=pk)
    from .services import vehicle_value
    risks = PolicyRisk.objects.filter(vehicle=v).select_related("policy__customer", "policy__product", "policy__currency").order_by("-policy__start_date")
    claims = Claim.objects.filter(risk__vehicle=v).select_related("policy").order_by("-date_of_loss")
    base = Currency.base()
    val = None
    live = vehicles.in_force_policies(v).first()
    last = risks.first()
    if last:
        val, _n = vehicle_value(v, date.today(), last.policy.product, base, 1, fallback_si=last.sum_insured * last.policy.fx_rate, fallback_date=last.policy.start_date)
    lr = lossratio.compute(request.user, date(2000, 1, 1), clock.today(), "vehicle", vehicle=v.pk)
    return render(request, "motor/vehicle_detail.html", {
        "v": v, "ownerships": v.ownerships.select_related("customer"), "risks": risks, "claims": claims, "value": val, "live": live,
        "lr": lr[0] if lr else None, "can_transfer": user_role(request.user) in TRANSFER_ROLES + ("ADMIN",), "can_finance": user_role(request.user) in ("ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER", "AGENT", "BROKER", "FINANCE", "CLAIMS_HANDLER", "CLAIMS_MANAGER"), "today": clock.today(),
    })


@role_required(*TRANSFER_ROLES)
@require_POST
def vehicle_transfer(request, pk):
    v = get_object_or_404(_visible(request.user, Vehicle.objects.all()), pk=pk)
    new = Customer.objects.filter(pk=request.POST.get("customer") or 0).first()
    try:
        eff = date.fromisoformat(request.POST.get("effective", ""))
    except ValueError:
        eff = clock.today()
    if not new:
        messages.error(request, "Search and select the new owner.")
        return redirect("vehicle_detail", pk=pk)
    try:
        vehicles.transfer_directly(v, new, eff, request.user, request.POST.get("note", "")[:200])
    except vehicles.TransferError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"{v.make} {v.model} now belongs to {new.name}. Its history stays on the same vehicle profile.")
    return redirect("vehicle_detail", pk=pk)


@role_required(*ROLES)
def vehicle_search_api(request):
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    return JsonResponse({"results": [{"id": v.pk, "label": str(v), "chassis_no": v.chassis_no, "owner": v.customer.name} for v in vehicles.search(q, limit=10)]})

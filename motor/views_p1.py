import json
from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.models import user_role
from core.permissions import role_required, scope

from . import catalogue, credit, phase1
from .models import CreditApproval, Customer, FinanceInterest, Policy, PostDatedCheque, Proposal, Vehicle, VehicleMake, VehicleModel

STAFF = ("PRODUCTION_MANAGER", "UNDERWRITER", "AGENT", "BROKER", "FINANCE", "CREDIT_CONTROL", "CLAIMS_HANDLER", "CLAIMS_MANAGER")


@login_required
def vehicle_catalogue(request):
    return JsonResponse(catalogue.as_dict())


@login_required
@require_POST
def vehicle_catalogue_add(request):
    if user_role(request.user) == "CUSTOMER":
        raise PermissionDenied
    make = (request.POST.get("make") or "").strip()[:40]
    model = (request.POST.get("model") or "").strip()[:40]
    if not make:
        return JsonResponse({"error": "Enter the make."}, status=400)
    mk = VehicleMake.objects.filter(name__iexact=make).first() or VehicleMake.objects.create(name=make)
    if model:
        VehicleModel.objects.get_or_create(make=mk, name=model) if not VehicleModel.objects.filter(make=mk, name__iexact=model).exists() else None
    return JsonResponse({"make": mk.name, "model": model})


@role_required("PRODUCTION_MANAGER", "UNDERWRITER")
@require_POST
def assign_producer(request, pk):
    c = get_object_or_404(Customer, pk=pk)
    u = User.objects.filter(pk=request.POST.get("producer")).first()
    if not u:
        messages.error(request, "Choose a broker or agent.")
    else:
        phase1.assign_producer(c, u, request.user, request.POST.get("note", ""))
        messages.success(request, f"{c.name} is now serviced by {u.get_username()}. The earlier assignment stays in the history.")
    return redirect("customer_detail", pk=pk)


@role_required("CREDIT_CONTROL", "FINANCE")
@require_POST
def credit_refer(request, pk):
    ca = get_object_or_404(CreditApproval, pk=pk)
    try:
        phase1.refer_back(ca, request.user, request.POST.get("note", ""))
        messages.success(request, f"{ca.uid} referred back to {ca.requested_by.get_username()}.")
    except credit.CreditError as e:
        messages.error(request, str(e))
    return redirect("credit_list")


def _proposal(request, pk):
    return get_object_or_404(scope(request.user, Proposal.objects.all()), pk=pk)


@role_required(*STAFF)
@require_POST
def pdc_add(request, pk):
    p = _proposal(request, pk)
    P = request.POST
    try:
        phase1.add_pdc(p, request.user, P.get("cheque_no", "").strip(), P.get("bank", "").strip(), P.get("amount") or 0, date.fromisoformat(P.get("cheque_date", "")),
                       int(P["installment_no"]) if P.get("installment_no") else None, note=P.get("note", ""))
        messages.success(request, "Post-dated cheque recorded.")
    except (phase1.Phase1Error, ValueError, ArithmeticError) as e:
        messages.error(request, str(e) if isinstance(e, phase1.Phase1Error) else "Enter a valid cheque date and amount.")
    return redirect("quote_detail", pk=pk)


@role_required(*STAFF)
@require_POST
def pdc_status(request, pk, status):
    pdc = get_object_or_404(PostDatedCheque, pk=pk)
    _proposal(request, pdc.proposal_id)
    try:
        phase1.set_pdc_status(pdc, status.upper(), request.user)
        messages.success(request, f"Cheque {pdc.cheque_no}: {pdc.get_status_display().lower()}.")
    except phase1.Phase1Error as e:
        messages.error(request, str(e))
    return redirect(request.POST.get("next") or "quote_detail", pk=pdc.proposal_id) if not request.POST.get("next") else redirect(request.POST["next"])


@role_required(*STAFF)
@require_POST
def finance_add(request, pk):
    v = get_object_or_404(Vehicle, pk=pk)
    P = request.POST
    try:
        pol = Policy.objects.filter(pk=P.get("policy")).first() if P.get("policy") else None
        phase1.add_finance(v, request.user, P.get("financier", ""), P.get("contract_no", ""), P.get("financed_amount") or 0, pol, note=P.get("note", ""))
        messages.success(request, "Finance details recorded.")
    except (phase1.Phase1Error, ArithmeticError) as e:
        messages.error(request, str(e) if isinstance(e, phase1.Phase1Error) else "Enter a valid amount.")
    return redirect(P.get("next") or "vehicle_detail", pk=pk) if not P.get("next") else redirect(P["next"])


@role_required(*STAFF)
@require_POST
def finance_release(request, pk):
    fi = get_object_or_404(FinanceInterest, pk=pk)
    phase1.release_finance(fi)
    messages.success(request, "Finance interest released.")
    return redirect(request.POST.get("next") or "vehicle_detail", pk=fi.vehicle_id) if not request.POST.get("next") else redirect(request.POST["next"])

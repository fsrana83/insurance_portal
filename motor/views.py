import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import flex
from core.models import Currency, FlexField, user_role
from core.permissions import CAN_QUOTE, role_required, scope

from . import services
from .forms import (
    DecisionForm, EndorsementForm, FleetUploadForm, QuoteForm, RiskFormSet, fleet_template_csv, parse_fleet_file,
    CustomerForm,
)
from .models import (
    Cover, Customer, Driver, Endorsement, Policy, Product, ProductCover, Proposal, ProposalRisk, Vehicle,
)
from .pdf import policy_schedule_pdf
from .dashboards import dashboard_context

D = Decimal


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@login_required
def dashboard(request):
    ctx = dashboard_context(request.user)
    return render(request, "motor/dashboard.html", ctx)


# ---------------------------------------------------------------------------
# Quote helpers
# ---------------------------------------------------------------------------
def _allow_waiver(user):
    return user_role(user) in ("UNDERWRITER", "ADMIN")


def _risk_initial_from_proposal(proposal):
    rows = []
    for r in proposal.risks.select_related("vehicle"):
        v = r.vehicle
        rows.append({
            "vehicle_id": v.id, "make": v.make, "model": v.model, "year": v.year, "plate_code": v.plate_code,
            "plate_number": v.plate_number, "chassis_no": v.chassis_no, "engine_no": v.engine_no, "color": v.color,
            "vehicle_type": v.vehicle_type, "usage": v.usage, "fuel_type": v.fuel_type, "cc": v.cc, "seats": v.seats,
            "sum_insured": r.sum_insured, "riders": list(r.riders.values_list("id", flat=True)),
            "waived": list(r.waived_riders.values_list("id", flat=True)), "waiver_reason": r.waiver_reason,
            "si_note": r.si_note, "flex": v.flex,
            "drivers_json": json.dumps([{"name": d.name, "licence_no": d.licence_no, "date_of_birth": d.date_of_birth.isoformat(),
                                         "licence_issue_date": d.licence_issue_date.isoformat() if d.licence_issue_date else "",
                                         "nationality": d.nationality, "relationship": d.relationship} for d in r.drivers.all()]),
        })
    return rows


def _quote_initial(p):
    c = p.customer
    return {"customer": c, "product": p.product, "currency": p.currency, "start_date": p.start_date, "end_date": p.end_date,
            "ncd_years": p.ncd_years, "discount_pct": p.discount_pct, "notes": p.notes,
            "voucher_code": p.voucher.code if p.voucher_id else "", "flex": p.flex}


@transaction.atomic
def _save_quote(form, formset, user, proposal=None):
    c = form.cleaned_data
    customer = form.get_customer()
    cur = c["currency"]
    if proposal is None:
        proposal = Proposal(number=services.next_quote_number(), producer=user)
    if c.get("producer") and user_role(user) in ("ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER"):
        proposal.producer = c["producer"]                    # staff choose the broker / agent the business is placed through
    proposal.product, proposal.customer, proposal.currency = c["product"], customer, cur
    proposal.fx_rate = cur.rate_to_base
    proposal.start_date, proposal.end_date = c["start_date"], c["end_date"]
    proposal.valid_until = date.today() + timedelta(days=c["product"].quote_validity_days)
    proposal.ncd_years, proposal.discount_pct = c["ncd_years"], c["discount_pct"]
    proposal.voucher = c.get("voucher")
    proposal.flex = flex.extract(form, FlexField.POLICY)
    proposal.notes = c.get("notes", "")
    if proposal.status in (Proposal.REJECTED, Proposal.PROPOSAL, Proposal.EXPIRED):
        proposal.status = Proposal.QUOTE
    proposal.save()
    proposal.risks.all().delete()
    for f in formset.forms:
        d = f.cleaned_data
        if not d or d.get("DELETE"):
            continue
        vehicle = None
        if d.get("vehicle_id"):
            vehicle = Vehicle.objects.filter(pk=d["vehicle_id"]).first()
        if vehicle is None:
            vehicle = Vehicle.objects.filter(chassis_no=d["chassis_no"]).first() or Vehicle(customer=customer)
        if vehicle.pk is None:
            vehicle.customer = customer      # existing vehicles keep their owner until a policy is issued to the new one
        for fld in ("make", "model", "year", "plate_code", "plate_number", "chassis_no", "engine_no", "color",
                    "vehicle_type", "usage", "fuel_type", "cc", "seats"):
            setattr(vehicle, fld, d[fld])
        vehicle.flex = {**(vehicle.flex or {}), **flex.extract(f, FlexField.VEHICLE)}
        vehicle.save()
        risk = ProposalRisk.objects.create(proposal=proposal, vehicle=vehicle, si_note=d.get("si_note", ""),
                                           sum_insured=d.get("sum_insured") or D("0"), waiver_reason=d.get("waiver_reason", ""))
        for dr in d.get("drivers_json") or []:
            Driver.objects.create(proposal_risk=risk, name=dr["name"], licence_no=dr["licence_no"], date_of_birth=dr["date_of_birth"],
                                  licence_issue_date=dr.get("licence_issue_date"), nationality=dr.get("nationality", ""),
                                  relationship=dr.get("relationship") or "OTHER")
        risk.riders.set(d.get("riders") or [])
        if d.get("waived"):
            risk.waived_riders.set(d["waived"])
    services.reprice_proposal(proposal)
    return proposal


def _formset(request, product=None, initial=None, data=None):
    fs = RiskFormSet(data, initial=initial, prefix="risks", form_kwargs={"allow_waiver": _allow_waiver(request.user)})
    fs.product = product
    return fs


def _quote_form_view(request, proposal=None):
    user = request.user
    session_rows = request.session.pop("fleet_rows", None) if proposal is None else None
    if request.method == "POST":
        form = QuoteForm(request.POST)
        product = None
        if form.is_valid() or form.cleaned_data.get("product"):
            product = form.cleaned_data.get("product")
        formset = _formset(request, product, data=request.POST)
        if form.is_valid() and formset.is_valid():
            try:
                proposal = _save_quote(form, formset, user, proposal)
            except ValueError as e:  # no rate configured for the combination
                messages.error(request, str(e))
            else:
                messages.success(request, f"Quote {proposal.number} saved.")
                return redirect("quote_detail", pk=proposal.pk)
    else:
        if proposal:
            form = QuoteForm(initial=_quote_initial(proposal))
            formset = _formset(request, proposal.product, initial=_risk_initial_from_proposal(proposal))
        else:
            cust = Customer.objects.filter(pk=request.GET.get("customer") or 0).first()
            form = QuoteForm(initial={"customer": cust} if cust else None)
            initial = session_rows or [{}]
            formset = _formset(request, None, initial=initial)
    return render(request, "motor/quote_form.html", {
        "form": form, "formset": formset, "proposal": proposal, "upload_form": FleetUploadForm(),
        "currency_symbols": _currency_json(), "product_meta": _product_meta(),
        "fleet_errors": request.session.pop("fleet_errors", None),
    })


def _currency_json():
    return {c.code: {"symbol": c.symbol, "fallback": c.symbol_fallback or c.code, "decimals": c.decimals,
                     "rate": str(c.rate_to_base)} for c in Currency.objects.filter(active=True)}


def _product_meta():
    meta = {}
    for p in Product.objects.filter(active=True):
        meta[str(p.pk)] = {"includes_od": p.includes_od, "regulated": p.is_tariff_regulated, "name": p.name}
    return meta


@role_required(*CAN_QUOTE)
def quote_create(request):
    return _quote_form_view(request)


@role_required(*CAN_QUOTE)
def quote_edit(request, pk):
    proposal = get_object_or_404(scope(request.user, Proposal.objects.all()), pk=pk)
    if not proposal.is_editable:
        messages.error(request, "This proposal can no longer be edited.")
        return redirect("quote_detail", pk=pk)
    return _quote_form_view(request, proposal)


# ---------------------------------------------------------------------------
# Live premium preview (AJAX)
# ---------------------------------------------------------------------------
def _num(v, default="0"):
    try:
        return D(str(v).strip() or default)
    except InvalidOperation:
        return D(default)


@role_required(*CAN_QUOTE)
@require_POST
def quote_preview(request):
    P = request.POST
    try:
        product = Product.objects.get(pk=P.get("product"))
        cur = Currency.objects.get(code=P.get("currency"))
        start = date.fromisoformat(P.get("start_date"))
        end = date.fromisoformat(P.get("end_date"))
    except (Product.DoesNotExist, Currency.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Choose a product, currency and policy period."})
    if end <= start:
        return JsonResponse({"ok": False, "error": "Expiry must be after inception."})
    total = int(P.get("risks-TOTAL_FORMS", 0) or 0)
    inputs, labels = [], []
    for i in range(total):
        g = lambda k: P.get(f"risks-{i}-{k}", "")
        if g("DELETE"):
            continue
        try:
            inputs.append(services.RiskInput(g("vehicle_type") or "SALOON", g("usage") or "PRIVATE", int(g("cc") or 1600),
                                             int(g("seats") or 5), int(g("year") or date.today().year), _num(g("sum_insured")),
                                             [int(x) for x in P.getlist(f"risks-{i}-riders")],
                                             [int(x) for x in P.getlist(f"risks-{i}-waived")]))
        except ValueError:
            return JsonResponse({"ok": False, "error": f"Vehicle {i + 1}: check the numeric fields."})
        labels.append(f"{g('make')} {g('model')}".strip() or f"Vehicle {len(inputs)}")
    if not inputs:
        return JsonResponse({"ok": False, "error": "Add a vehicle."})
    try:
        customer = Customer.objects.filter(pk=P.get("customer") or 0).first()
        voucher, v_err = services.validate_voucher(P.get("voucher_code"), product, customer)
        res = services.price_quote(product, cur, cur.rate_to_base, start, end, inputs, ncd_years=int(P.get("ncd_years") or 0),
                                   extra_discount_pct=_num(P.get("discount_pct")), voucher=voucher)
        v_err = v_err or res["voucher_error"]
    except ValueError as e:
        return JsonResponse({"ok": False, "error": str(e)})
    f = lambda x: f"{cur.quantize(x):,.{cur.decimals}f}"
    lines = []
    for lab, l in zip(labels, res["lines"]):
        lines.append({"label": lab, "od": f(l["od"]), "tp": f(l["tp"]), "riders": f(l["riders"]), "discount": f(l["discount"]),
                      "net": f(l["net"]), "disc_lines": [{"label": d["label"], "pct": str(d["pct"])} for d in l["disc_lines"]],
                      "rider_lines": [{"name": r["name"], "amount": f(r["amount"]), "mandatory": r["mandatory"]} for r in l["rider_lines"]]})
    return JsonResponse({"ok": True, "currency": cur.code, "includes_od": product.includes_od, "lines": lines,
                         "net": f(res["net"]), "fees": f(res["fees"]), "tax": f(res["tax"]), "total": f(res["total"]),
                         "vat_pct": str(settings_vat()), "regulated": product.is_tariff_regulated,
                         "voucher": {"code": voucher.code, "label": voucher.label, "applied": f(res["voucher_discount"]),
                                     "error": v_err} if voucher and not v_err else ({"code": (P.get("voucher_code") or "").upper(), "error": v_err} if v_err else None)})


def settings_vat():
    from django.conf import settings

    return (settings.VAT_RATE * 100).normalize()


# ---------------------------------------------------------------------------
# Fleet upload
# ---------------------------------------------------------------------------
@role_required(*CAN_QUOTE)
@require_POST
def fleet_upload(request):
    form = FleetUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "; ".join(e for errs in form.errors.values() for e in errs))
        return redirect("quote_create")
    try:
        rows, errors = parse_fleet_file(form.cleaned_data["file"])
    except Exception as exc:  # corrupt files
        messages.error(request, f"Could not read the file: {exc}")
        return redirect("quote_create")
    request.session["fleet_rows"] = rows
    request.session["fleet_errors"] = errors
    if rows:
        messages.success(request, f"{len(rows)} vehicles loaded from the file." + (f" {len(errors)} row issue(s) below." if errors else ""))
    else:
        messages.error(request, "No valid vehicle rows were found.")
    return redirect("quote_create")


@login_required
def fleet_template(request):
    resp = HttpResponse(fleet_template_csv(), content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="fleet_vehicles_template.csv"'
    return resp


# ---------------------------------------------------------------------------
# Quotes & proposals
# ---------------------------------------------------------------------------
@login_required
def quote_list(request):
    qs = scope(request.user, Proposal.objects.select_related("customer", "product", "currency", "producer"))
    status = request.GET.get("status")
    q = request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(customer__name__icontains=q) | Q(risks__vehicle__chassis_no__icontains=q)).distinct()
    return render(request, "motor/quote_list.html", {"proposals": qs[:200], "statuses": Proposal.STATUSES, "status": status, "q": q})


@login_required
def quote_detail(request, pk):
    p = get_object_or_404(scope(request.user, Proposal.objects.select_related("customer", "product", "currency", "producer")), pk=pk)
    risks = list(p.risks.select_related("vehicle"))
    for r in risks:
        r.disc_lines = r.breakdown.get("disc_lines", [])
        r.rider_lines = r.breakdown.get("rider_lines", [])
    return render(request, "motor/quote_detail.html", {
        "p": p, "risks": risks, "can_decide": services.can_decide(request.user, p) if p.status == Proposal.PENDING else False,
        "decision_form": DecisionForm(), "credit": getattr(p, "credit", None), "max_installments": 12,
        "can_issue": user_role(request.user) in CAN_QUOTE,
    })


@role_required(*CAN_QUOTE)
@require_POST
def quote_submit(request, pk):
    p = get_object_or_404(scope(request.user, Proposal.objects.all()), pk=pk)
    if p.status not in (Proposal.QUOTE, Proposal.PROPOSAL, Proposal.REJECTED):
        messages.error(request, "This quote cannot be converted.")
        return redirect("quote_detail", pk=pk)
    try:
        services.reprice_proposal(p)  # re-rate against the current tariff
        services.convert_to_proposal(p)
    except services.ValidationFailed as e:
        for m in e.args[0]:
            messages.error(request, m)
    except ValueError as e:
        messages.error(request, str(e))
    else:
        if p.status == Proposal.PENDING:
            messages.warning(request, "Proposal sent for approval: " + "; ".join(p.approval_reasons))
        else:
            messages.success(request, "Proposal validated and approved within your authority. It can now be issued.")
    return redirect("quote_detail", pk=pk)


@role_required("PRODUCTION_MANAGER", "UNDERWRITER")
def approval_queue(request):
    pending = Proposal.objects.filter(status=Proposal.PENDING).select_related("customer", "currency", "product", "producer")
    rows = [(p, services.can_decide(request.user, p)) for p in pending]
    return render(request, "motor/approval_queue.html", {"rows": rows})


@role_required("PRODUCTION_MANAGER", "UNDERWRITER")
@require_POST
def decide(request, pk, action):
    p = get_object_or_404(Proposal, pk=pk, status=Proposal.PENDING)
    if not services.can_decide(request.user, p):
        raise PermissionDenied("Outside your approval authority — escalate to an Underwriter.")
    form = DecisionForm(request.POST)
    note = form.data.get("note", "")
    if action == "reject" and not note:
        messages.error(request, "Give a reason when rejecting.")
        return redirect("quote_detail", pk=pk)
    p.status = Proposal.APPROVED if action == "approve" else Proposal.REJECTED
    p.decision_by, p.decision_note = request.user, note[:300]
    p.save()
    messages.success(request, f"Proposal {p.number} {p.get_status_display().lower()}.")
    return redirect("approval_queue")


@role_required(*CAN_QUOTE)
@require_POST
def issue(request, pk):
    from . import credit
    p = get_object_or_404(scope(request.user, Proposal.objects.all()), pk=pk)
    try:
        policy = credit.issue_with_terms(p, request.user, request.POST.get("basis", ""), request.POST.get("method", ""), request.POST.get("reference", "").strip())
    except credit.CreditError as e:
        messages.error(request, str(e))
        return redirect("quote_detail", pk=pk)
    except services.ValidationFailed as e:
        for m in e.args[0]:
            messages.error(request, m)
        return redirect("quote_detail", pk=pk)
    _email_policy(policy)
    if policy.status == Policy.PENDING_FAC:
        messages.warning(request, f"Policy {policy.policy_no} issued but held for facultative placement — risk exceeds treaty capacity.")
    else:
        messages.success(request, f"Policy {policy.policy_no} issued. Schedule emailed to the customer.")
    return redirect("policy_detail", pk=policy.pk)


@role_required(*CAN_QUOTE)
@require_POST
def credit_request(request, pk):
    from . import credit
    p = get_object_or_404(scope(request.user, Proposal.objects.all()), pk=pk)
    try:
        credit.request_credit(p, request.user, request.POST.get("installments", "3"), request.POST.get("advance") or 0, request.POST.get("note", ""))
        messages.success(request, "Credit requested: Credit Control must approve it before the policy can be issued.")
    except (credit.CreditError, ValueError, ArithmeticError) as e:
        messages.error(request, str(e) if isinstance(e, credit.CreditError) else "Enter valid instalments and advance.")
    return redirect("quote_detail", pk=pk)


@role_required("CREDIT_CONTROL", "FINANCE")
def credit_list(request):
    from .models import CreditApproval
    rows = CreditApproval.objects.select_related("proposal__customer", "proposal__currency", "requested_by", "decided_by")
    return render(request, "motor/credit_list.html", {"pending": rows.filter(status="PENDING"), "decided": rows.exclude(status="PENDING")[:60]})


@role_required("CREDIT_CONTROL", "FINANCE")
@require_POST
def credit_decide(request, pk, action):
    from . import credit
    from .models import CreditApproval
    ca = get_object_or_404(CreditApproval, pk=pk)
    try:
        credit.decide(ca, request.user, action == "approve", request.POST.get("note", ""))
        messages.success(request, f"Credit {ca.get_status_display().lower()} for {ca.proposal.number}.")
    except credit.CreditError as e:
        messages.error(request, str(e))
    return redirect("credit_list")


def _email_policy(policy):
    from django.core.mail import EmailMessage
    from .models import PolicyDocument
    from core.templatetags.money import money_text

    c = policy.customer
    if not c.email:
        return
    body = (f"Dear {c.name},\n\nYour motor policy {policy.policy_no} is attached.\n"
            f"Period: {policy.start_date:%d %b %Y} – {policy.end_date:%d %b %Y}\nTotal paid: {money_text(policy.total_payable, policy.currency)}\n\n"
            "Please keep the attached schedule in your vehicle.")
    msg = EmailMessage(f"Motor policy {policy.policy_no}", body, to=[c.email])
    msg.attach(f"{policy.policy_no.replace('/', '-')}.pdf", policy_schedule_pdf(policy), "application/pdf")
    msg.send(fail_silently=True)
    PolicyDocument.objects.create(policy=policy, sent_to=c.email)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------
@login_required
def policy_list(request):
    qs = scope(request.user, Policy.objects.select_related("customer", "product", "currency", "producer"))
    status, q = request.GET.get("status"), request.GET.get("q", "").strip()
    if request.GET.get("expiring"):
        qs = qs.filter(status=Policy.ACTIVE, end_date__lte=date.today() + timedelta(days=45), end_date__gte=date.today())
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(policy_no__icontains=q) | Q(customer__name__icontains=q) | Q(risks__chassis_no__icontains=q) | Q(risks__plate__icontains=q)).distinct()
    return render(request, "motor/policy_list.html", {"policies": qs[:200], "statuses": Policy.STATUSES, "status": status, "q": q})


@login_required
def policy_detail(request, pk):
    p = get_object_or_404(scope(request.user, Policy.objects.select_related("customer", "product", "currency", "producer")), pk=pk)
    return render(request, "motor/policy_detail.html", {
        "p": p, "risks": p.risks.prefetch_related("riders"), "endorsements": p.endorsements.select_related("risk"),
        "cessions": p.cessions.select_related("treaty", "risk"), "commissions": p.commissions.select_related("producer"),
        "payments": p.payments.select_related("created_by"), "fin_documents": p.fin_documents.all(),
        "paid": p.paid_amount, "balance": max(p.balance_due, 0), "pay_forms": _pay_forms(p),
        "can_collect": user_role(request.user) in ("ADMIN", "PRODUCTION_MANAGER", "FINANCE", "CREDIT_CONTROL", "AGENT", "BROKER", "UNDERWRITER"),
        "drivers": [d for r in p.risks.all() for d in r.drivers.all()],
        "docs": __import__("motor.sharing", fromlist=["x"]).available_documents(p),
        "flex_rows": flex.display(FlexField.POLICY, p.flex),
        "claims": p.claims.all(), "can_endorse": user_role(request.user) in ("ADMIN", "AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER", "CUSTOMER"),
        "is_staff": user_role(request.user) != "CUSTOMER",
        "schedule": __import__("motor.credit", fromlist=["x"]).schedule_status(p),
    })


def _pay_forms(p):
    from payments.forms import ReceiptForm, RequestForm
    return {"receipt": ReceiptForm(initial={"amount": max(p.balance_due, 0)}, currency=p.currency),
            "request": RequestForm(initial={"amount": max(p.balance_due, 0)}, currency=p.currency)}


@login_required
def policy_pdf(request, pk):
    p = get_object_or_404(scope(request.user, Policy.objects.all()), pk=pk)
    resp = HttpResponse(policy_schedule_pdf(p), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{p.policy_no.replace("/", "-")}.pdf"'
    return resp


@login_required
def endorse(request, pk):
    p = get_object_or_404(scope(request.user, Policy.objects.all()), pk=pk)
    role = user_role(request.user)
    if role == "CUSTOMER":
        # Customers can only request endorsements: they are applied by staff.
        messages.info(request, "Endorsement requests from customers are reviewed by your agent.")
    form = EndorsementForm(p, request.POST or None)
    quote = None
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        kw = form.service_kwargs()
        try:
            quote = services.quote_endorsement(p, d["endorsement_type"], d["effective_date"], d.get("risk"), **kw)
            if "confirm" in request.POST:
                if role == "CUSTOMER":
                    raise PermissionDenied
                from core import confirm as dual
                if dual.enabled():
                    pa = dual.submit(request.user, "policy", p.pk, p.policy_no, "endorse", f"{p.policy_no}: {d['endorsement_type']} endorsement effective {d['effective_date']}",
                                     {k: request.POST.getlist(k) for k in request.POST if k != "confirm"})
                    messages.info(request, f"{pa.number} submitted: a second person must confirm this endorsement before it is applied.")
                    return redirect("policy_detail", pk=p.pk)
                endo = services.apply_endorsement(p, d["endorsement_type"], d["effective_date"], request.user, d.get("risk"), **kw)
                messages.success(request, f"Endorsement {endo.number} applied (version {endo.version_after}).")
                return redirect("policy_detail", pk=p.pk)
        except services.ValidationFailed as e:
            for m in e.args[0]:
                form.add_error(None, m)
            quote = None
        except ValueError as e:
            form.add_error(None, str(e))
            quote = None
    return render(request, "motor/endorse.html", {"p": p, "form": form, "quote": quote, "currency_symbols": _currency_json(),
                                                    "can_apply": role != "CUSTOMER"})


@role_required(*CAN_QUOTE)
@require_POST
def renew(request, pk):
    p = get_object_or_404(scope(request.user, Policy.objects.all()), pk=pk)
    if p.status not in (Policy.ACTIVE, Policy.EXPIRED):
        messages.error(request, "Only active or expired policies can be renewed.")
        return redirect("policy_detail", pk=pk)
    from . import phase1
    err = phase1.renewal_error(p)
    if err:
        messages.error(request, err)
        return redirect("policy_detail", pk=pk)
    if p.renewals.filter(status__in=[Proposal.QUOTE, Proposal.PROPOSAL, Proposal.PENDING, Proposal.APPROVED]).exists():
        existing = p.renewals.filter(status__in=[Proposal.QUOTE, Proposal.PROPOSAL, Proposal.PENDING, Proposal.APPROVED]).first()
        messages.info(request, f"{existing.number} is already open for this renewal.")
        return redirect("quote_detail", pk=existing.pk)
    q = services.renew_policy_as_quote(p, request.user)
    messages.success(request, f"Renewal quote {q.number} created from {p.policy_no}. Review and adjust before converting.")
    return redirect("quote_edit", pk=q.pk)


@login_required
def customer_list(request):
    if user_role(request.user) == "CUSTOMER":
        raise PermissionDenied
    from .customers import search
    q = request.GET.get("q", "").strip()
    return render(request, "motor/customer_list.html", {"customers": search(q, limit=200), "q": q})


@login_required
def vehicle_lookup(request):
    """Autocomplete: find an existing vehicle by chassis / plate to re-use its details."""
    q = request.GET.get("q", "").strip().upper()
    if len(q) < 3:
        return JsonResponse({"results": []})
    qs = Vehicle.objects.filter(Q(chassis_no__icontains=q) | Q(plate_number__icontains=q))[:8]
    return JsonResponse({"results": [{"id": v.id, "label": str(v), "chassis_no": v.chassis_no, "make": v.make, "model": v.model,
                                      "year": v.year, "engine_no": v.engine_no, "color": v.color, "vehicle_type": v.vehicle_type,
                                      "usage": v.usage, "cc": v.cc, "seats": v.seats, "plate_code": v.plate_code,
                                      "plate_number": v.plate_number} for v in qs]})

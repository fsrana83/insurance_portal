import csv
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core import clock
from core.models import Currency, user_role
from core.permissions import CAN_QUOTE, role_required, scope
from claims.models import CatEvent, Provider

from . import actions, pdf as pdfmod, services as sv
from .forms import METHODS, PolicyForm, RiskForm
from .models import PncClaim, PncClass, PncCredit, PncPolicy, PncRisk

D = Decimal
QUOTERS = tuple(CAN_QUOTE)
STAFF_PNC = ("PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE", "CREDIT_CONTROL", "AGENT", "BROKER")


def _policy(request, pk):
    return get_object_or_404(scope(request.user, PncPolicy.objects.select_related("customer", "currency", "pnc_class")), pk=pk)


def _claim(request, pk):
    qs = PncClaim.objects.select_related("policy__customer", "policy__currency", "policy__pnc_class", "risk")
    return get_object_or_404(scope(request.user, qs, "policy__producer"), pk=pk)


def _err(request, e):
    messages.error(request, str(e.args[0]) if e.args else "Not allowed")


@login_required
def home(request):
    if user_role(request.user) == "CUSTOMER":
        return redirect("pnc_policy_list")
    pols = scope(request.user, PncPolicy.objects.all())
    cl = scope(request.user, PncClaim.objects.all(), "policy__producer")
    act = pols.filter(status="ACTIVE")
    by_group = {}
    for p in act.select_related("pnc_class", "currency"):
        g = p.pnc_class.get_group_display()
        by_group[g] = by_group.get(g, D(0)) + p.net_premium * p.fx_rate
    ctx = dict(active=act.count(), quotes=pols.filter(status__in=["QUOTE", "REJECTED"]).count(), pending=pols.filter(status="PENDING").count(),
               approved=pols.filter(status="APPROVED").count(), open_claims=cl.exclude(status__in=PncClaim.LOCKED).count(),
               gwp=sum(by_group.values(), D(0)), by_group=sorted(by_group.items(), key=lambda x: -x[1]),
               recent=pols[:8], recent_claims=cl[:6])
    return render(request, "pnc/home.html", ctx)


@login_required
def policy_list(request):
    qs = scope(request.user, PncPolicy.objects.select_related("customer", "currency", "pnc_class"))
    q, st, g = request.GET.get("q", "").strip(), request.GET.get("status", ""), request.GET.get("group", "")
    if q:
        qs = qs.filter(Q(policy_no__icontains=q) | Q(quote_no__icontains=q) | Q(customer__name__icontains=q))
    if st:
        qs = qs.filter(status=st)
    if g:
        qs = qs.filter(pnc_class__group=g)
    from .models import GROUPS
    return render(request, "pnc/policy_list.html", {"rows": qs[:300], "q": q, "st": st, "g": g, "statuses": PncPolicy.STATUSES, "groups": GROUPS})


@role_required(*QUOTERS)
def policy_new(request, pk=None):
    pol = _policy(request, pk) if pk else None
    if pol and not pol.is_editable:
        messages.error(request, "This quote can no longer be edited.")
        return redirect("pnc_policy_detail", pk=pol.pk)
    form = PolicyForm(request.POST or None, instance=pol)
    if request.method == "POST" and form.is_valid():
        p = form.save(commit=False)
        if not pol:
            p.quote_no, p.producer = sv.seq("PQ"), request.user
        p.fx_rate = p.currency.rate_to_base
        p.save()
        if pol and p.risks.exists():
            sv.rate_policy(p)
        messages.success(request, "Saved. Add the risks to be insured.")
        return redirect("pnc_policy_detail", pk=p.pk)
    return render(request, "pnc/policy_form.html", {"form": form, "pol": pol})


@login_required
def policy_detail(request, pk):
    p = _policy(request, pk)
    role = user_role(request.user)
    risks = list(p.risks.all())
    form = RiskForm(p.pnc_class) if p.is_editable and role in QUOTERS else None
    ctx = dict(p=p, risks=risks, riskform=form, methods=METHODS, role=role, can_decide=p.status == "PENDING" and sv.can_decide(request.user, p),
               cessions=p.cessions.select_related("treaty", "risk"), sched=sv.schedule_status(p) if p.installments.exists() else [],
               claims=p.claims.all(), ends=p.endorsements.all(), credit=getattr(p, "credit", None),
               can_credit_decide=role in sv.CREDIT_APPROVERS, can_issue=role in QUOTERS and p.status == "APPROVED",
               can_endorse=role in ("ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER") and p.status == "ACTIVE",
               can_receive=role in ("ADMIN", "FINANCE", "CREDIT_CONTROL", "PRODUCTION_MANAGER", "UNDERWRITER", "AGENT", "BROKER") and p.is_issued,
               fac=p.cessions.filter(is_facultative=True).exists(), etypes=[t for t in p.endorsements.model.TYPES])
    return render(request, "pnc/policy_detail.html", ctx)


@role_required(*QUOTERS)
def risk_add(request, pk):
    p = _policy(request, pk)
    form = RiskForm(p.pnc_class, request.POST)
    if request.method == "POST" and p.is_editable:
        if form.is_valid():
            form.save(p)
            sv.rate_policy(p)
            messages.success(request, "Risk added and premium recalculated.")
        else:
            messages.error(request, "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    return redirect("pnc_policy_detail", pk=p.pk)


@role_required(*QUOTERS)
def risk_remove(request, pk, rid):
    p = _policy(request, pk)
    if request.method == "POST" and p.is_editable:
        p.risks.filter(pk=rid).delete()
        sv.rate_policy(p)
    return redirect("pnc_policy_detail", pk=p.pk)


@role_required(*QUOTERS)
def submit(request, pk):
    p = _policy(request, pk)
    if request.method == "POST":
        try:
            sv.submit(p, request.user)
            messages.success(request, "Approved." if p.status == "APPROVED" else "Sent for underwriter approval.")
        except sv.PncError as e:
            _err(request, e)
    return redirect("pnc_policy_detail", pk=p.pk)


@role_required("PRODUCTION_MANAGER", "UNDERWRITER")
def decide(request, pk, action):
    p = _policy(request, pk)
    if request.method == "POST":
        try:
            sv.decide(p, request.user, action == "approve", request.POST.get("note", ""))
            messages.success(request, "Decision recorded.")
        except sv.PncError as e:
            _err(request, e)
    return redirect("pnc_policy_detail", pk=p.pk)


@role_required(*QUOTERS)
def credit_request(request, pk):
    p = _policy(request, pk)
    if request.method == "POST":
        try:
            sv.request_credit(p, request.user, request.POST.get("installments", 1), request.POST.get("advance") or 0, request.POST.get("note", ""))
            messages.success(request, "Credit requested: Credit Control will decide.")
        except (sv.PncError, ArithmeticError, ValueError) as e:
            _err(request, e)
    return redirect("pnc_policy_detail", pk=p.pk)


@role_required("CREDIT_CONTROL", "FINANCE")
def credit_list(request):
    return render(request, "pnc/credit_list.html", {"pending": PncCredit.objects.filter(status="PENDING").select_related("policy__customer", "policy__currency", "requested_by"),
                                                     "decided": PncCredit.objects.exclude(status="PENDING").select_related("policy__customer", "decided_by")[:50]})


@role_required("CREDIT_CONTROL", "FINANCE")
def credit_decide(request, pk, action):
    c = get_object_or_404(PncCredit, pk=pk)
    if request.method == "POST":
        try:
            sv.decide_credit(c, request.user, action == "approve", request.POST.get("note", ""))
            messages.success(request, "Credit decision recorded.")
        except sv.PncError as e:
            _err(request, e)
    return redirect("pnc_credit_list")


@role_required(*QUOTERS)
def issue(request, pk):
    p = _policy(request, pk)
    if request.method == "POST":
        try:
            sv.issue(p, request.user, request.POST.get("basis", ""), request.POST.get("method", ""), request.POST.get("reference", ""), request.POST.get("fac_reference", ""))
            messages.success(request, f"Policy {p.policy_no} issued.")
            if p.cessions.filter(is_facultative=True).exists():
                messages.warning(request, "Part of the sum insured exceeds treaty capacity: place the excess facultatively.")
        except sv.PncError as e:
            _err(request, e)
    return redirect("pnc_policy_detail", pk=p.pk)


@login_required
def receipt(request, pk):
    p = _policy(request, pk)
    if request.method == "POST" and user_role(request.user) in ("ADMIN", "FINANCE", "CREDIT_CONTROL", "PRODUCTION_MANAGER", "UNDERWRITER", "AGENT", "BROKER"):
        try:
            r = sv.record_receipt(p, request.POST.get("amount") or 0, request.POST.get("method", ""), request.user, request.POST.get("reference", ""))
            messages.success(request, f"Receipt {r.number} recorded.")
        except (sv.PncError, ArithmeticError) as e:
            _err(request, e)
    return redirect("pnc_policy_detail", pk=p.pk)


@login_required
def policy_pdf(request, pk):
    p = _policy(request, pk)
    resp = HttpResponse(pdfmod.schedule(p), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{p.number.replace("/", "-")}.pdf"'
    return resp


@role_required("PRODUCTION_MANAGER", "UNDERWRITER")
def endorse(request, pk):
    p = _policy(request, pk)
    if request.method == "POST":
        P = request.POST
        et = P.get("type", "")
        try:
            eff = date.fromisoformat(P.get("effective") or clock.today().isoformat())
            payload = dict(effective=eff.isoformat(), risk=P.get("risk") or "", new_si=P.get("new_si", ""), new_end=P.get("new_end", ""), description=P.get("description", ""),
                           conditions=P.get("conditions") if et == "DETAILS" else None, sum_insured=P.get("sum_insured", ""), location=P.get("location", ""),
                           value_at_risk=P.get("value_at_risk", ""), loading_pct=P.get("loading_pct", ""))
            if et == "ADD_RISK":
                payload["description"] = P.get("risk_description", "")
                payload["note"] = P.get("description", "")
            done, msg = actions.request_or_apply(request.user, "policy", p, et, f"{dict(p.endorsements.model.TYPES).get(et, 'Endorsement')} on {p.number}", payload, P.get("description", ""))
            (messages.success if done else messages.info)(request, msg)
        except (sv.PncError, ValueError, KeyError, ArithmeticError) as e:
            _err(request, e)
    return redirect("pnc_policy_detail", pk=p.pk)


# ---- claims ----------------------------------------------------------------------------------------------------------
@login_required
def claim_list(request):
    qs = scope(request.user, PncClaim.objects.select_related("policy__customer", "policy__currency", "policy__pnc_class", "risk"), "policy__producer")
    st, q = request.GET.get("status", ""), request.GET.get("q", "").strip()
    if st:
        qs = qs.filter(status=st)
    if q:
        qs = qs.filter(Q(claim_no__icontains=q) | Q(policy__policy_no__icontains=q) | Q(policy__customer__name__icontains=q))
    return render(request, "pnc/claim_list.html", {"rows": qs[:300], "st": st, "q": q, "statuses": PncClaim.STATUSES})


@role_required("CLAIMS_HANDLER", "CLAIMS_MANAGER")
def claim_new(request, pk):
    p = _policy(request, pk)
    if request.method == "POST":
        P = request.POST
        try:
            risk = get_object_or_404(p.risks, pk=P.get("risk"))
            c = sv.register_claim(p, risk, request.user, date.fromisoformat(P.get("date_of_loss", "")), P.get("cause", "OTHER"), P.get("reserve") or 0,
                                  P.get("description", ""), P.get("location", ""), is_legal=bool(P.get("is_legal")))
            messages.success(request, f"Claim {c.claim_no} registered.")
            return redirect("pnc_claim_detail", pk=c.pk)
        except (sv.PncError, ValueError, ArithmeticError) as e:
            _err(request, e)
    return render(request, "pnc/claim_form.html", {"p": p, "causes": PncClaim.CAUSES, "today": clock.today()})


@login_required
def claim_detail(request, pk):
    c = _claim(request, pk)
    role = user_role(request.user)
    base = Currency.base()
    from core.models import to_base
    ctx = dict(c=c, role=role, handler=role in sv.CLAIM_HANDLERS, layers=[(t, k, a) for t, k, a in sv.claim_layers(c, sv.incurred_base(c))],
               retained=sv.retained_loss(c), incurred=sv.incurred_base(c), payments=c.payments.all(), recoveries=c.recoveries.all(),
               adjusters=Provider.objects.filter(provider_type="SURVEYOR") if hasattr(Provider, "provider_type") else Provider.objects.all(),
               cats=CatEvent.objects.all(), base=base, deductible=sv.deductible_for(c, c.assessed_loss or c.reserve), avg=sv.average_factor(c))
    return render(request, "pnc/claim_detail.html", ctx)


@role_required("CLAIMS_HANDLER", "CLAIMS_MANAGER")
def claim_action(request, pk, action):
    c = _claim(request, pk)
    P = request.POST
    if request.method != "POST":
        return redirect("pnc_claim_detail", pk=c.pk)
    try:
        if action == "reserve":
            from . import nonmotor
            ch = nonmotor.revise_reserve(c, request.user, P.get("reserve") or 0, P.get("reason", ""))
            if ch.status == "PENDING":
                messages.warning(request, "The change is above your authority and is waiting for the Claims Manager.")
        elif action == "assess":
            adj = Provider.objects.filter(pk=P.get("adjuster")).first() if P.get("adjuster") else None
            sv.assess(c, request.user, P.get("assessed_loss") or 0, adj)
        elif action == "approve":
            sv.approve(c, request.user)
        elif action == "pay":
            sv.pay(c, request.user, P.get("amount") or 0, P.get("payee", "") or c.policy.customer.name, P.get("stage", "INTERIM"), P.get("reference", ""))
        elif action == "recovery":
            sv.add_recovery(c, request.user, P.get("rtype", "SALVAGE"), P.get("amount") or 0, note=P.get("note", ""))
        elif action == "cat":
            c.cat_event = CatEvent.objects.filter(pk=P.get("cat_event")).first() if P.get("cat_event") else None
            c.save(update_fields=["cat_event"])
        elif action in actions.CLAIM_ACTIONS:
            done, msg = actions.request_or_apply(request.user, "claim", c, action, f"{actions.CLAIM_ACTIONS[action]} {c.claim_no}", {"reason": P.get("reason", "")}, P.get("reason", ""))
            (messages.success if done else messages.info)(request, msg)
            return redirect("pnc_claim_detail", pk=c.pk)
        else:
            raise Http404
        messages.success(request, "Done.")
    except (sv.PncError, ArithmeticError, ValueError) as e:
        _err(request, e)
    return redirect("pnc_claim_detail", pk=c.pk)


# ---- reports ---------------------------------------------------------------------------------------------------------
REPORT_ROLES = ("PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER", "CREDIT_CONTROL")


def _tables(request):
    from core.models import to_base
    pol = []
    for p in scope(request.user, PncPolicy.objects.exclude(policy_no=None).select_related("customer", "currency", "pnc_class")):
        pol.append([p.policy_no, p.customer.name, p.pnc_class.name, p.get_status_display(), p.start_date, p.end_date, p.currency.code, p.sum_insured, p.net_premium, p.tax, p.total_payable,
                    p.paid_amount, p.balance_due])
    cl = []
    for c in scope(request.user, PncClaim.objects.select_related("policy__customer", "policy__currency", "policy__pnc_class", "risk"), "policy__producer"):
        cl.append([c.claim_no, c.policy.policy_no, c.policy.customer.name, c.policy.pnc_class.name, c.date_of_loss, c.get_status_display(), c.policy.currency.code, c.reserve, c.assessed_loss,
                   c.net_payable, c.total_paid, c.outstanding, sv.ri_total(c, sv.incurred_base(c)), sv.retained_loss(c), c.cat_event.code if c.cat_event_id else ""])
    from .models import PncCession
    ces = [[x.policy.policy_no, x.risk.number, x.treaty.reference if x.treaty_id else "FAC", x.get_treaty_type_display() if hasattr(x, "get_treaty_type_display") else x.treaty_type,
            x.sum_insured_ceded, x.premium_ceded, x.cession_pct, "Yes" if x.is_facultative else ""] for x in PncCession.objects.select_related("policy", "risk", "treaty")]
    ag = []
    for p in scope(request.user, PncPolicy.objects.filter(status="ACTIVE").select_related("customer", "currency")):
        bal = p.balance_due
        if bal > 0:
            overdue = [r for r in sv.schedule_status(p) if r[2] > 0 and r[3] > 0] if p.installments.exists() else []
            days = max([r[3] for r in overdue] or [max((clock.today() - p.start_date).days - 30, 0)])
            ag.append([p.policy_no, p.customer.name, p.currency.code, bal, days, "0-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"])
    return {
        "premium": ("Premium register", ["Policy", "Insured", "Class", "Status", "Start", "Expiry", "Cur", "Sum insured", "Net premium", "VAT", "Total", "Received", "Balance"], pol),
        "claims": ("Claims register", ["Claim", "Policy", "Insured", "Class", "Loss date", "Status", "Cur", "Reserve", "Assessed", "Net payable", "Paid", "Outstanding", "Reinsurers (base)", "Retained (base)", "CAT"], cl),
        "cessions": ("Reinsurance cessions", ["Policy", "Risk", "Treaty", "Type", "Sum insured ceded", "Premium ceded", "%", "Facultative"], ces),
        "aging": ("Receivables aging", ["Policy", "Insured", "Cur", "Balance", "Days overdue", "Bucket"], ag),
    }


@role_required(*REPORT_ROLES)
def report(request, slug=None):
    t = _tables(request)
    if slug is None:
        return render(request, "pnc/report_hub.html", {"items": [(k, v[0], len(v[2])) for k, v in t.items()]})
    if slug not in t:
        raise Http404
    title, head, rows = t[slug]
    if request.GET.get("csv"):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="pnc_{slug}.csv"'
        w = csv.writer(resp)
        w.writerow(head)
        w.writerows(rows)
        return resp
    return render(request, "pnc/report.html", {"title": title, "head": head, "rows": rows[:1000], "slug": slug, "total": len(rows)})

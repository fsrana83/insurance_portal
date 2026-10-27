"""Accounting reports: transaction journal, trial balance, IFRS 17 movement, revenue recognition run."""
import csv
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.permissions import role_required

from . import services
from .models import Account, FinancialDocument, JournalEntry, JournalLine

D = Decimal
ZERO = D("0")
ROLES = ("FINANCE", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_MANAGER")


def _dates(request, default_start=None):
    today = date.today()
    try:
        s = date.fromisoformat(request.GET.get("from", ""))
    except ValueError:
        s = default_start or today.replace(month=1, day=1)
    try:
        e = date.fromisoformat(request.GET.get("to", ""))
    except ValueError:
        e = today
    return s, e


@role_required(*ROLES)
def transactions(request):
    """Accounting transaction report: every journal line in the period."""
    services.ensure_accounts()
    s, e = _dates(request, date.today().replace(day=1))
    g = request.GET
    qs = JournalLine.objects.filter(entry__date__gte=s, entry__date__lte=e).select_related("entry", "account", "entry__policy", "entry__claim")
    if g.get("type"):
        qs = qs.filter(entry__entry_type=g["type"])
    if g.get("account"):
        qs = qs.filter(account_id=g["account"])
    if g.get("q"):
        q = g["q"].strip()
        qs = qs.filter(Q(entry__policy__policy_no__icontains=q) | Q(entry__claim__claim_no__icontains=q) | Q(entry__number__icontains=q) | Q(entry__narration__icontains=q))
    qs = qs.order_by("entry__date", "entry_id", "id")
    tot = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    if g.get("format") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="accounting_transactions_{s}_{e}.csv"'
        w = csv.writer(resp)
        w.writerow(["Date", "Journal", "Type", "Policy", "Claim", "Narration", "Account", "Account name", "Debit (OMR)", "Credit (OMR)", "IFRS 17 line"])
        for l in qs.iterator():
            w.writerow([l.entry.date, l.entry.number, l.entry.get_entry_type_display(), l.entry.policy.policy_no if l.entry.policy else "",
                        l.entry.claim.claim_no if l.entry.claim else "", l.entry.narration, l.account_id, l.account.name, l.debit, l.credit, l.account.ifrs17_line])
        return resp
    n = qs.count()
    return render(request, "accounting/transactions.html", {
        "lines": qs[:1500], "n": n, "truncated": n > 1500, "debit": tot["d"] or ZERO, "credit": tot["c"] or ZERO, "date_from": s, "date_to": e,
        "types": JournalEntry.TYPES, "accounts": Account.objects.all(), "f": g, "tab": "transactions"})


@role_required(*ROLES)
def trial_balance(request):
    services.ensure_accounts()
    s, e = _dates(request)
    rows, td, tc = [], ZERO, ZERO
    for a in Account.objects.all():
        before = JournalLine.objects.filter(account=a, entry__date__lt=s).aggregate(d=Sum("debit"), c=Sum("credit"))
        within = JournalLine.objects.filter(account=a, entry__date__gte=s, entry__date__lte=e).aggregate(d=Sum("debit"), c=Sum("credit"))
        opening = (before["d"] or ZERO) - (before["c"] or ZERO)
        dr, cr = within["d"] or ZERO, within["c"] or ZERO
        closing = opening + dr - cr
        if opening or dr or cr:
            rows.append({"a": a, "opening": opening, "debit": dr, "credit": cr, "closing": closing})
        td += dr
        tc += cr
    return render(request, "accounting/trial_balance.html", {"rows": rows, "date_from": s, "date_to": e, "td": td, "tc": tc, "tab": "tb"})


def _net(code, s, e, types=None, credit_normal=True):
    """Net movement of an account in [s, e] (optionally only for some entry types), in its normal direction."""
    qs = JournalLine.objects.filter(account_id=code, entry__date__gte=s, entry__date__lte=e)
    if types:
        qs = qs.filter(entry__entry_type__in=types)
    a = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    d, c = a["d"] or ZERO, a["c"] or ZERO
    return (c - d) if credit_normal else (d - c)


def _bal(code, before, credit_normal=True):
    a = JournalLine.objects.filter(account_id=code, entry__date__lt=before).aggregate(d=Sum("debit"), c=Sum("credit"))
    d, c = a["d"] or ZERO, a["c"] or ZERO
    return (c - d) if credit_normal else (d - c)


@role_required(*ROLES)
def ifrs17(request):
    """IFRS 17 (PAA) reconciliations and the insurance service result for the period."""
    from datetime import timedelta

    services.ensure_accounts()
    s, e = _dates(request)
    after = e + timedelta(days=1)
    # Liability for remaining coverage (2110): opening + premiums written - revenue released = closing
    lrc = {"open": _bal("2110", s), "written": _net("2110", s, e, ["ISSUE", "ENDORSE"]), "revenue": _net("2110", s, e, ["EARN"], credit_normal=False),
           "close": _bal("2110", after)}
    lrc["check"] = lrc["open"] + lrc["written"] - lrc["revenue"] - lrc["close"]
    # Liability for incurred claims (2120): opening + incurred - paid = closing
    lic = {"open": _bal("2120", s), "incurred": _net("2120", s, e, ["CLAIM"]), "paid": _net("2120", s, e, ["CLAIM_PAY"], credit_normal=False),
           "close": _bal("2120", after)}
    lic["check"] = lic["open"] + lic["incurred"] - lic["paid"] - lic["close"]
    arc = {"open": _bal("1210", s, False), "ceded": _net("1210", s, e, ["ISSUE", "ENDORSE"], credit_normal=False),
           "alloc": _net("1210", s, e, ["EARN"]), "close": _bal("1210", after, False)}
    arc["check"] = arc["open"] + arc["ceded"] - arc["alloc"] - arc["close"]
    aic = {"open": _bal("1230", s, False), "recov": _net("1230", s, e, ["CLAIM"], credit_normal=False),
           "recvd": _net("1230", s, e, ["RECOVERY"]), "close": _bal("1230", after, False)}
    aic["check"] = aic["open"] + aic["recov"] - aic["recvd"] - aic["close"]
    revenue = _net("4110", s, e)
    claims_exp = _net("5110", s, e, credit_normal=False)
    acq = _net("5120", s, e, credit_normal=False)
    ri_alloc = _net("5130", s, e, credit_normal=False)
    ri_rec = _net("4210", s, e)
    pl = {"revenue": revenue, "claims": claims_exp, "acq": acq, "service_result": revenue - claims_exp - acq,
          "ri_alloc": ri_alloc, "ri_rec": ri_rec, "ri_result": ri_rec - ri_alloc}
    pl["net"] = pl["service_result"] + pl["ri_result"]
    return render(request, "accounting/ifrs17.html", {
        "date_from": s, "date_to": e, "tab": "ifrs17", "lrc": lrc, "lic": lic, "arc": arc, "aic": aic, "pl": pl,
        "last_run": JournalEntry.objects.filter(entry_type="EARN").order_by("-date").values_list("date", flat=True).first()})


@role_required("FINANCE", "ADMIN")
@require_POST
def run_revenue(request):
    try:
        through = date.fromisoformat(request.POST.get("through", ""))
    except ValueError:
        messages.error(request, "Choose the date to recognise revenue to.")
        return redirect("acc_ifrs17")
    if through > date.today():
        messages.error(request, "Revenue can only be recognised up to today.")
        return redirect("acc_ifrs17")
    n = services.recognise_revenue(through, request.user)
    messages.success(request, f"Revenue recognised to {through:%d %b %Y}: {n} policy entr{'y' if n == 1 else 'ies'} posted (policies already up to date are skipped).")
    return redirect("acc_ifrs17")


@role_required(*ROLES)
def fin_documents(request):
    qs = FinancialDocument.objects.select_related("policy", "customer", "currency")
    g = request.GET
    if g.get("type"):
        qs = qs.filter(doc_type=g["type"])
    if g.get("q"):
        qs = qs.filter(Q(number__icontains=g["q"]) | Q(policy__policy_no__icontains=g["q"]) | Q(customer__name__icontains=g["q"]))
    return render(request, "accounting/documents.html", {"docs": qs[:300], "types": FinancialDocument.TYPES, "f": g, "tab": "docs"})


@role_required("FINANCE", "PRODUCTION_MANAGER", "UNDERWRITER", "AGENT", "BROKER", "CLAIMS_MANAGER", "CUSTOMER")
def document_pdf(request, pk):
    from core.permissions import scope
    from motor.models import Policy

    from .documents import financial_document_pdf

    fd = get_object_or_404(FinancialDocument.objects.filter(policy__in=scope(request.user, Policy.objects.all())), pk=pk)
    r = HttpResponse(financial_document_pdf(fd), content_type="application/pdf")
    r["Content-Disposition"] = f'inline; filename="{fd.number.replace("/", "-")}.pdf"'
    return r

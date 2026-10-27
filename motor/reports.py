import csv
from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render

from core.models import Currency, to_base, user_role
from core.permissions import role_required, scope

D = Decimal
ZERO = D("0")

REPORTS = {
    "production": "Production report",
    "new_business": "New business report",
    "renewal": "Renewal report",
    "claims_ratio": "Claims ratio report",
}


def _dates(request):
    today = date.today()
    try:
        s = date.fromisoformat(request.GET.get("from", ""))
    except ValueError:
        s = today.replace(day=1)
    try:
        e = date.fromisoformat(request.GET.get("to", ""))
    except ValueError:
        e = today
    return s, e


def production(request, s, e):
    from motor.models import Policy

    pols = scope(request.user, Policy.objects.filter(issued_at__date__gte=s, issued_at__date__lte=e)).select_related("producer", "product", "currency")
    agg = {}
    for p in pols:
        k = (p.issued_at.date().isoformat(), p.producer.get_username(), p.product.name)
        row = agg.setdefault(k, [0, ZERO])
        row[0] += 1
        row[1] += to_base(p.net_premium, p.currency)
    cols = ["Date", "Producer", "Product", "Policies", "Net premium (OMR)"]
    return cols, [[*k, v[0], v[1]] for k, v in sorted(agg.items())]


def new_business(request, s, e):
    from motor.models import Policy

    pols = scope(request.user, Policy.objects.filter(issued_at__date__gte=s, issued_at__date__lte=e, proposal__renewal_of__isnull=True)) \
        .select_related("customer", "product", "currency", "producer")
    cols = ["Policy", "Issued", "Customer", "Product", "Producer", "Currency", "Net premium", "Net premium (OMR)"]
    return cols, [[p.policy_no, p.issued_at.date().isoformat(), p.customer.name, p.product.name, p.producer.get_username(), p.currency.code,
                   p.net_premium, to_base(p.net_premium, p.currency)] for p in pols]


def renewal(request, s, e):
    from motor.models import Policy

    due = scope(request.user, Policy.objects.filter(end_date__gte=s, end_date__lte=e)).select_related("customer", "currency")
    cols = ["Policy", "Customer", "Expiry", "Net premium (OMR)", "Outcome"]
    rows, renewed = [], 0
    for p in due:
        done = p.renewals.filter(status="ISSUED").exists()
        renewed += done
        rows.append([p.policy_no, p.customer.name, p.end_date.isoformat(), to_base(p.net_premium, p.currency), "Renewed" if done else ("Cancelled" if p.status == "CANCELLED" else "Lost / open")])
    if rows:
        rows.append(["", "", "Retention", "", f"{renewed}/{len(rows)} = {renewed / len(rows) * 100:.1f}%"])
    return cols, rows


def claims_ratio(request, s, e):
    from claims.models import Claim
    from motor.models import Policy

    pols = Policy.objects.filter(issued_at__date__lte=e, end_date__gte=s).select_related("currency", "product")
    out = {}
    for p in pols:
        total = max((p.end_date - p.start_date).days, 1)
        lo, hi = max(p.start_date, s), min(p.end_date, e)
        earned = to_base(p.net_premium, p.currency) * D(max((hi - lo).days, 0)) / D(total)
        out.setdefault(p.product.name, [ZERO, ZERO])[0] += earned
    for c in Claim.objects.filter(date_of_loss__gte=s, date_of_loss__lte=e).select_related("policy__product", "policy__currency"):
        out.setdefault(c.policy.product.name, [ZERO, ZERO])[1] += to_base(c.approved_repair_cost or c.reserve, c.policy.currency)
    cols = ["Product", "Earned premium (OMR)", "Incurred claims (OMR)", "Loss ratio %"]
    rows = [[k, round(v[0], 3), round(v[1], 3), f"{(v[1] / v[0] * 100):.1f}" if v[0] else "—"] for k, v in out.items()]
    return cols, rows


_FUNCS = {"production": production, "new_business": new_business, "renewal": renewal, "claims_ratio": claims_ratio}


CARDS = [
    # (group, title, description, url name, url kwargs / query, roles that may open it; None = same as the report page)
    ("Production & renewals", "Production report", "Policies and net premium by day, producer and product.", "report_run", "type=production", "ALLP"),
    ("Production & renewals", "New business report", "Every new policy issued in the period.", "report_run", "type=new_business", "ALLP"),
    ("Production & renewals", "Renewal report", "Policies expiring in the period and what happened to them.", "report_run", "type=renewal", "ALLP"),
    ("Production & renewals", "Broker production & commission", "Production by broker with commission earned, due, paid and balance.", "register:broker-commission", "", "ALLP"),
    ("Premium & reinsurance", "Premium register", "Every policy, endorsement and cancellation, with premium ceded by treaty.", "premium_register", "", "ALLP"),
    ("Premium & reinsurance", "Premium by treaty", "Reinsurance distribution of each premium transaction, treaty by treaty.", "register:premium-treaty", "", "STAFF"),
    ("Premium & reinsurance", "Reinsurance by treaty", "Premium ceded, claims share, recoveries and cash per treaty (incl. excess of loss).", "register:ri-treaty", "", "STAFF"),
    ("Claims", "Claim register", "Claims registered in the period with reinsurance recoveries by treaty.", "register:claims-register", "", "STAFF"),
    ("Claims", "Treaty recovery journal", "Reinsurers' share of every claim payment posted by treaty, with cash received and balance due.", "register:treaty-recovery", "", "STAFF"),
    ("Claims", "Injuries and deaths", "Bodily injuries and deaths recorded on claims.", "register:injuries", "", "STAFF"),
    ("Claims", "Legal claims", "Claims in legal process with latest action and next date.", "legal_list", "", "STAFF"),
    ("Management", "Change log", "Field by field history of every change to policies and claims.", "changelog", "", "STAFF"),
    ("Claims", "Claims incurred", "Incurred in a selected period: paid + change in outstanding, by treaty.", "register:claims-incurred", "", "STAFF"),
    ("Claims", "Outstanding claims as at", "Claims reserve at any date, with ageing and reinsurers' share.", "register:claims-os", "", "STAFF"),
    ("Claims", "Paid claims", "Payments made in the period and reinsurance cash received.", "register:claims-paid", "", "STAFF"),
    ("Finance", "Receivables / payables ageing", "Detailed ageing of premium and claims receivables and payables, with summary, as at a date.", "register:aging", "", "FIN"),
    ("Profitability", "Claims ratio by product", "Earned premium against incurred claims by product.", "report_run", "type=claims_ratio", "STAFF"),
    ("Profitability", "Loss ratio by insured / vehicle", "Loss ratio by insured, by vehicle or both, worst first.", "register:loss-ratio", "", "STAFF"),
    ("Management", "Management insights", "KPIs, trends and findings for management. Export as PDF, PowerPoint, Word, Excel or CSV.", "management", "", "MGMT"),
    ("Management", "Budget vs actual", "Budget, forecast and outlier analytics.", "budget_analytics", "", "BUDGET"),
]
MGMT_ROLES = ("PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER")


def _visible(role, kind):
    from core.permissions import PRODUCERS
    if role == "ADMIN":
        return True
    if kind == "ALLP":
        return role in PRODUCERS | {"PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER", "CLAIMS_HANDLER"} and True
    if kind == "STAFF":
        return role in ("PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER", "CLAIMS_HANDLER")
    if kind == "FIN":
        return role in ("FINANCE", "CREDIT_CONTROL", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_MANAGER")
    if kind == "MGMT":
        return role in MGMT_ROLES
    if kind == "BUDGET":
        return role in ("FINANCE", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_MANAGER")
    return False


@role_required("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER", "CLAIMS_HANDLER", "CREDIT_CONTROL")
def report_hub(request):
    from django.urls import reverse

    role = user_role(request.user)
    groups = {}
    for group, title, desc, name, query, kind in CARDS:
        if not _visible(role, kind):
            continue
        if name.startswith("register:"):
            url = reverse("register", kwargs={"slug": name.split(":")[1]})
        else:
            url = reverse(name)
        if query:
            url += "?" + query
        groups.setdefault(group, []).append({"title": title, "desc": desc, "url": url})
    return render(request, "motor/report_hub.html", {"groups": groups})


@role_required("AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER")
def report_view(request):
    kind = request.GET.get("type", "production")
    if kind not in _FUNCS:
        kind = "production"
    from .registers import gate

    ran, err = gate(request)
    ctx = {"kind": kind, "reports": REPORTS, "title": REPORTS[kind], "waiting": not ran, "error": err, "f": request.GET}
    if not ran:
        ctx.update({"cols": [], "rows": [], "date_from": _iso(request.GET.get("from")), "date_to": _iso(request.GET.get("to"))})
        return render(request, "motor/report.html", ctx)
    s, e = _dates(request)
    cols, rows = _FUNCS[kind](request, s, e)
    if request.GET.get("format") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{kind}_{s}_{e}.csv"'
        w = csv.writer(resp)
        w.writerow(cols)
        w.writerows(rows)
        return resp
    ctx.update({"cols": cols, "rows": rows, "date_from": s, "date_to": e})
    return render(request, "motor/report.html", ctx)


def _iso(v):
    try:
        return date.fromisoformat(v or "")
    except ValueError:
        return None


@role_required("PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER")
def management(request):
    """Management insights / performance analytics. Nothing is computed until a period is chosen and run."""
    from budgeting import charts
    from . import mgmt_export, mgmt_pack
    from .registers import gate

    ran, err = gate(request)
    ctx = {"waiting": not ran, "error": err, "date_from": _iso(request.GET.get("from")), "date_to": _iso(request.GET.get("to")), "formats": list(mgmt_export.FORMATS)}
    if not ran:
        return render(request, "motor/management.html", ctx)
    s, e = date.fromisoformat(request.GET["from"]), date.fromisoformat(request.GET["to"])
    pack = mgmt_pack.build(request.user, s, e)
    fmt = request.GET.get("format")
    if fmt in mgmt_export.FORMATS:
        data, mime, name = mgmt_export.export(pack, fmt)
        resp = HttpResponse(data, content_type=mime)
        resp["Content-Disposition"] = f'attachment; filename="{name}"'
        return resp
    t = pack["trend"]
    chart = charts.bars([r["label"] for r in t], [("Written", "ch-act", [float(r["gwp"]) for r in t], "bar"), ("Claims", "ch-fc-bar", [float(r["claims"]) for r in t], "bar"),
                                                   ("Loss ratio %", "ch-fc", [float(r["lr"]) if r["lr"] is not None else None for r in t], "line")],
                        title="Premium and claims by month", unit="") if t else ""
    ctx.update({"pack": pack, "chart": chart, "date_from": s, "date_to": e, "tables": mgmt_export.tables(pack)[2:-1]})
    return render(request, "motor/management.html", ctx)

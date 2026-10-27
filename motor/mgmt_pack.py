"""Management insights pack: KPIs, trends, performance analytics and findings for a period, from the IFRS 17 ledger.

``build(user, start, end)`` returns plain data (dicts / lists) that the page and every export format (PDF, PPTX, DOCX, XLSX, CSV)
render from, so all of them always show the same numbers."""
from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import Count, F, Sum
from django.db.models.functions import Coalesce, TruncMonth

from accounting.models import JournalLine
from core.models import Company
from core.permissions import scope

D = Decimal
ZERO = D("0")
HUNDRED = D(100)
TARGET_LR = D(70)


def _pct(a, b):
    return (D(a) / D(b) * HUNDRED) if b else None


def _chg(cur, prev):
    """% change; None when the earlier figure is zero."""
    return ((D(cur) - D(prev)) / abs(D(prev)) * HUNDRED) if prev else None


def _months(s, e):
    out, y, m = [], s.year, s.month
    while (y, m) <= (e.year, e.month) and len(out) < 36:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _lines(s, e):
    return JournalLine.objects.filter(entry__book="MOTOR", entry__date__gte=s, entry__date__lte=e)


def totals(s, e):
    """Ledger totals for a period."""
    L = _lines(s, e)

    def net(qs, side):
        a = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
        return ((a["d"] or ZERO) - (a["c"] or ZERO)) if side == "D" else ((a["c"] or ZERO) - (a["d"] or ZERO))

    gwp = net(L.filter(account_id="2110", entry__entry_type__in=["ISSUE", "ENDORSE"]), "C")
    earned = net(L.filter(account_id="4110", entry__entry_type="EARN"), "C")
    claims = net(L.filter(account_id="5110", entry__entry_type__in=["CLAIM", "CLAIM_PAY"]), "D")
    recov = net(L.filter(account_id="5110", entry__entry_type="RECOVERY"), "C")
    comm = net(L.filter(account_id="5120"), "D")
    ceded = net(L.filter(account_id="2230", entry__entry_type__in=["ISSUE", "ENDORSE"]), "C")
    ri_rec = net(L.filter(account_id="1230", entry__entry_type="CLAIM"), "D")
    ri_cash = net(L.filter(account_id="1230", entry__entry_type="RECOVERY"), "C")
    return {"gwp": gwp, "earned": earned, "claims": claims, "recoveries": recov, "comm": comm, "ceded": ceded, "ri_recoverable": ri_rec, "ri_cash": ri_cash}


def _group(s, e, key):
    """{key: {metric: value}} - key is 'product', 'producer' or 'loss'."""
    L = _lines(s, e)
    if key == "product":
        k_pol, k_clm = F("entry__policy__product__name"), F("entry__claim__policy__product__name")
    elif key == "producer":
        k_pol, k_clm = F("entry__policy__producer__username"), F("entry__claim__policy__producer__username")
    else:
        k_pol, k_clm = None, F("entry__claim__loss_type")
    out = defaultdict(lambda: defaultdict(lambda: ZERO))
    pol = L.filter(entry__policy__isnull=False)
    clm = L.filter(entry__claim__isnull=False)
    if k_pol is not None:
        for acct, types, name, side in (("2110", ["ISSUE", "ENDORSE"], "gwp", "C"), ("4110", ["EARN"], "earned", "C"), ("5120", None, "comm", "D"),
                                        ("2230", ["ISSUE", "ENDORSE"], "ceded", "C")):
            qs = pol.filter(account_id=acct)
            if types:
                qs = qs.filter(entry__entry_type__in=types)
            for r in qs.annotate(k=k_pol).values("k").annotate(c=Sum("credit"), d=Sum("debit")):
                out[r["k"]][name] += ((r["c"] or ZERO) - (r["d"] or ZERO)) if side == "C" else ((r["d"] or ZERO) - (r["c"] or ZERO))
    for r in clm.filter(account_id="5110", entry__entry_type__in=["CLAIM", "CLAIM_PAY"]).annotate(k=k_clm).values("k").annotate(c=Sum("credit"), d=Sum("debit")):
        out[r["k"]]["claims"] += (r["d"] or ZERO) - (r["c"] or ZERO)
    for r in clm.filter(account_id="1230", entry__entry_type="CLAIM").annotate(k=k_clm).values("k").annotate(c=Sum("credit"), d=Sum("debit")):
        out[r["k"]]["ri"] += (r["d"] or ZERO) - (r["c"] or ZERO)
    return out


def _trend(s, e):
    L = _lines(s, e).annotate(mo=TruncMonth("entry__date"))
    data = defaultdict(lambda: defaultdict(lambda: ZERO))
    for acct, types, name, side in (("2110", ["ISSUE", "ENDORSE"], "gwp", "C"), ("4110", ["EARN"], "earned", "C"), ("5110", ["CLAIM", "CLAIM_PAY"], "claims", "D")):
        for r in L.filter(account_id=acct, entry__entry_type__in=types).values("mo").annotate(c=Sum("credit"), d=Sum("debit")):
            k = (r["mo"].year, r["mo"].month)
            data[k][name] += ((r["c"] or ZERO) - (r["d"] or ZERO)) if side == "C" else ((r["d"] or ZERO) - (r["c"] or ZERO))
    rows = []
    for y, m in _months(s, e):
        d = data[(y, m)]
        rows.append({"label": date(y, m, 1).strftime("%b %Y"), "gwp": d["gwp"], "earned": d["earned"], "claims": d["claims"], "lr": _pct(d["claims"], d["earned"])})
    return rows


def _fake(user, **get):
    return SimpleNamespace(user=user, GET=get)


def build(user, s, e):
    from claims.models import Claim
    from motor import registers
    from motor.models import Policy

    base = Company.get().currency_code if hasattr(Company.get(), "currency_code") else "OMR"
    days = (e - s).days + 1
    ps, pe = s - timedelta(days=days), s - timedelta(days=1)
    cur, prev = totals(s, e), totals(ps, pe)
    pols = scope(user, Policy.objects.filter(issued_at__date__gte=s, issued_at__date__lte=e))
    prev_pols = scope(user, Policy.objects.filter(issued_at__date__gte=ps, issued_at__date__lte=pe))
    n_pol, n_prev = pols.count(), prev_pols.count()
    n_new = pols.filter(proposal__renewal_of__isnull=True).count()
    # collection
    invoiced = collected = ZERO
    for p in pols.prefetch_related("payments"):
        invoiced += D(p.total_payable) * D(p.fx_rate)
        collected += sum((D(x.amount) * D(x.fx_rate) for x in p.payments.all() if x.status == "PAID"), ZERO)
    # renewals due in the period
    due = scope(user, Policy.objects.filter(end_date__gte=s, end_date__lte=e)).exclude(status="CANCELLED")
    renewed = sum(1 for p in due if p.renewals.filter(status="ISSUED").exists())
    n_due = due.count()
    # claims
    reg = registers._registered()
    claims_all = list(scope(user, Claim.objects.all(), producer_field="policy__producer"))
    registered = [c for c in claims_all if s <= reg.get(c.id, c.created_at.date()) <= e]
    large = sum(1 for c in registered if c.large_loss)
    os_tables = registers.claims_os(_fake(user, as_at=e.isoformat()))[0]
    os_total = os_tables[0].totals[10] if os_tables[0].totals else ZERO
    aged = sum((r[2] for r in os_tables[1].rows if r[0] in ("181-365", "Over 1 year")), ZERO)

    lr = _pct(cur["claims"], cur["earned"])
    prev_lr = _pct(prev["claims"], prev["earned"])
    exp = _pct(cur["comm"], cur["earned"])
    combined = (lr + exp) if (lr is not None and exp is not None) else None
    net_prem = cur["gwp"] - cur["ceded"]
    kpis = [
        {"key": "gwp", "label": "Written premium", "value": cur["gwp"], "prev": prev["gwp"], "unit": base, "good": "up"},
        {"key": "policies", "label": "Policies issued", "value": D(n_pol), "prev": D(n_prev), "unit": "", "good": "up", "int": True},
        {"key": "earned", "label": "Earned premium", "value": cur["earned"], "prev": prev["earned"], "unit": base, "good": "up"},
        {"key": "claims", "label": "Incurred claims", "value": cur["claims"], "prev": prev["claims"], "unit": base, "good": "down"},
        {"key": "lr", "label": "Loss ratio", "value": lr, "prev": prev_lr, "unit": "%", "good": "down"},
        {"key": "exp", "label": "Commission / expense ratio", "value": exp, "prev": _pct(prev["comm"], prev["earned"]), "unit": "%", "good": "down"},
        {"key": "cor", "label": "Combined ratio", "value": combined, "prev": ((prev_lr + _pct(prev["comm"], prev["earned"])) if prev_lr is not None and _pct(prev["comm"], prev["earned"]) is not None else None),
         "unit": "%", "good": "down"},
        {"key": "ceded", "label": "Premium ceded to reinsurers", "value": cur["ceded"], "prev": prev["ceded"], "unit": base, "good": None},
        {"key": "ri", "label": "Reinsurance recoverable on claims", "value": cur["ri_recoverable"], "prev": prev["ri_recoverable"], "unit": base, "good": None},
        {"key": "net", "label": "Net retained premium", "value": net_prem, "prev": prev["gwp"] - prev["ceded"], "unit": base, "good": "up"},
        {"key": "collect", "label": "Premium collected on new policies", "value": _pct(collected, invoiced), "prev": None, "unit": "%", "good": "up"},
        {"key": "retention", "label": "Renewal retention", "value": _pct(renewed, n_due), "prev": None, "unit": "%", "good": "up"},
        {"key": "reg", "label": "Claims registered", "value": D(len(registered)), "prev": None, "unit": "", "good": "down", "int": True},
        {"key": "os", "label": "Claims outstanding at period end", "value": os_total, "prev": None, "unit": base, "good": "down"},
    ]
    for k in kpis:
        k["change"] = _chg(k["value"], k["prev"]) if (k["value"] is not None and k["prev"] is not None and k["unit"] != "%") else None
        k["delta"] = (k["value"] - k["prev"]) if (k["unit"] == "%" and k["value"] is not None and k["prev"] is not None) else None

    def table(group, label, limit=None, sort="gwp"):
        g = _group(s, e, group)
        rows = []
        for name, m in g.items():
            if name is None:
                continue
            rows.append({"name": str(name), "gwp": m["gwp"], "earned": m["earned"], "claims": m["claims"], "comm": m["comm"], "ceded": m["ceded"], "ri": m["ri"],
                         "lr": _pct(m["claims"], m["earned"])})
        rows.sort(key=lambda r: -(r[sort] or 0))
        return rows[:limit] if limit else rows

    by_product = table("product", "Product")
    by_producer = table("producer", "Producer", 12)
    by_loss = []
    for name, m in _group(s, e, "loss").items():
        if name:
            n = sum(1 for c in registered if c.loss_type == name)
            by_loss.append({"name": dict(Claim.LOSS_TYPES).get(name, name), "claims": m["claims"], "ri": m["ri"], "count": n, "avg": (m["claims"] / n) if n else None})
    by_loss.sort(key=lambda r: -r["claims"])
    trend = _trend(s, e)
    ri_table = registers.ri_treaty(_fake(user, **{"from": s.isoformat(), "to": e.isoformat()}))[0][0]
    outl = []
    try:
        from budgeting import actuals as A
        from budgeting import engine, outliers
        b = engine.active_budget(e.year)
        for o in outliers.detect(b, e.year, A.last_closed(), s, e)[:8]:
            outl.append({"severity": o["severity"], "title": o["title"], "subject": o["subject"], "reason": o["reason"]})
    except Exception:       # the pack must never fail because of an optional analytic
        b = None
    bva = None
    try:
        if b:
            from budgeting import engine
            cut = (e.year, e.month) if e.year == date.today().year or e.year == e.year else None
            row = engine.analysis(b, e.year, "year", "GWP", cutoff=cut)["rows"][0]
            bva = {"version": b.name, "budget": row["budget_to_date"], "actual": row["actual"], "var_pct": row["var_pct"]}
    except Exception:
        bva = None

    insights = _insights(kpis, by_product, by_producer, cur, aged, large, outl, bva, n_due)
    return {"period": {"start": s, "end": e, "prev_start": ps, "prev_end": pe, "days": days}, "company": Company.get().name, "currency": base,
            "kpis": kpis, "insights": insights, "trend": trend, "by_product": by_product, "by_producer": by_producer, "by_loss": by_loss,
            "reinsurance": {"cols": [c for c, _ in ri_table.cols], "rows": ri_table.rows, "totals": ri_table.totals}, "outliers": outl, "budget": bva,
            "generated": date.today()}


def _insights(kpis, by_product, by_producer, cur, aged, large, outl, bva, n_due):
    K = {k["key"]: k for k in kpis}
    out = []

    def add(level, text):
        out.append({"level": level, "text": text})

    g = K["gwp"]
    if g["change"] is not None:
        add("good" if g["change"] >= 0 else "warn", f"Written premium is {abs(g['change']):.1f}% {'higher' if g['change'] >= 0 else 'lower'} than the previous period "
                                                    f"({g['value']:,.0f} against {g['prev']:,.0f} {g['unit']}).")
    lr = K["lr"]["value"]
    if lr is not None:
        lvl = "bad" if lr > 100 else "warn" if lr > TARGET_LR else "good"
        add(lvl, f"Loss ratio is {lr:.1f}% ({'above' if lr > TARGET_LR else 'within'} the {TARGET_LR}% working target)"
                 + (f", {abs(K['lr']['delta']):.1f} points {'worse' if K['lr']['delta'] > 0 else 'better'} than the previous period." if K["lr"]["delta"] is not None else "."))
    cor = K["cor"]["value"]
    if cor is not None and cor > 100:
        add("bad", f"Combined ratio is {cor:.1f}%: claims and commission together exceed the premium earned.")
    worst = [r for r in by_product if r["lr"] is not None and r["earned"] > 0]
    if len(worst) > 1:
        w, b = max(worst, key=lambda r: r["lr"]), min(worst, key=lambda r: r["lr"])
        add("warn" if w["lr"] > TARGET_LR else "info", f"{w['name']} has the highest loss ratio ({w['lr']:.1f}%); {b['name']} has the lowest ({b['lr']:.1f}%).")
    tot = sum((r["gwp"] for r in by_producer), ZERO)
    if by_producer and tot > 0 and by_producer[0]["gwp"] / tot > D("0.4"):
        add("warn", f"{by_producer[0]['name']} writes {by_producer[0]['gwp'] / tot * 100:.0f}% of the premium in this period: production is concentrated.")
    c = K["collect"]["value"]
    if c is not None and c < 80:
        add("warn", f"Only {c:.0f}% of the premium invoiced on new policies has been collected.")
    r = K["retention"]["value"]
    if r is not None and n_due and r < 70:
        add("warn", f"Renewal retention is {r:.0f}% of {n_due} policies that expired in the period.")
    if aged > 0:
        add("warn", f"{aged:,.0f} {K['gwp']['unit']} of outstanding claims is older than 180 days: review for settlement or release.")
    if large:
        add("info", f"{large} large loss{'es' if large != 1 else ''} registered in the period were flagged for excess-of-loss notification.")
    if cur["ri_recoverable"] and cur["ri_cash"] < cur["ri_recoverable"]:
        add("info", f"Reinsurance recoverable of {cur['ri_recoverable']:,.0f} was booked in the period; {cur['ri_cash']:,.0f} was received in cash.")
    if outl:
        highs = sum(1 for o in outl if o["severity"] == "high")
        add("warn" if highs else "info", f"{len(outl)} outlier finding{'s' if len(outl) != 1 else ''} (premium, claim size, claim pattern or producer) need review"
                                          + (f", {highs} of them high severity." if highs else "."))
    if bva and bva["var_pct"] is not None:
        v = bva["var_pct"]
        add("good" if v >= 0 else "warn", f"Premium is {abs(v):.1f}% {'ahead of' if v >= 0 else 'behind'} the {bva['version']} budget year to date.")
    if not out:
        add("info", "There is no activity in this period.")
    return out

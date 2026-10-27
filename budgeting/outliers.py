"""Outlier identification. Robust statistics only (median / MAD and Tukey fences), so a few extreme values cannot hide themselves
by inflating the mean and standard deviation.

Detectors
  variance   budget-vs-actual cells (product x metric x closed month) whose variance % is extreme against all other cells
  trend      monthly actuals that jump far from the usual level of their own series
  premium    policies priced far from comparable policies (same product and vehicle type): rate on sum insured / premium per vehicle
  severity   claims much more costly than other claims of the same loss type
  claims     early claims (within N days of inception), late reporting, repeat claims on one vehicle
  producer   agents / brokers whose loss ratio or average discount sits far from their peers
Each finding: {kind, severity, title, subject, link, value, expected, score, reason}."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from django.urls import reverse

from . import actuals as A
from .engine import STORED, budget_cube, pids
from .models import METRIC_SHORT, MONTHS

ZERO = Decimal("0")
DEFAULTS = {"z": 3.0, "iqr": 1.5, "min_var": 15.0, "early_days": 30, "late_days": 30}


def mad(values):
    m = median(values)
    return m, median(abs(v - m) for v in values) * 1.4826


def robust_z(x, med, sigma):
    if sigma <= 0:
        return 0.0
    return (x - med) / sigma


def fences(values, k):
    s = sorted(values)
    n = len(s)
    if n < 8:
        return None
    q1, q3 = s[n // 4], s[(3 * n) // 4]
    iqr = q3 - q1
    return q1 - k * iqr, q3 + k * iqr, median(s)


def _sev(score, thr):
    return "high" if score >= thr * 2 else "medium"


def f(x, nd=0):
    return f"{float(x):,.{nd}f}"


# ------------------------------------------------------------------------------------------------ variance & trend
def variance_outliers(version, year, cutoff, prm):
    if not version:
        return []
    act, bud = A.flat(year, year), budget_cube(version)
    names = dict(__import__("motor.models", fromlist=["Product"]).Product.objects.values_list("id", "name"))
    cells = []
    for p in pids():
        for met in STORED:
            for m in range(1, 13):
                if (year, m) > cutoff:
                    continue
                b, a = bud.get((p, met, m)) or ZERO, act.get((p, met, year, m), ZERO)
                if b > 0:
                    cells.append(((p, met, m), float((a - b) / b * 100), a, b))
    if len(cells) < 8:
        return []
    med, sg = mad([c[1] for c in cells])
    out = []
    for (p, met, m), pct, a, b in cells:
        z = robust_z(pct, med, sg)
        if abs(pct) >= prm["min_var"] and abs(z) >= prm["z"]:
            out.append({"kind": "variance", "severity": _sev(abs(z), prm["z"]), "title": f"{METRIC_SHORT[met]}: {pct:+.0f}% vs budget",
                        "subject": f"{names.get(p, p)} · {MONTHS[m - 1]} {year}", "link": reverse("budget_analytics") + f"?year={year}&metric={met}&mode=month&product={p}",
                        "value": f(a), "expected": f(b), "score": round(abs(z), 1),
                        "reason": f"Variance is {abs(z):.1f} robust deviations from the typical variance of {med:+.0f}% across all cells."})
    return out


def trend_outliers(cutoff, prm, months_back=24):
    names = dict(__import__("motor.models", fromlist=["Product"]).Product.objects.values_list("id", "name"))
    cy, cm = cutoff
    act = A.flat(cy - 2, cy)
    out = []
    keys = [(cy * 12 + cm - 1 - i) for i in range(months_back - 1, -1, -1)]
    for p in pids():
        for met in STORED:
            series = [(i, act.get((p, met, i // 12, i % 12 + 1), ZERO)) for i in keys]
            first = next((j for j, (_, v) in enumerate(series) if v), None)
            if first is None:
                continue
            series = series[first:]
            if len(series) < 8:
                continue
            vals = [float(v) for _, v in series]
            med, sg = mad(vals)
            for (i, v), x in zip(series, vals):
                z = robust_z(x, med, sg)
                if abs(z) >= prm["z"] and abs(x - med) > 0.25 * abs(med or 1):
                    out.append({"kind": "trend", "severity": _sev(abs(z), prm["z"]), "title": f"{METRIC_SHORT[met]} {'spike' if z > 0 else 'drop'}",
                                "subject": f"{names.get(p, p)} · {MONTHS[i % 12]} {i // 12}", "link": reverse("acc_transactions") + f"?from={i // 12}-{i % 12 + 1:02d}-01",
                                "value": f(x), "expected": f(med), "score": round(abs(z), 1),
                                "reason": f"{abs(z):.1f} robust deviations from the usual monthly level over the last {len(series)} active months."})
    return out


# ------------------------------------------------------------------------------------------------ transaction level
def premium_outliers(d_from, d_to, prm):
    from motor.models import Policy
    pols = Policy.objects.filter(issued_at__date__gte=d_from, issued_at__date__lte=d_to).select_related("product", "currency", "customer").prefetch_related("risks")
    groups = defaultdict(list)
    for p in pols:
        risks = list(p.risks.all())
        if not risks:
            continue
        r = risks[0]
        base_net = float(p.net_premium * p.fx_rate)
        if p.product.includes_od and r.sum_insured:
            val, label = base_net / float(r.sum_insured * p.fx_rate) / len(risks) * 100, "rate on sum insured %"
        else:
            val, label = base_net / len(risks), "premium per vehicle (OMR)"
        groups[(p.product.name, r.vehicle_type)].append((val, p, label))
    out = []
    for (prod, vt), items in groups.items():
        fe = fences([v for v, _, _ in items], prm["iqr"])
        if not fe:
            continue
        lo, hi, med = fe
        for v, p, label in items:
            if v > hi or v < lo:
                dist = (v - hi) / (hi - med + 1e-9) if v > hi else (lo - v) / (med - lo + 1e-9)
                out.append({"kind": "premium", "severity": "high" if dist > 1 else "medium", "title": f"Unusual {'high' if v > hi else 'low'} {label}",
                            "subject": f"{p.policy_no} · {p.customer.name} · {prod} / {vt.title()}", "link": reverse("policy_detail", args=[p.pk]),
                            "value": f(v, 2), "expected": f(med, 2), "score": round(1 + dist, 1),
                            "reason": f"Outside the normal range {lo:,.2f} – {hi:,.2f} for comparable {prod} {vt.lower()} policies. Check rating, discounts and sum insured."})
    return out


def claim_outliers(d_from, d_to, prm):
    from claims.models import Claim
    claims = Claim.objects.filter(date_of_loss__gte=d_from, date_of_loss__lte=d_to).select_related("policy__currency", "risk", "policy__customer")
    out, by_type = [], defaultdict(list)
    veh = defaultdict(list)
    for c in claims:
        base = float(c.gross_incurred * c.policy.fx_rate)
        by_type[c.loss_type].append((base, c))
        veh[c.risk_id].append(c)
    for lt, items in by_type.items():
        vals = [v for v, _ in items]
        fe = fences(vals, prm["iqr"])
        if not fe:
            continue
        lo, hi, med = fe
        med2, sg = mad(vals)
        for v, c in items:
            z = robust_z(v, med2, sg)
            if v > hi and z >= prm["z"] and v > 0:
                out.append({"kind": "severity", "severity": _sev(z, prm["z"]), "title": f"Costly {c.get_loss_type_display().lower()} claim",
                            "subject": f"{c.claim_no} · {c.risk.make} {c.risk.model} · {c.policy.customer.name}", "link": reverse("claim_detail", args=[c.pk]),
                            "value": f(v), "expected": f(med), "score": round(z, 1),
                            "reason": f"{z:.1f} robust deviations above the typical {c.get_loss_type_display().lower()} claim; fence {hi:,.0f} OMR." + (" Total loss." if c.is_total_loss else "")})
    for c in claims:
        days = (c.date_of_loss - c.policy.start_date).days
        base = float(c.gross_incurred * c.policy.fx_rate)
        if 0 <= days <= prm["early_days"] and base > 0:
            out.append({"kind": "claims", "severity": "high" if days <= 3 else "medium", "title": f"Early claim: {days} days after inception",
                        "subject": f"{c.claim_no} · {c.policy.policy_no} · {c.policy.customer.name}", "link": reverse("claim_detail", args=[c.pk]),
                        "value": f(base), "expected": "—", "score": round(max(1, (prm["early_days"] - days) / 5 + 1), 1),
                        "reason": "Loss within the first weeks of cover: verify pre-existing damage and the policy start."})
        lag = (c.created_at.date() - c.date_of_loss).days
        if lag > prm["late_days"]:
            out.append({"kind": "claims", "severity": "medium", "title": f"Late reported: {lag} days after the loss",
                        "subject": f"{c.claim_no} · {c.policy.policy_no}", "link": reverse("claim_detail", args=[c.pk]), "value": f"{lag} days", "expected": f"≤ {prm['late_days']}",
                        "score": round(lag / prm["late_days"], 1), "reason": "Late notification weakens the investigation: check the reason."})
    for rid, cs in veh.items():
        if len(cs) >= 2:
            cs.sort(key=lambda c: c.date_of_loss)
            for a, b in zip(cs, cs[1:]):
                if (b.date_of_loss - a.date_of_loss).days <= 365:
                    out.append({"kind": "claims", "severity": "medium", "title": "Repeat claim on the same vehicle within 12 months",
                                "subject": f"{b.claim_no} (after {a.claim_no}) · {b.risk.make} {b.risk.model}", "link": reverse("claim_detail", args=[b.pk]),
                                "value": f"{(b.date_of_loss - a.date_of_loss).days} days apart", "expected": "—", "score": 2.0,
                                "reason": "Frequency outlier: review the vehicle, driver and repair history."})
                    break
    return out


def producer_outliers(d_from, d_to, prm, min_policies=8):
    from claims.models import Claim
    from motor.models import Policy
    pols = Policy.objects.filter(issued_at__date__gte=d_from, issued_at__date__lte=d_to).select_related("producer", "proposal")
    stat = defaultdict(lambda: {"n": 0, "prem": 0.0, "disc": 0.0, "claims": 0.0})
    for p in pols:
        s = stat[p.producer_id]
        s["n"] += 1
        s["prem"] += float(p.net_premium * p.fx_rate)
        s["disc"] += float(p.proposal.discount_pct or 0)
        s["name"] = p.producer.get_username()
    for c in Claim.objects.filter(policy__in=pols).select_related("policy"):
        stat[c.policy.producer_id]["claims"] += float(c.gross_incurred * c.policy.fx_rate)
    rows = {k: v for k, v in stat.items() if v["n"] >= min_policies and v["prem"] > 0}
    out = []
    for label, fn, unit in (("loss ratio", lambda v: v["claims"] / v["prem"] * 100, "%"), ("average discount", lambda v: v["disc"] / v["n"], "%")):
        vals = {k: fn(v) for k, v in rows.items()}
        if len(vals) < 4:
            continue
        med, sg = mad(list(vals.values()))
        for k, x in vals.items():
            z = robust_z(x, med, sg)
            if abs(z) >= prm["z"] * 0.7 and (x - med) > 0:
                out.append({"kind": "producer", "severity": _sev(abs(z), prm["z"] * 0.7), "title": f"High {label} for {rows[k]['name']}",
                            "subject": f"{rows[k]['name']} · {rows[k]['n']} policies", "link": reverse("premium_register"), "value": f"{x:.1f}{unit}", "expected": f"{med:.1f}{unit}",
                            "score": round(abs(z), 1), "reason": f"{abs(z):.1f} robust deviations above the peer median {label}."})
    return out


KINDS = [("variance", "Budget variance"), ("trend", "Monthly trend"), ("premium", "Premium pricing"), ("severity", "Claim severity"), ("claims", "Claim patterns"),
         ("producer", "Producers")]


def detect(version, year, cutoff, d_from, d_to, prm=None):
    prm = {**DEFAULTS, **(prm or {})}
    found = (variance_outliers(version, year, cutoff, prm) + trend_outliers(cutoff, prm) + premium_outliers(d_from, d_to, prm)
             + claim_outliers(d_from, d_to, prm) + producer_outliers(d_from, d_to, prm))
    found.sort(key=lambda o: (o["severity"] != "high", -o["score"]))
    return found

"""Loss ratio by insured, by vehicle, or by insured and vehicle (from the IFRS 17 ledger).

Earned premium = revenue recognised (4110) on the policy in the period, shared across the policy's vehicles pro-rata to their net premium.
Incurred claims = paid in the period + OS at the end - OS at the start (gross of reinsurance, net of excess).
Loss ratio = incurred / earned.
"""
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum

from accounting.models import JournalLine
from claims.models import Claim
from core.permissions import scope

from .models import Customer, Policy, Vehicle

D = Decimal
ZERO = D("0")
GROUPS = [("insured", "By insured"), ("vehicle", "By vehicle"), ("both", "By insured and vehicle")]


def _by_policy(account, types, s, e):
    qs = JournalLine.objects.filter(account_id=account, entry__entry_type__in=types, entry__policy__isnull=False, entry__date__gte=s, entry__date__lte=e)
    return {r["entry__policy_id"]: (r["c"] or ZERO) - (r["d"] or ZERO) for r in qs.values("entry__policy_id").annotate(c=Sum("credit"), d=Sum("debit"))}


def compute(user, s, e, group="insured", customer=None, vehicle=None):
    from .registers import _cr, _dr, _ledger, _registered

    pols = scope(user, Policy.objects.all()).select_related("customer").prefetch_related("risks")
    claims = scope(user, Claim.objects.all(), producer_field="policy__producer").select_related("policy__customer", "risk")
    if customer:
        pols, claims = pols.filter(customer_id=customer), claims.filter(policy__customer_id=customer)
    if vehicle:
        pols, claims = pols.filter(risks__vehicle_id=vehicle).distinct(), claims.filter(risk__vehicle_id=vehicle)
    pmap = {p.id: p for p in pols}
    earned = _by_policy("4110", ["EARN"], s, e)
    written = _by_policy("2110", ["ISSUE", "ENDORSE"], s, e)

    def key(cust, veh):
        return (cust,) if group == "insured" else (veh,) if group == "vehicle" else (cust, veh)

    acc = OrderedDict()

    def A(k):
        return acc.setdefault(k, {"policies": set(), "written": ZERO, "earned": ZERO, "incurred": ZERO, "paid": ZERO, "os": ZERO, "claims": 0, "cust": k[0] if group != "vehicle" else None})

    for amounts, field in ((earned, "earned"), (written, "written")):
        for pid, amt in amounts.items():
            p = pmap.get(pid)
            if not p:
                continue
            risks = [r for r in p.risks.all() if r.active] or list(p.risks.all())
            tot = sum((r.net_premium for r in risks), ZERO)
            for r in risks:
                if vehicle and r.vehicle_id != vehicle:
                    continue
                share = (r.net_premium / tot) if tot else D(1) / D(len(risks))
                a = A(key(p.customer_id, r.vehicle_id))
                a[field] += amt * share
                a["policies"].add(pid)
    open_d = s - timedelta(days=1)
    o = _ledger(["2120"], d_to=open_d)
    c_ = _ledger(["2120"], d_to=e)
    paid = _ledger(["2120", "5110"], types=["CLAIM_PAY"], d_from=s, d_to=e)
    reg = _registered()
    for c in claims:
        cid = c.id
        os_o, os_c = _cr(o, cid, "2120") - _dr(o, cid, "2120"), _cr(c_, cid, "2120") - _dr(c_, cid, "2120")
        p_ = _dr(paid, cid, "2120") + _dr(paid, cid, "5110")
        inc = p_ + os_c - os_o
        r_date = reg.get(cid, c.created_at.date())
        if not any((os_o, os_c, p_, inc)) and not (s <= r_date <= e):
            continue
        a = A(key(c.policy.customer_id, c.risk.vehicle_id))
        a["incurred"] += inc
        a["paid"] += p_
        a["os"] += os_c
        a["claims"] += 1 if s <= r_date <= e else 0
    cust = {x.id: x for x in Customer.objects.filter(id__in={k[0] for k in acc} if group != "vehicle" else [])}
    vids = {k[-1] for k in acc} if group != "insured" else set()
    veh = {x.id: x for x in Vehicle.objects.select_related("customer").filter(id__in=vids)}
    rows = []
    for k, a in acc.items():
        row = dict(a)
        row["n_policies"] = len(a["policies"])
        row["loss_ratio"] = (a["incurred"] / a["earned"] * 100) if a["earned"] > 0 else None
        if group in ("insured", "both"):
            c = cust.get(k[0])
            row["insured"] = c.name if c else "—"
            row["insured_id"] = c.identifier if c else ""
        if group in ("vehicle", "both"):
            v = veh.get(k[-1])
            row["vehicle"] = f"{v.make} {v.model} {v.year}" if v else "—"
            row["plate"] = v.registration if v else ""
            row["chassis"] = v.chassis_no if v else ""
            row["owner"] = v.customer.name if v else ""
            row["vehicle_pk"] = v.pk if v else None
        if group in ("insured", "both"):
            row["customer_pk"] = k[0]
        rows.append(row)
    return rows


def totals(rows):
    t = {f: sum((r[f] for r in rows), ZERO) for f in ("written", "earned", "incurred", "paid", "os")}
    t["claims"] = sum(r["claims"] for r in rows)
    t["loss_ratio"] = (t["incurred"] / t["earned"] * 100) if t["earned"] > 0 else None
    return t

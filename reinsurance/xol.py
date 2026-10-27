"""Non-proportional reinsurance beyond per-risk XoL: catastrophe XoL, aggregate XoL, stop loss, plus burning-cost pricing.

All amounts are in base currency. These covers respond to totals (an event, a year), so they are computed here, stored as
``AggRecovery`` rows and posted to the ledger (1230 reinsurance asset / 4210 recoveries) as the amount changes.
The sequence is: quota share, surplus, per-risk XoL (claim by claim), then cat XoL (per event), then aggregate XoL and stop loss (per period).
"""
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from core import clock
from core.models import to_base

D = Decimal
ZERO = D("0")
H = D(100)


def q3(x):
    return D(x).quantize(D("0.001"), rounding=ROUND_HALF_UP)


def layer(loss, retention, limit):
    """Part of ``loss`` above ``retention``, up to ``limit`` (0 = unlimited)."""
    x = max(D(loss) - D(retention), ZERO)
    return min(x, D(limit)) if limit else x


def _rl(c):
    """Retained loss of a motor or P&C claim, base currency."""
    if hasattr(c, "cat_event") and c.__class__.__name__ == "PncClaim":
        from pnc.services import retained_loss
    else:
        from claims.services import retained_loss
    return retained_loss(c)


def _split(c):
    """(net after salvage, recovered by proportional treaties, recovered by per-risk XoL) for a motor or P&C claim."""
    if c.__class__.__name__ == "PncClaim":
        from pnc import services as ps
        base = ps.incurred_base(c)
        net = max(base - to_base(ps.salvage_total(c), c.policy.currency), ZERO)
        lay = ps.claim_layers(c, base)
        return net, sum((a for _, k, a in lay if k != "XOL"), ZERO), sum((a for _, k, a in lay if k == "XOL"), ZERO)
    from claims.services import retained_split
    return retained_split(c)


def _claims_for(business, start, end, event=None):
    if business == "PNC":
        from pnc.models import PncClaim
        qs = PncClaim.objects.filter(date_of_loss__gte=start, date_of_loss__lte=end).exclude(status=PncClaim.REJECTED).select_related("policy__currency", "risk")
        return qs.filter(cat_event=event) if event is not None else qs
    from claims.models import Claim
    qs = Claim.objects.filter(date_of_loss__gte=start, date_of_loss__lte=end).exclude(status=Claim.REJECTED).select_related("policy__currency", "risk")
    return qs.filter(cat_event=event) if event is not None else qs


def _claims_in(treaty, event=None):
    if True:
        return _claims_for(treaty.business, treaty.effective_from, treaty.effective_to, event)
    from claims.models import Claim
    qs = Claim.objects.filter(date_of_loss__gte=treaty.effective_from, date_of_loss__lte=treaty.effective_to).exclude(status=Claim.REJECTED).select_related("policy__currency", "risk")
    if event is not None:
        qs = qs.filter(cat_event=event)
    return qs


def subject_premium(start, end, business="MOTOR"):
    """Retained net premium written in the period: net premium less premium ceded to proportional treaties."""
    if business == "PNC":
        from pnc.models import PncCession, PncPolicy
        pols = PncPolicy.objects.filter(issued_at__date__gte=start, issued_at__date__lte=end)
        gross = sum((to_base(p.net_premium, p.currency) for p in pols), ZERO)
        ceded = PncCession.objects.filter(policy__in=pols, treaty_type__in=["QUOTA", "SURPLUS"], is_facultative=False).aggregate(s=Sum("premium_ceded"))["s"] or ZERO
        return gross, D(ceded)
    from motor.models import Policy
    from reinsurance.models import Cession
    pols = Policy.objects.filter(issued_at__date__gte=start, issued_at__date__lte=end)
    gross = sum((to_base(p.net_premium, p.currency) for p in pols), ZERO)
    ceded = Cession.objects.filter(policy__in=pols, treaty_type__in=["QUOTA", "SURPLUS"], is_facultative=False).aggregate(s=Sum("premium_ceded"))["s"] or ZERO
    return gross, D(ceded)


def cat_position(treaty, event):
    """Cat XoL for one event: retained loss of every claim in the event, above the retention, up to the limit."""
    rows = [(c, _rl(c)) for c in _claims_in(treaty, event)]
    total = sum((r for _, r in rows), ZERO)
    rec = layer(total, treaty.xol_retention, treaty.xol_limit)
    return _result(treaty, "CAT", f"{event.code} {event.name}", total, rec, rows=rows)


def period_retained(treaty):
    """Retained loss of the treaty period after per-risk XoL and after the cat XoL recoveries of the events in it."""
    from reinsurance.models import AggRecovery
    total = sum((_rl(c) for c in _claims_in(treaty)), ZERO)
    rel = "pnc_claims" if treaty.business == "PNC" else "claims"
    cat = AggRecovery.objects.filter(kind="CAT", treaty__business=treaty.business, **{f"event__{rel}__date_of_loss__gte": treaty.effective_from, f"event__{rel}__date_of_loss__lte": treaty.effective_to}).distinct().aggregate(s=Sum("recovery"))["s"] or ZERO
    return max(total - D(cat), ZERO)


def aggregate_position(treaty):
    total = period_retained(treaty)
    rec = layer(total, treaty.aad, treaty.aal)
    return _result(treaty, "AGGREGATE", f"Aggregate XoL {treaty.effective_from:%Y}", total, rec)


def stop_loss_position(treaty):
    total = period_retained(treaty)
    gross, ceded = subject_premium(treaty.effective_from, treaty.effective_to, treaty.business)
    subj = max(gross - ceded, ZERO)
    lr = (total / subj * H) if subj else ZERO
    rec = ZERO
    if subj:
        rec = min(max(lr - treaty.sl_attach_pct, ZERO), treaty.sl_limit_pct or H) / H * subj
    out = _result(treaty, "STOP_LOSS", f"Stop loss {treaty.effective_from:%Y}", total, rec, subject=subj)
    out["loss_ratio"] = lr
    return out


def _result(treaty, kind, label, total, rec, rows=None, subject=ZERO):
    rec = q3(rec)
    reinst = ZERO
    if treaty.xol_limit and kind == "CAT" and rec:
        used = min(rec / treaty.xol_limit, D(treaty.reinstatements))
        reinst = q3(used * treaty.reinstatement_pct / H * treaty.mdp)
    elif kind == "AGGREGATE" and treaty.aal and rec:
        reinst = q3(min(rec / treaty.aal, D(treaty.reinstatements)) * treaty.reinstatement_pct / H * treaty.mdp)
    return {"kind": kind, "label": label, "retained": q3(total), "recovery": rec, "subject": q3(subject), "reinstatement": reinst, "rows": rows or []}


def position(treaty, event=None):
    if treaty.xol_kind == treaty.CAT:
        return cat_position(treaty, event) if event else None
    if treaty.xol_kind == treaty.AGGREGATE:
        return aggregate_position(treaty)
    if treaty.xol_kind == treaty.STOP_LOSS:
        return stop_loss_position(treaty)
    return None


@transaction.atomic
def post_position(treaty, event=None, user=None):
    """Store the position and post the change in the recoverable to the ledger. Returns the AggRecovery."""
    from accounting import services as acc
    from reinsurance.models import AggRecovery
    pos = position(treaty, event)
    if pos is None:
        return None
    row, _ = AggRecovery.objects.get_or_create(treaty=treaty, event=event, defaults={"kind": pos["kind"], "label": pos["label"][:160]})
    delta = pos["recovery"] - row.posted
    if delta:
        acc.post("XOL_REC", f"{treaty.reference}: {pos['label']} - XoL recovery", [("1230", "D", delta), ("4210", "C", delta)],
                 source=treaty.reference, user=user)
    row.kind, row.label, row.retained_loss, row.subject_premium = pos["kind"], pos["label"][:160], pos["retained"], pos["subject"]
    row.recovery, row.posted, row.reinstatement_premium = pos["recovery"], pos["recovery"], pos["reinstatement"]
    row.save()
    return row


@transaction.atomic
def receive_cash(row, amount, user=None):
    from accounting import services as acc
    amount = q3(amount)
    if amount <= 0 or amount > row.due + D("0.0005"):
        raise ValueError(f"Enter an amount up to the balance due ({row.due}).")
    acc.post("XOL_REC", f"{row.treaty.reference}: {row.label} - cash received from reinsurer", [("1110", "D", amount), ("1230", "C", amount)],
             source=row.treaty.reference, user=user)
    row.received += amount
    row.save(update_fields=["received"])
    return row


# ---- burning cost -------------------------------------------------------------------------------------------------
def burning_cost(treaty, years=None, today=None):
    """Price the cover from its own history. For each loss year: the losses the layer would have paid / the subject premium of that year.

    Burning cost rate = total layer losses / total subject premium. Indicated rate = burning cost x loading, held between the minimum and maximum rate."""
    retained_loss, retained_split = _rl, _split

    today = today or clock.today()
    years = years or treaty.bc_years or 5
    kind = treaty.xol_kind if treaty.treaty_type == "XOL" else "RISK"
    table = []
    for y in range(today.year - years + 1, today.year + 1):
        from datetime import date
        start, end = date(y, 1, 1), date(y, 12, 31)
        gross, ceded = subject_premium(start, end, treaty.business)
        subj = gross if kind == "RISK" else max(gross - ceded, ZERO)
        claims = list(_claims_for(treaty.business, start, end))
        if kind == "RISK":
            loss = sum((layer(max(n - p, ZERO), treaty.xol_retention, treaty.xol_limit) for n, p, _ in (retained_split(c) for c in claims)), ZERO)
        elif kind == "CAT":
            by_event = {}
            for c in claims:
                if c.cat_event_id:
                    by_event[c.cat_event_id] = by_event.get(c.cat_event_id, ZERO) + retained_loss(c)
            loss = sum((layer(v, treaty.xol_retention, treaty.xol_limit) for v in by_event.values()), ZERO)
        elif kind == "AGGREGATE":
            loss = layer(sum((retained_loss(c) for c in claims), ZERO), treaty.aad, treaty.aal)
        else:
            tot = sum((retained_loss(c) for c in claims), ZERO)
            lr = (tot / subj * H) if subj else ZERO
            loss = min(max(lr - treaty.sl_attach_pct, ZERO), treaty.sl_limit_pct or H) / H * subj
        table.append({"year": y, "subject": q3(subj), "loss": q3(loss), "rate": q3(loss / subj * H) if subj else ZERO})
    tl, ts = sum((r["loss"] for r in table), ZERO), sum((r["subject"] for r in table), ZERO)
    bc = (tl / ts * H) if ts else ZERO
    ind = bc * (treaty.bc_loading or D(1))
    if treaty.bc_min_rate:
        ind = max(ind, treaty.bc_min_rate)
    if treaty.bc_max_rate:
        ind = min(ind, treaty.bc_max_rate)
    base = treaty.est_gnrpi
    return {"table": table, "burning_cost": q3(bc), "indicated_rate": q3(ind), "current_rate": treaty.rate_pct,
            "indicated_premium": q3(base * ind / H), "current_premium": q3(base * treaty.rate_pct / H), "loading": treaty.bc_loading, "years": years}

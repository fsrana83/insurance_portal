"""Reinsurance Program Engine.

Walks the active treaties in ``rank`` order and distributes every risk of a policy:

* Quota Share  -> fixed % ceded (optionally only for the covers the treaty lists)
* Surplus      -> everything above the retention, up to the treaty capacity
* Facultative  -> anything above treaty capacity is flagged; the policy is blocked from automatic
                  binding (status PENDING_FAC) until a manual reinsurer placement is confirmed
* XoL          -> non-proportional. Premium (rate x net premium) is accrued on every policy and posted with it; claim recoveries
                  (loss above retention, up to the limit) are posted with the claim; deposit instalments and the minimum-premium
                  top-up are posted from the treaty page (see xol_* helpers)

Amounts are stored in the base currency.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from core.models import to_base

from .models import Cession, MDPInstallment, Treaty

D = Decimal
ZERO = D("0")
HUNDRED = D("100")


def q3(x):
    return D(x).quantize(D("0.001"), rounding=ROUND_HALF_UP)


def _parts_for(treaty, parts):
    """Which premium parts (OD/TP/RIDER) a treaty applies to."""
    codes = list(treaty.covers.values_list("code", "kind"))
    if not codes:
        return set(parts)
    out = set()
    for code, kind in codes:
        out.add(code if code in ("OD", "TP") else kind)
    return out & set(parts)


def active_treaties(on_date, treaty_types=None, business="MOTOR"):
    qs = Treaty.objects.filter(active=True, superseded_by__isnull=True, effective_from__lte=on_date, effective_to__gte=on_date, business=business)
    if treaty_types:
        qs = qs.filter(treaty_type__in=treaty_types)
    return list(qs.order_by("rank", "id"))


def risk_exposure(policy, risk):
    """Sum insured exposed to the treaty program, in base currency (TP-only uses the TP limit)."""
    if policy.product.includes_od:
        return to_base(risk.sum_insured, policy.currency)
    return D(policy.product.tp_limit)


@transaction.atomic
def distribute_policy(policy, replace=False):
    if replace:
        policy.cessions.all().delete()
    treaties = active_treaties(policy.start_date, [Treaty.QUOTA, Treaty.SURPLUS])
    needs_fac = False
    for risk in policy.risks.filter(active=True):
        exposure = risk_exposure(policy, risk)
        parts = {"OD": to_base(risk.od_premium, policy.currency),
                 "TP": to_base(risk.tp_premium, policy.currency),
                 "RIDER": to_base(risk.rider_premium, policy.currency)}
        total_prem = sum(parts.values(), ZERO)
        retained_si = exposure
        retained = dict(parts)
        # NB the expense load is part of the net premium; cede on the net premium proportionally
        scale = to_base(risk.net_premium, policy.currency) / total_prem if total_prem else D(1)
        retained = {k: v * scale for k, v in retained.items()}
        for t in treaties:
            applicable = _parts_for(t, retained)
            if not applicable:
                continue
            if t.treaty_type == Treaty.QUOTA:
                pct = t.cession_pct / HUNDRED
                ceded_prem = sum((retained[p] for p in applicable), ZERO) * pct
                for p in applicable:
                    retained[p] -= retained[p] * pct
                if "OD" in applicable:
                    ceded_si = retained_si * pct
                    retained_si -= ceded_si
                else:  # TP / rider-only quota share: report the share of the TP limit, vehicle value unaffected
                    ceded_si = D(policy.product.tp_limit) * pct
                Cession.objects.create(policy=policy, risk=risk, treaty=t, treaty_type=t.treaty_type, rank=t.rank,
                                       sum_insured_ceded=q3(ceded_si), premium_ceded=q3(ceded_prem), cession_pct=t.cession_pct)
            elif t.treaty_type == Treaty.SURPLUS:
                if retained_si <= t.retention or retained_si <= 0:
                    continue
                surplus = retained_si - t.retention
                ceded_si = min(surplus, t.capacity) if t.capacity else surplus
                ratio = ceded_si / retained_si
                prem_base = sum((retained[p] for p in applicable), ZERO)
                ceded_prem = prem_base * ratio
                for p in applicable:
                    retained[p] -= retained[p] * ratio
                retained_si -= ceded_si
                Cession.objects.create(policy=policy, risk=risk, treaty=t, treaty_type=t.treaty_type, rank=t.rank,
                                       sum_insured_ceded=q3(ceded_si), premium_ceded=q3(ceded_prem), cession_pct=q3(ratio * HUNDRED),
                                       note=f"Retention {t.retention}")
                overflow = surplus - ceded_si
                if overflow > 0:
                    needs_fac = True
                    fac_ratio = overflow / (retained_si + overflow) if (retained_si + overflow) else ZERO
                    fac_prem = prem_base * fac_ratio
                    Cession.objects.create(policy=policy, risk=risk, treaty=None, treaty_type="FAC", rank=t.rank + 1,
                                           sum_insured_ceded=q3(overflow), premium_ceded=q3(fac_prem), cession_pct=q3(fac_ratio * HUNDRED),
                                           is_facultative=True, note="Exceeds automatic treaty capacity — facultative placement required")
                    retained_si -= overflow
    for risk in policy.risks.filter(active=True):
        for t in active_treaties(policy.start_date, [Treaty.XOL]):
            prem = to_base(risk.net_premium, policy.currency) * t.rate_pct / HUNDRED
            if prem:
                Cession.objects.create(policy=policy, risk=risk, treaty=t, treaty_type=Treaty.XOL, rank=t.rank, sum_insured_ceded=ZERO,
                                       premium_ceded=q3(prem), cession_pct=t.rate_pct, note="XoL premium = rate x net premium (GNRPI)")
    if needs_fac:
        policy.status = policy.PENDING_FAC
    elif policy.status == policy.PENDING_FAC and not policy.cessions.filter(is_facultative=True, fac_confirmed=False).exists():
        policy.status = policy.ACTIVE
    policy.save(update_fields=["status"])
    return needs_fac


def confirm_facultative(cession, reinsurer):
    cession.fac_reinsurer = reinsurer
    cession.fac_confirmed = True
    cession.save()
    policy = cession.policy
    if not policy.cessions.filter(is_facultative=True, fac_confirmed=False).exists():
        policy.status = policy.ACTIVE
        policy.save(update_fields=["status"])


# ---------------------------------------------------------------------------
# Treaty versioning
# ---------------------------------------------------------------------------
@transaction.atomic
def modify_treaty(treaty, user_note="", **changes):
    """Active treaties are never edited in place: create a new version with an effective date (addendum 2.2)."""
    from claims.models import Claim
    from motor.models import Policy

    new = Treaty.objects.get(pk=treaty.pk)
    new.pk = None
    new.version = Treaty.objects.filter(reference=treaty.reference).count() + 1
    for k, v in changes.items():
        setattr(new, k, v)
    new.amendment_note = user_note
    new.superseded_by = None
    new.save()
    new.covers.set(treaty.covers.all())
    treaty.superseded_by = new
    treaty.save(update_fields=["superseded_by"])
    return new, retroactive_warnings(new)


def retroactive_warnings(treaty):
    from claims.models import Claim
    from motor.models import Policy

    eff = treaty.effective_from
    warnings = []
    unearned = Policy.objects.filter(status__in=["ACTIVE", "PENDING_FAC"], start_date__lt=eff, end_date__gte=eff).count()
    if unearned:
        warnings.append(f"{unearned} policies written before {eff:%d %b %Y} still have unearned premium that this amendment may affect.")
    open_claims = Claim.objects.filter(date_of_loss__lt=eff).exclude(status__in=["CLOSED", "REJECTED"]).count()
    if open_claims:
        warnings.append(f"{open_claims} open claims with a loss date before {eff:%d %b %Y} may be affected.")
    return warnings


# ---------------------------------------------------------------------------
# MDP (Minimum & Deposit Premium) for non-proportional treaties
# ---------------------------------------------------------------------------
@transaction.atomic
def schedule_mdp(treaty):
    treaty.mdp_installments.all().delete()
    n = max(treaty.installments, 1)
    amount = q3(treaty.mdp / n)
    span = (treaty.effective_to - treaty.effective_from).days
    for i in range(n):
        due = treaty.effective_from.fromordinal(treaty.effective_from.toordinal() + int(span * i / n))
        MDPInstallment.objects.create(treaty=treaty, due_date=due, amount=amount)


def mdp_adjustment(treaty, actual_gnrpi):
    """Additional premium due at expiry: only if actual GNRPI x rate > MDP (no refund — it is a minimum)."""
    earned = D(actual_gnrpi) * treaty.rate_pct / HUNDRED
    return max(earned - treaty.mdp, ZERO), earned


def actual_gnrpi(treaty):
    """Actual GNRPI from policies written in the treaty period (base currency)."""
    from motor.models import Policy

    if treaty.business == "PNC":
        from pnc.models import PncPolicy
        pols = PncPolicy.objects.filter(issued_at__date__gte=treaty.effective_from, issued_at__date__lte=treaty.effective_to)
        return sum((to_base(p.net_premium, p.currency) for p in pols), ZERO)
    pols = Policy.objects.filter(issued_at__date__gte=treaty.effective_from, issued_at__date__lte=treaty.effective_to)
    return sum((to_base(p.net_premium, p.currency) for p in pols), ZERO)


def capacity_utilisation(treaty):
    used = Cession.objects.filter(treaty__reference=treaty.reference, treaty_type="SURPLUS",
                                  policy__status__in=["ACTIVE", "PENDING_FAC"]).aggregate(s=Sum("sum_insured_ceded"))["s"] or ZERO
    cap = treaty.capacity
    return used, cap, (used / cap * HUNDRED if cap else ZERO)


def large_loss_flag(claim, base_gross):
    """Flag single losses that breach an XoL retention for specific reinsurance notification."""
    for t in active_treaties(claim.date_of_loss, [Treaty.XOL]):
        if base_gross > t.xol_retention:
            return True, t
    return False, None


# ---------------------------------------------------------------------------
# XoL: premium position, instalments and minimum-premium top-up (auto-posted)
# ---------------------------------------------------------------------------
def xol_position(treaty):
    """Premium picture for an XoL treaty (all versions of the reference share the same cessions)."""
    from accounting.models import JournalLine

    accrued = Cession.objects.filter(treaty__reference=treaty.reference, treaty_type=Treaty.XOL).aggregate(s=Sum("premium_ceded"))["s"] or ZERO
    gnrpi = actual_gnrpi(treaty)
    minimum = treaty.mdp
    final = max(accrued, minimum)
    paid = MDPInstallment.objects.filter(treaty__reference=treaty.reference, paid=True).aggregate(s=Sum("amount"))["s"] or ZERO
    src = f"XOLADJ-{treaty.reference}"
    posted = JournalLine.objects.filter(account_id="2230", entry__source=src).aggregate(s=Sum("credit"))["s"] or ZERO
    topup = max(minimum - accrued, ZERO)
    return {"gnrpi": gnrpi, "accrued": accrued, "minimum": minimum, "final": final, "paid": paid, "balance": max(accrued, minimum) - paid,
            "topup": topup, "topup_posted": posted, "topup_due": q3(topup - posted), "ended": treaty.effective_to < date.today()}


@transaction.atomic
def pay_installment(inst, user=None, paid_on=None, reference=""):
    """Mark a deposit instalment as paid and post it (Dr reinsurance premium payable / Cr bank)."""
    from accounting import services as acc
    from core import clock

    if inst.paid:
        return None
    inst.paid, inst.paid_on, inst.reference = True, paid_on or clock.today(), reference
    inst.save(update_fields=["paid", "paid_on", "reference"])
    return acc.on_ri_premium_paid(inst.treaty, inst.amount, f"MDP-{inst.treaty.reference}-{inst.pk}", user)


@transaction.atomic
def post_xol_adjustment(treaty, user=None, force=False):
    """Lift the accrued premium to the minimum premium (only once the treaty has expired unless forced). Idempotent."""
    from accounting import services as acc

    pos = xol_position(treaty)
    if pos["topup_due"] <= 0 or not (pos["ended"] or force):
        return None
    return acc.on_xol_minimum_topup(treaty, pos["topup_due"], f"XOLADJ-{treaty.reference}", user)

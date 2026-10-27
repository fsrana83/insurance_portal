"""Claims logic: excess, 70/30 repair payments, FSA repair-delay compensation, subrogation, recoveries, ratings."""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core import clock
from core.models import Currency, DocumentSequence, convert, to_base, user_role

from .models import (CatEvent, Claim, ClaimAdjustment, ClaimPayment, Injury, LegalAction, Provider, ProviderRating, Recovery, RepairEstimate, SubroNotice, Subrogation,
                     TreatyRecovery)

D = Decimal
ZERO = D("0")


class ClaimError(Exception):
    pass


def q(cur, x):
    return cur.quantize(D(x))


def total_loss_value(claim_or_risk, policy, date_of_loss):
    """Market value of the vehicle at the date of loss (reducing balance), capped at the sum insured. Policy currency."""
    from motor.services import vehicle_value

    risk = claim_or_risk
    val, note = vehicle_value(risk.vehicle, date_of_loss, policy.product, policy.currency, policy.fx_rate,
                              fallback_si=risk.sum_insured, fallback_date=policy.start_date)
    if val is None:
        val = risk.sum_insured
    return min(val, risk.sum_insured) if risk.sum_insured else val


def next_claim_no():
    return f"CLM/{clock.today().year}/{DocumentSequence.next('CLAIM'):06d}"


def suggest_surveyor(loss_type, governorate):
    qs = Provider.objects.filter(provider_type=Provider.SURVEYOR, active=True)
    scored = []
    for p in qs:
        score = 0
        if governorate and p.governorate.lower() == governorate.lower():
            score += 2
        if loss_type in (p.specialities or "").split(","):
            score += 2
        if p.preferred:
            score += 1
        scored.append((score, p.average_score, p))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2] if scored else None


@transaction.atomic
def register_claim(policy, risk, user, **data):
    if policy.status not in ("ACTIVE", "PENDING_FAC", "CANCELLED", "EXPIRED"):
        raise ClaimError("Policy is not in force.")
    dol = data["date_of_loss"]
    cancelled = policy.cancelled_on
    if dol < policy.start_date or dol > (cancelled or policy.end_date):
        raise ClaimError(f"Date of loss {dol:%d %b %Y} falls outside the period of cover.")
    sub_data = {k: data.pop(k, "") for k in ("liable_party", "liable_insurer", "liable_insurer_email", "their_policy_no", "their_vehicle", "their_claim_ref")}
    driver = data.get("driver")
    if driver is not None:                      # a registered additional driver: age and young-driver flag follow the licence holder
        data["driver_age"] = driver.age_on(dol)
        data["young_driver"] = data["driver_age"] < settings.YOUNG_DRIVER_AGE
        data["undisclosed_driver"] = False
    typed = data.get("estimated_damage")
    data["estimated_damage"] = typed or ZERO
    claim = Claim(claim_no=next_claim_no(), policy=policy, risk=risk, registered_by=user, **data)
    if claim.is_total_loss:
        claim.depreciated_value = total_loss_value(risk, policy, dol)
        claim.estimated_damage = claim.depreciated_value
    # total cost estimated from the details given; a figure typed by the handler overrides it
    compute_auto_estimate(claim)
    if typed and not claim.is_total_loss:
        claim.estimate_overridden = True
        claim.reserve = typed
    else:
        claim.reserve = claim.auto_estimate
        claim.estimated_damage = D(claim.auto_estimate_detail.get("repair") or claim.estimated_damage or 0)
    total, parts = risk.calculate_total_excess(claim.driver_age, claim.young_driver, claim.undisclosed_driver)
    claim.excess_applied, claim.excess_breakdown = total, {k: str(v) for k, v in parts.items()}
    surv = suggest_surveyor(claim.loss_type, claim.governorate)
    if surv:
        claim.surveyor, claim.surveyor_assigned_on = surv, clock.today()
    base_gross = to_base(claim.reserve, policy.currency)
    from reinsurance.engine import large_loss_flag
    claim.large_loss, _ = large_loss_flag(claim, base_gross)
    claim.save()
    if claim.third_party_liable:
        sub = Subrogation.objects.create(claim=claim, liable_party=sub_data["liable_party"] or "To be identified", liable_insurer=sub_data["liable_insurer"],
                                         liable_insurer_email=sub_data["liable_insurer_email"], their_policy_no=sub_data["their_policy_no"],
                                         their_vehicle=sub_data["their_vehicle"], their_claim_ref=sub_data["their_claim_ref"], reserve_amount=claim.reserve)
        if sub.liable_insurer or sub.liable_insurer_email:
            send_notice(claim, SubroNotice.OPENING, user)
    from accounting import services as acc
    acc.sync_claim(claim, user)
    return claim


def compute_auto_estimate(claim):
    """Estimate the total cost of the claim from what is known: damage map, total loss value, injuries, legal allowance.

    Stored on the claim (``auto_estimate`` + a breakdown). The reserve follows it until somebody overrides it."""
    cur = claim.policy.currency
    risk = claim.risk
    lines = []
    repair = ZERO
    if claim.is_total_loss:
        repair = claim.depreciated_value or total_loss_value(risk, claim.policy, claim.date_of_loss)
        lines.append(("Total loss: depreciated market value", repair))
    elif claim.damage_zones:
        from . import ai
        res = ai.estimate(risk=risk, zones=claim.damage_zones, loss_type=claim.loss_type, policy=claim.policy, llm=False)
        repair = D(str(res.get("expected") or 0))
        if repair:
            lines.append((f"Vehicle damage ({res.get('baseline', {}).get('method', 'statistical')})", repair))
    elif claim.estimated_damage:
        repair = D(claim.estimated_damage)
        lines.append(("Damage figure provided", repair))
    inj = ZERO
    if claim.pk:
        for i in claim.injuries.all():
            table = convert(settings.INJURY_COST_TABLE.get(i.severity, ZERO), Currency.base(), cur)
            amt = (i.compensation_reserve or table) + i.medical_cost
            inj += amt
            lines.append((f"{i.get_severity_display()}: {i.person_name}", amt))
    legal = ZERO
    if claim.is_legal:
        legal = convert(settings.LEGAL_COST_ALLOWANCE, Currency.base(), cur)
        lines.append(("Legal costs allowance", legal))
    total = repair + inj + legal
    if claim.is_total_loss and risk.sum_insured:
        total = min(total, risk.sum_insured + inj + legal)
    claim.auto_estimate = q(cur, total)
    claim.auto_estimate_detail = {"repair": str(q(cur, repair)), "injury": str(q(cur, inj)), "legal": str(q(cur, legal)),
                                  "lines": [{"label": l, "amount": str(q(cur, a))} for l, a in lines]}
    return claim.auto_estimate


def refresh_estimate(claim, user=None, reset=False):
    """Recalculate the automatic estimate; the reserve follows it unless it was overridden (``reset`` removes the override)."""
    compute_auto_estimate(claim)
    if reset:
        claim.estimate_overridden = False
    if not claim.estimate_overridden and claim.status in (Claim.OPEN, Claim.ASSESSED):
        claim.reserve = claim.auto_estimate
    claim.save()
    from accounting import services as acc
    acc.sync_claim(claim, user)
    return claim


def check_estimate(claim, garage, basis, labour, parts, fixed, est_days, supplementary=False):
    """Store the garage's estimate (labour only, parts only, labour + parts, or a fixed amount as quoted).

    A new estimate replaces the garage's earlier one unless it is marked supplementary. A non-preferred garage whose quote is more
    than 15% above the reserve is flagged."""
    est = RepairEstimate(claim=claim, garage=garage, pricing_basis=basis, labour_amount=labour or ZERO, parts_cost=parts or ZERO,
                         fixed_amount=fixed or ZERO, estimated_days=est_days, supplementary=supplementary)
    base_ref = claim.reserve or claim.estimated_damage
    est.exceeds_market = bool((not garage.preferred) and base_ref and est.total > base_ref * D("1.15"))
    if not supplementary:
        claim.estimates.filter(garage=garage).update(superseded=True)
    est.save()
    claim.garage = garage
    claim.save(update_fields=["garage"])
    return est


def estimate_total(est, cur):
    return q(cur, est.total)


def active_estimates(claim):
    return claim.estimates.filter(superseded=False)


def record_invoice(est, invoice_no, amount, invoice_date):
    if not invoice_no or not amount:
        raise ClaimError("Enter the garage invoice number and amount.")
    est.invoice_no, est.invoice_amount, est.invoice_date = invoice_no, amount, invoice_date
    est.save()
    return est


def settle_estimate(est):
    if not est.invoice_no or not est.invoice_amount:
        raise ClaimError("Record the garage invoice before settling this estimate.")
    est.settled, est.settled_on = True, clock.today()
    est.save()
    return est


def record_excess(claim, amount, on, waiver_reason=""):
    """Record the motor vehicle excess collected from the insured (or why it was waived)."""
    if (not amount or D(amount) <= 0) and not waiver_reason:
        raise ClaimError("Enter the excess collected, or the reason it was waived.")
    claim.excess_collected, claim.excess_collected_on = q(claim.policy.currency, amount or 0), on
    claim.excess_waiver_reason = waiver_reason
    claim.save(update_fields=["excess_collected", "excess_collected_on", "excess_waiver_reason"])
    return claim


@transaction.atomic
def approve_claim(claim, approved_cost, user, total_loss=False):
    cur = claim.policy.currency
    role = user_role(user)
    if total_loss:
        if not claim.depreciated_value:
            claim.depreciated_value = total_loss_value(claim.risk, claim.policy, claim.date_of_loss)
        if not approved_cost:
            approved_cost = claim.depreciated_value      # default: depreciated market value, never above the sum insured
        approved_cost = min(D(approved_cost), claim.risk.sum_insured) if claim.risk.sum_insured else D(approved_cost)
    base_amt = to_base(approved_cost, cur)
    if base_amt > settings.CLAIM_MANAGER_LIMIT and role not in ("CLAIMS_MANAGER", "ADMIN"):
        raise ClaimError(f"Settlements above {settings.CLAIM_MANAGER_LIMIT} {settings.BASE_CURRENCY} need the Head of Claims / Claims Manager.")
    total, parts = claim.risk.calculate_total_excess(claim.driver_age, claim.young_driver, claim.undisclosed_driver)
    claim.approved_repair_cost = q(cur, approved_cost)
    claim.excess_applied, claim.excess_breakdown = total, {k: str(v) for k, v in parts.items()}
    claim.net_payable = max(q(cur, claim.approved_repair_cost - total), ZERO)  # Approved Repair Cost – Total Applicable Excess
    if claim.pk:
        claim.net_payable += sum((h.net for h in claim.heads.all()), ZERO)       # approved cover heads are paid on top
    claim.is_total_loss = total_loss
    claim.status = Claim.APPROVED
    claim.approved_by = user
    claim.surveyor_report_on = claim.surveyor_report_on or (clock.today() if claim.surveyor_id else None)
    if not claim.file_finalised_on:
        claim.file_finalised_on = clock.today()
    claim.save()
    # Reinsurance recovery: quota share (up to its limit), then surplus, then excess of loss
    layers = ri_layers(claim, to_base(claim.net_payable, cur))
    ri_total_base = sum((x for _, _, x in layers), ZERO)
    if ri_total_base > 0:
        note = "Waterfall: " + "; ".join(f"{k} {t.reference} {x}" for t, k, x in layers)
        Recovery.objects.update_or_create(claim=claim, recovery_type=Recovery.REINSURANCE, received_on=None,
                                          defaults={"amount": q(cur, ri_total_base / (claim.policy.fx_rate or D(1))), "note": note[:150]})
    if claim.third_party_liable:
        sub, _ = Subrogation.objects.get_or_create(claim=claim, defaults={"liable_party": "To be identified", "reserve_amount": claim.net_payable})
        if sub.reserve_amount != claim.net_payable and sub.recovery_status in ("DEMAND_SENT", "IN_NEGOTIATION"):
            sub.reserve_amount = claim.net_payable
            sub.save(update_fields=["reserve_amount"])
    from accounting import services as acc
    acc.sync_claim(claim, user)
    return claim


def salvage_base(claim):
    """Salvage received so far, in base currency."""
    got = claim.recoveries.filter(recovery_type=Recovery.SALVAGE).exclude(received_on=None).aggregate(s=Sum("amount"))["s"] or ZERO
    return to_base(got, claim.policy.currency)


def ri_layers(claim, loss_base):
    """Reinsurance waterfall for a claim, in base currency.

    Claim amount = (claim payable after excess) - salvage recovered.  Then, in sequence:
      1. Quota share: amount x the quota share factor (premium ceded / net premium), up to the treaty's quota share limit;
      2. Surplus: amount x the surplus factor, on what the quota share left;
      3. Excess of loss: whatever is still retained above the XoL retention, up to the XoL limit (treaties in force on the date of loss).
    Returns a list of (treaty or None, kind, amount) with kind QUOTA / SURPLUS / XOL."""
    from reinsurance.engine import active_treaties
    from reinsurance.models import Treaty

    net = max(D(loss_base) - salvage_base(claim), ZERO)
    out = []
    if net <= 0:
        return out
    net_prem = to_base(claim.risk.net_premium, claim.policy.currency)
    remaining = net
    if net_prem:
        ces = [c for c in claim.policy.cessions.select_related("treaty").order_by("rank", "id")
               if not c.is_facultative and c.treaty_id and c.treaty_type != "XOL" and (not c.risk_id or c.risk_id == claim.risk_id)]
        for kind in ("QUOTA", "SURPLUS"):
            layer = {}
            for c in ces:
                if c.treaty_type == kind:
                    layer[c.treaty] = layer.get(c.treaty, ZERO) + D(c.premium_ceded) / net_prem
            tot = sum(layer.values(), ZERO)
            scale = D(1) / tot if tot > 1 else D(1)            # never recover more than the claim
            base = remaining
            for t, factor in layer.items():
                amt = min(net * factor * scale, base)
                if kind == "QUOTA" and t.qs_limit:
                    amt = min(amt, t.qs_limit)
                amt = amt.quantize(D("0.001"), rounding=ROUND_HALF_UP)
                if amt > 0:
                    out.append((t, kind, amt))
                    remaining -= amt
    if remaining > 0:
        for t in active_treaties(claim.date_of_loss, [Treaty.XOL]):
            if t.xol_kind != Treaty.RISK:
                continue                                        # cat / aggregate / stop loss work on totals (reinsurance.xol)
            if t.xol_retention and remaining > t.xol_retention:
                amt = remaining - t.xol_retention
                if t.xol_limit:
                    amt = min(amt, t.xol_limit)
                out.append((t, "XOL", amt.quantize(D("0.001"), rounding=ROUND_HALF_UP)))
    return out


def incurred_base(claim):
    """The claim amount in base currency used for reinsurance: payable after excess once approved, otherwise reserve less excess."""
    if claim.status == Claim.REJECTED:
        return ZERO
    amt = claim.net_payable if claim.status in (Claim.APPROVED, Claim.SETTLED, Claim.CLOSED) else max(claim.reserve - claim.excess_applied, ZERO)
    return to_base(amt, claim.policy.currency)


def retained_split(claim):
    """(amount after salvage, recovered by quota share / surplus, recovered by per-risk XoL) in base currency."""
    base = incurred_base(claim)
    net = max(base - salvage_base(claim), ZERO)
    layers = ri_layers(claim, base)
    prop = sum((a for _, k, a in layers if k != "XOL"), ZERO)
    risk = sum((a for _, k, a in layers if k == "XOL"), ZERO)
    return net, prop, risk


def retained_loss(claim):
    """What the company keeps after quota share, surplus and per-risk XoL: the loss that cat, aggregate and stop-loss covers sit on."""
    net, prop, risk = retained_split(claim)
    return max(net - prop - risk, ZERO)


def reinsurance_share(claim, loss_base=None):
    """Proportional (quota share + surplus) part of the claim as a fraction of the claim payable."""
    base = D(loss_base) if loss_base is not None else to_base(claim.net_payable or claim.reserve, claim.policy.currency)
    if base <= 0:
        return ZERO
    return min(sum((a for _, k, a in ri_layers(claim, base) if k != "XOL"), ZERO) / base, D(1))


def xol_recoveries(claim, loss_base):
    return [(t, a) for t, k, a in ri_layers(claim, loss_base) if k == "XOL"]


def reinsurance_target(claim, loss_base):
    """Total reinsurance recoverable on the loss (base currency): quota share, then surplus, then XoL."""
    return sum((a for _, _, a in ri_layers(claim, loss_base)), ZERO)


def rebalance_treaty_recoveries(claim, user=None):
    """Bring the treaty recovery journal in line after the claim amount changed (reversal on closing, salvage): one adjustment row per
    treaty for the difference between what is now recoverable on the amount paid and what was posted."""
    from reinsurance.models import Treaty

    cur = claim.policy.currency
    incurred = to_base(claim.net_payable, cur)
    if not incurred:
        return []
    ratio = min(to_base(claim.total_paid, cur) / incurred, D(1))
    want = {}
    for t, kind, a in ri_layers(claim, incurred):
        want[t.reference] = (f"{t.reference} {t.name}", want.get(t.reference, ("", ZERO))[1] + a * ratio)
    posted = {}
    for r in claim.treaty_recoveries.all():
        posted[r.treaty_ref] = posted.get(r.treaty_ref, ZERO) + r.amount
    rows, jno = [], None
    for k in sorted(set(want) | set(posted)):
        diff = (want.get(k, ("", ZERO))[1] - posted.get(k, ZERO)).quantize(D("0.001"), rounding=ROUND_HALF_UP)
        if diff == 0:
            continue
        jno = jno or f"TRJ-{DocumentSequence.next('TRJ'):06d}"
        t = Treaty.objects.filter(reference=k).order_by("-version").first() if k not in ("FAC", "XOL") else None
        lab = want.get(k, (None,))[0] or next((r.treaty_label for r in claim.treaty_recoveries.filter(treaty_ref=k)), k)
        rows.append(TreatyRecovery.objects.create(claim=claim, payment=None, journal_no=jno, treaty=t, treaty_ref=k, treaty_label=f"Adjustment: {lab}"[:120],
                                                  amount=diff, posted_on=clock.today(), is_demo=claim.is_demo))
    return rows


@transaction.atomic
def pay_claim(claim, stage, payee, reference=""):
    """FSA unified policy: 70% before repair begins, remaining 30% after completion; total losses are paid in full."""
    cur = claim.policy.currency
    if claim.status not in (Claim.APPROVED, Claim.SETTLED):
        raise ClaimError("Claim must be approved before payment.")
    paid = {p.stage for p in claim.payments.exclude(stage="HEAD")}
    net = claim.net_payable - sum((h.net for h in claim.heads.all()), ZERO)   # staging applies to own damage only; heads go by credit note
    if stage == "INTERIM":
        if paid & {"INTERIM", "FULL"}:
            raise ClaimError("The pre-repair instalment was already paid.")
        amt = q(cur, net * settings.REPAIR_INTERIM_PCT / 100)
    elif stage == "FINAL":
        if "INTERIM" not in paid:
            raise ClaimError("Pay the pre-repair 70% first.")
        if "FINAL" in paid or "FULL" in paid:
            raise ClaimError("The final instalment was already paid.")
        if not claim.repair_completed_on:
            raise ClaimError("Final 30% is released only after the repair is completed — record completion first.")
        amt = q(cur, net - sum((p.amount for p in claim.payments.filter(stage="INTERIM")), ZERO))
    elif stage == "FULL":
        if paid:
            raise ClaimError("Payments already exist for this claim.")
        amt = net
    elif stage == "DELAY":
        if claim.delay_compensation <= 0:
            raise ClaimError("No delay compensation is due.")
        if "DELAY" in paid:
            raise ClaimError("Delay compensation was already paid.")
        amt = claim.delay_compensation
    else:
        raise ClaimError("Unknown stage")
    pay = ClaimPayment.objects.create(claim=claim, stage=stage, amount=amt, paid_on=clock.today(), payee=payee, reference=reference)
    claim.total_paid = claim.payments.aggregate(s=Sum("amount"))["s"] or ZERO
    if claim.status == Claim.APPROVED and claim.total_paid >= claim.net_payable:
        claim.status = Claim.SETTLED
    claim.save()
    from accounting import services as acc
    acc.on_claim_payment(claim, pay)
    post_treaty_recovery(claim, pay)
    return amt


@transaction.atomic
def complete_repair(claim, completed_on, rework=False, customer_score=None, final_bill=None):
    """Record repair completion: FSA 30-day clock, delay compensation and the garage rating."""
    cur = claim.policy.currency
    if not claim.file_finalised_on:
        raise ClaimError("The claim file has not been finalised.")
    claim.repair_completed_on = completed_on
    elapsed = (completed_on - claim.file_finalised_on).days
    delay = max(elapsed - settings.REPAIR_SLA_DAYS, 0)
    claim.repair_delay_days = delay
    daily = convert(settings.REPAIR_DELAY_DAILY_COMPENSATION, Currency.base(), cur)
    claim.delay_compensation = q(cur, daily * delay)
    claim.save()
    if claim.garage_id:
        est = claim.estimates.filter(garage=claim.garage).last()
        est_days = est.estimated_days if est else settings.REPAIR_SLA_DAYS
        est_amt = estimate_total(est, cur) if est else claim.approved_repair_cost
        final = D(final_bill) if final_bill else claim.approved_repair_cost
        rate_provider(claim.garage, claim, est_days, elapsed, est_amt, final, rework, customer_score)
    sub = getattr(claim, "subrogation", None)
    if sub and (sub.liable_insurer or sub.liable_insurer_email):      # tell the at-fault party's insurer the car is repaired and ask for payment
        send_notice(claim, SubroNotice.CONFIRMATION)
        send_notice(claim, SubroNotice.DEMAND)
    return claim


def rate_provider(provider, claim, est_days, actual_days, est_amt, final_amt, rework, customer_score):
    """Rating from turnaround, cost variance, customer feedback and re-work (addendum 4.2)."""
    est_days = max(est_days, 1)
    timeliness = D(5) if actual_days <= est_days else max(D(1), D(5) - D(actual_days - est_days) / D(est_days) * D(5))
    variance = ((D(final_amt) - D(est_amt)) / D(est_amt) * 100) if est_amt else ZERO
    cost = D(5) if variance <= 0 else max(D(1), D(5) - variance / D(5))
    quality = D(1) if rework else D(5)
    scores = [timeliness, cost, quality]
    if customer_score:
        scores.append(D(customer_score))
    overall = sum(scores) / len(scores)
    r, _ = ProviderRating.objects.update_or_create(
        provider=provider, claim=claim,
        defaults=dict(timeliness_score=round(timeliness, 2), cost_score=round(cost, 2), quality_score=quality,
                      customer_score=customer_score, rework=rework, overall=round(overall, 2), estimated_days=est_days,
                      actual_days=actual_days, estimate_amount=est_amt, final_amount=final_amt))
    provider.refresh_score()
    return r


def add_recovery(claim, rtype, amount, note=""):
    cur = claim.policy.currency
    from accounting import services as acc
    if rtype == Recovery.OVERPAYMENT:
        due = claim.recoveries.filter(recovery_type=rtype, received_on=None).first()
        if not due and not claim.overpaid:
            raise ClaimError("No overpayment is outstanding on this claim.")
        rec = due or Recovery(claim=claim, recovery_type=rtype)
        rec.amount, rec.received_on, rec.note = q(cur, amount), clock.today(), note or rec.note
        rec.save()
        claim.overpaid = max(claim.overpaid - rec.amount, ZERO)
        claim.save(update_fields=["overpaid"])
        acc.on_overpayment_refund(claim, rec.amount)
        return rec
    rec = Recovery.objects.create(claim=claim, recovery_type=rtype, amount=q(cur, amount), received_on=clock.today(), note=note)
    if rtype == Recovery.REINSURANCE:
        acc.on_reinsurance_cash(claim, rec.amount)
        apply_ri_cash(claim, to_base(rec.amount, claim.policy.currency))
    else:
        acc.on_recovery(claim, rec.amount, f"{rec.get_recovery_type_display()} recovery received")
        if rtype == Recovery.SALVAGE and claim.status in (Claim.APPROVED, Claim.SETTLED, Claim.CLOSED):
            rebalance_treaty_recoveries(claim)           # salvage reduces the amount reinsurers share in
            acc.sync_claim(claim, memo="salvage recovered")
    return rec


@transaction.atomic
def record_subrogation_recovery(claim, amount):
    """Record money recovered from the at-fault party and refund the insured's excess if due."""
    cur = claim.policy.currency
    sub = claim.subrogation
    sub.amount_recovered = q(cur, sub.amount_recovered + D(amount))
    full = sub.amount_recovered >= sub.reserve_amount
    if full:
        sub.recovery_status = Subrogation.RECOVERED
        # Excess refund: insured gets their excess back once the insurer is made whole
        refund = min(claim.excess_applied, sub.amount_recovered - sub.reserve_amount + claim.excess_applied)
        sub.excess_refunded = q(cur, max(refund, ZERO))
        if sub.excess_refunded > 0:
            ClaimPayment.objects.create(claim=claim, stage="FULL", amount=sub.excess_refunded, paid_on=clock.today(),
                                        payee=claim.policy.customer.name, reference="Excess refund (subrogation)")
            from accounting import services as acc
            acc.on_excess_refund(claim, sub.excess_refunded)
    sub.save()
    add_recovery(claim, Recovery.SUBROGATION, amount, "Subrogation recovery")
    return sub


def financial_position(claim):
    cur = claim.policy.currency
    gross = claim.gross_incurred
    received = claim.recoveries.exclude(received_on=None).aggregate(s=Sum("amount"))["s"] or ZERO
    expected = claim.recoveries.filter(received_on=None).aggregate(s=Sum("amount"))["s"] or ZERO
    outstanding = max(claim.net_payable - claim.total_paid, ZERO)
    sub_open = hasattr(claim, "subrogation") and claim.subrogation.recovery_status not in ("RECOVERED", "WRITTEN_OFF")
    return {"gross": gross, "paid": claim.total_paid, "received": received, "expected": expected,
            "net_cost": claim.total_paid - received, "outstanding": outstanding, "sub_open": sub_open}


def close_blockers(claim):
    """Everything that stops the claim being financially closed (empty list = it can be closed)."""
    pos = financial_position(claim)
    out = []
    if claim.status not in (Claim.SETTLED, Claim.APPROVED):
        return ["Only approved or settled claims can be closed."]
    pre = closing_preview(claim)
    outstanding = max(pre["new_net"] - claim.total_paid, ZERO) if pre["applies"] and pre["delta"] < 0 else pos["outstanding"]
    if outstanding > 0:
        out.append(f"Outstanding payments remain ({outstanding} {claim.policy.currency.code}).")
    if pos["sub_open"]:
        out.append("Subrogation is still open: mark it recovered or written off first.")
    ests = list(active_estimates(claim))
    repairable = (not claim.is_total_loss and claim.loss_type in ("COLLISION", "FIRE", "NATURAL", "TP_PROPERTY", "OTHER") and claim.approved_repair_cost > 0)
    if repairable and not ests:
        out.append("No garage estimate or invoice is recorded for this repair.")
    for e in ests:
        if not e.invoice_no or not e.invoice_amount:
            out.append(f"Invoice missing for the {e.garage.name} estimate ({e.get_pricing_basis_display()}).")
        elif not e.settled:
            out.append(f"The {e.garage.name} estimate / invoice {e.invoice_no} is not settled.")
    if claim.excess_applied > 0 and claim.excess_collected < claim.excess_applied and not claim.excess_waiver_reason:
        out.append(f"Motor vehicle excess of {claim.excess_applied} {claim.policy.currency.code} is not recorded as collected (or waived with a reason).")
    return out


def closing_preview(claim):
    """What closing will do to the estimate: the outstanding (OS) estimate is reversed to the invoiced cost.

    e.g. approved estimate 1000, invoice 900 -> the estimate is reversed by 100 and the claim is adjusted to 900."""
    cur = claim.policy.currency
    ests = list(active_estimates(claim))
    invoiced = sum((D(e.invoice_amount) for e in ests if e.invoice_amount), ZERO)
    out = {"applies": False, "estimate": claim.approved_repair_cost, "invoiced": invoiced, "delta": ZERO, "new_cost": claim.approved_repair_cost,
           "new_net": claim.net_payable, "overpaid": ZERO, "currency": cur.code}
    if claim.is_total_loss or not ests or not invoiced or claim.status not in (Claim.APPROVED, Claim.SETTLED):
        return out
    new_cost = q(cur, invoiced)
    new_net = max(q(cur, new_cost - claim.excess_applied), ZERO)
    out.update(applies=True, delta=q(cur, new_cost - claim.approved_repair_cost), new_cost=new_cost, new_net=new_net,
               overpaid=max(q(cur, claim.total_paid - new_net), ZERO))
    return out


@transaction.atomic
def close_claim(claim, user=None, confirmed=False):
    """Financially close only when nothing is outstanding, every estimate is invoiced and settled, the excess is recorded and
    every recovery action is resolved. If the invoices differ from the approved estimate, the estimate is reversed to the invoiced
    amount (logged) - the person closing must confirm that."""
    pre = closing_preview(claim)
    if pre["applies"] and pre["delta"] > 0:
        raise ClaimError(f"The invoices ({pre['invoiced']}) are above the approved cost ({pre['estimate']}). Reopen and re-approve the higher amount first.")
    blockers = close_blockers(claim)
    if blockers:
        raise ClaimError(" ".join(blockers))
    cur = claim.policy.currency
    from accounting import services as acc
    memo = ""
    if pre["applies"] and pre["delta"] != 0:
        if not confirmed:
            raise ClaimError(f"Confirm the reversal of the estimate by {abs(pre['delta'])} {cur.code} (from {pre['estimate']} to {pre['invoiced']}) before closing.")
        ClaimAdjustment.objects.create(
            claim=claim, user=user, kind="ESTIMATE_REVERSAL", before=pre["estimate"], after=pre["new_cost"], delta=pre["delta"],
            description=f"On closing: estimate {pre['estimate']} reversed by {abs(pre['delta'])} to the invoiced {pre['invoiced']} {cur.code}")
        claim.approved_repair_cost, claim.net_payable, claim.reserve = pre["new_cost"], pre["new_net"], pre["new_cost"]
        claim.overpaid = pre["overpaid"]
        claim.save(update_fields=["approved_repair_cost", "net_payable", "reserve", "overpaid"])
        memo = f"estimate reversed by {abs(pre['delta'])} to invoice {pre['invoiced']}"
        sub = getattr(claim, "subrogation", None)
        if sub and sub.recovery_status in ("DEMAND_SENT", "IN_NEGOTIATION"):
            sub.reserve_amount = claim.net_payable
            sub.save(update_fields=["reserve_amount"])
        if claim.overpaid:
            Recovery.objects.update_or_create(claim=claim, recovery_type=Recovery.OVERPAYMENT, received_on=None,
                                              defaults={"amount": claim.overpaid, "note": "Paid above the final invoice: recover from the payee"})
    # the expected reinsurance recovery follows the final amount (quota share to its limit, then surplus, then XoL)
    layers = ri_layers(claim, to_base(claim.net_payable, cur))
    ri_base = sum((x for _, _, x in layers), ZERO)
    claim.recoveries.filter(recovery_type=Recovery.REINSURANCE, received_on=None).delete()
    if ri_base > 0:
        Recovery.objects.create(claim=claim, recovery_type=Recovery.REINSURANCE, amount=q(cur, ri_base / (claim.policy.fx_rate or D(1))),
                                note="Waterfall: " + "; ".join(f"{k} {t.reference} {x}" for t, k, x in layers)[:130])
    rebalance_treaty_recoveries(claim, user)
    expected_ri = claim.recoveries.filter(received_on=None, recovery_type=Recovery.REINSURANCE)
    ri_total = expected_ri.aggregate(s=Sum("amount"))["s"] or ZERO
    expected_ri.update(received_on=clock.today())
    claim.status = Claim.CLOSED
    claim.save(update_fields=["status"])
    acc.sync_claim(claim, user, memo=memo)
    if ri_total:
        acc.on_reinsurance_cash(claim, ri_total, user)
        apply_ri_cash(claim, to_base(ri_total, cur))
    return pre


# ---- locking, reopening ---------------------------------------------------------------------------------------
LOCKED_OK = {"cat_flag", "close", "reopen", "recovery", "subro_recover", "subrogation", "legal_flag", "legal_action", "notice"}
CLOSED_OK = {"cat_flag", "reopen", "recovery", "legal_flag", "legal_action", "notice"}


def assert_editable(claim, action, post=None):
    """Settled, closed and rejected claims are read only. Only closing and recoveries (money coming in), subrogation, legal work and notices are allowed; anything else needs a reopen."""
    if not claim.locked:
        return
    allowed = CLOSED_OK if claim.status in (Claim.CLOSED, Claim.REJECTED) else LOCKED_OK
    if action in allowed:
        return
    raise ClaimError(f"Claim {claim.claim_no} is {claim.get_status_display().lower()} and read only. Reopen it to change this.")


@transaction.atomic
def reopen_claim(claim, user, reason):
    if not claim.locked:
        raise ClaimError("Only a settled, closed or rejected claim needs to be reopened.")
    if not (reason or "").strip():
        raise ClaimError("Give the reason for reopening the claim.")
    was = claim.status
    claim.status = Claim.OPEN if was == Claim.REJECTED else Claim.APPROVED
    claim.reopen_count += 1
    claim.reopened_on, claim.reopen_reason = clock.today(), reason.strip()[:200]
    claim.save()
    from accounting import services as acc
    acc.sync_claim(claim, user)
    return claim


def settle_claim(claim):
    """Mark an approved claim settled (read only) once everything payable has been paid."""
    if claim.status != Claim.APPROVED:
        raise ClaimError("Only an approved claim can be marked settled.")
    if claim.total_paid < claim.net_payable:
        raise ClaimError("Payments are still outstanding.")
    claim.status = Claim.SETTLED
    claim.save(update_fields=["status"])
    return claim


# ---- reinsurance recovery by treaty on each payment --------------------------------------------------------------
def post_treaty_recovery(claim, payment):
    """Work out the reinsurers' share of a payment and post it, treaty by treaty, to the treaty recovery journal."""
    from reinsurance import shares
    from reinsurance.models import Treaty

    if payment.stage == "DELAY" or not claim.net_payable:
        return []
    paid_base = to_base(payment.amount, claim.policy.currency)
    incurred = to_base(claim.net_payable, claim.policy.currency)
    ratio = paid_base / incurred if incurred else ZERO
    acc = {}
    for t, kind, a in ri_layers(claim, incurred):
        lab = acc.get(t.reference, (f"{t.reference} {t.name}", ZERO))
        acc[t.reference] = (lab[0], lab[1] + a * ratio)
    parts = [(k, lab, a.quantize(D("0.001"), rounding=ROUND_HALF_UP)) for k, (lab, a) in acc.items() if a > 0]
    if not parts:
        return []
    jno = f"TRJ-{DocumentSequence.next('TRJ'):06d}"
    rows = []
    for k, lab, amt in parts:
        t = (Treaty.objects.filter(reference=k, effective_from__lte=claim.date_of_loss, effective_to__gte=claim.date_of_loss).first()
             or Treaty.objects.filter(reference=k).order_by("-version").first())
        rows.append(TreatyRecovery.objects.create(claim=claim, payment=payment, journal_no=jno, treaty=t, treaty_ref=k, treaty_label=lab[:120], amount=amt,
                                                  posted_on=payment.paid_on, is_demo=claim.is_demo))
    return rows


def apply_ri_cash(claim, amount_base):
    """Cash received from reinsurers settles the oldest treaty recoveries of the claim first. Negative adjustment rows (reversals)
    are netted off: they reduce the cash needed to clear the positive rows."""
    left = D(amount_base)
    rows = list(claim.treaty_recoveries.order_by("posted_on", "id"))
    for r in rows:
        if r.due < 0:
            left -= r.due
            r.received = r.amount
            r.save(update_fields=["received"])
    for r in rows:
        if left <= 0:
            break
        take = min(r.due, left)
        if take > 0:
            r.received += take
            r.save(update_fields=["received"])
            left -= take
    return left


# ---- notices to the at-fault party's insurer -----------------------------------------------------------------------
def damage_summary(claim):
    from . import ai
    cur = claim.policy.currency.code
    r = claim.risk
    lines = [f"Our insured vehicle: {r.make} {r.model} {r.year}, plate {r.plate or '-'}, chassis {r.chassis_no}",
             f"Date and place of loss: {claim.date_of_loss:%d %b %Y}, {claim.location or '-'} {claim.governorate}".strip(),
             f"Circumstances: {claim.cause_of_loss}",
             f"Damage: {ai.describe_zones(claim.damage_zones) or 'see attached survey'}" + (" (total loss)" if claim.is_total_loss else ""),
             f"Police report: {claim.police_report_no or 'not yet received'}"]
    if claim.injuries.exists():
        lines.append("Injuries: " + "; ".join(f"{i.person_name} ({i.get_severity_display()})" for i in claim.injuries.all()))
    return lines, cur


def send_notice(claim, kind, user=None):
    """Create (and e-mail when an address is on file) a notice to the liable party's insurer. Opening notice carries the damage details."""
    from django.core.mail import EmailMessage

    from core.models import Company

    sub = getattr(claim, "subrogation", None)
    if not sub:
        raise ClaimError("No subrogation / liable party is recorded on this claim.")
    co = Company.get().name
    lines, cur = damage_summary(claim)
    ref = f"Your insured / policy: {sub.liable_party} {sub.their_policy_no}".strip()
    amount = ZERO
    if kind == SubroNotice.OPENING:
        subject = f"Notice of claim {claim.claim_no} - subrogation against your insured"
        body = [f"Dear {sub.liable_insurer or 'Sirs'},", "", f"We have opened claim {claim.claim_no} under our policy {claim.policy.policy_no}. "
                "Our insured's vehicle was damaged in an accident for which your insured appears to be responsible. Please acknowledge this notice and "
                "confirm the details of your policy and the person handling the matter.", "", ref, *lines, "",
                f"Estimated cost to date: {claim.reserve} {cur}. We will confirm the final figure once the repair is complete and reserve the right to recover everything we pay."]
    elif kind == SubroNotice.CONFIRMATION:
        subject = f"Repair completed - claim {claim.claim_no}"
        done = claim.repair_completed_on
        body = [f"Dear {sub.liable_insurer or 'Sirs'},", "", f"We confirm that the repair of the vehicle below was completed on {done:%d %b %Y}" + (f" by {claim.garage.name}" if claim.garage_id else "") + ".", "",
                ref, *lines, "", f"Approved repair cost: {claim.approved_repair_cost} {cur}; paid by us: {claim.total_paid} {cur}."]
    else:
        amount = max(claim.total_paid - claim.excess_collected * 0, ZERO)
        amount = sub.reserve_amount or claim.net_payable
        subject = f"Demand for payment - claim {claim.claim_no} - {amount} {cur}"
        body = [f"Dear {sub.liable_insurer or 'Sirs'},", "", f"We have paid {claim.total_paid} {cur} under claim {claim.claim_no} and the repair is complete. "
                f"We therefore demand payment of {amount} {cur} within 15 days of this notice, failing which we will refer the matter to our legal counsel without further notice.",
                "", ref, *lines]
    body += ["", "Yours faithfully,", co]
    n = SubroNotice.objects.create(claim=claim, kind=kind, to_name=sub.liable_insurer or sub.liable_party, to_email=sub.liable_insurer_email, subject=subject,
                                   body="\n".join(body), amount=amount, created_by=user)
    if n.to_email:
        try:
            EmailMessage(subject, n.body, to=[n.to_email]).send(fail_silently=False)
            n.sent_on, n.delivery = timezone.now(), "E-mailed"
        except Exception as e:                          # keep the notice; it can be re-sent or downloaded
            n.delivery = f"E-mail failed: {str(e)[:40]}"
    else:
        n.delivery = "No e-mail address on file: download and send"
    n.save()
    if kind == SubroNotice.DEMAND and not sub.demand_sent_on:
        sub.demand_sent_on = clock.today()
        sub.save(update_fields=["demand_sent_on"])
    return n


# ---- legal and injuries ----------------------------------------------------------------------------------------------
def set_legal(claim, on, user=None):
    claim.is_legal = on
    claim.legal_since = (claim.legal_since or clock.today()) if on else claim.legal_since
    claim.save()
    if claim.status in (Claim.OPEN, Claim.ASSESSED):
        refresh_estimate(claim, user)
    return claim


def add_legal_action(claim, user, **d):
    if not claim.is_legal:
        set_legal(claim, True, user)
    return LegalAction.objects.create(claim=claim, created_by=user, **d)


def add_injury(claim, user, **d):
    inj = Injury.objects.create(claim=claim, **d)
    if inj.severity == "DEATH" and not inj.date_of_death:
        inj.date_of_death = claim.date_of_loss
        inj.save(update_fields=["date_of_death"])
    if claim.status in (Claim.OPEN, Claim.ASSESSED):
        refresh_estimate(claim, user)
    return inj


# ---- catastrophe events ------------------------------------------------------------------------------------------
def next_cat_code(on):
    """Next unused code for the year: one above the highest existing number, and checked against the table, so deletions,
    manual codes and requests confirmed at different times can never collide."""
    import re
    prefix = f"CAT-{on.year}-"
    nums = [int(m.group(1)) for c in CatEvent.objects.filter(code__startswith=prefix).values_list("code", flat=True) if (m := re.match(r"^(\d+)$", c[len(prefix):]))]
    n = max(nums, default=0) + 1
    while CatEvent.objects.filter(code=f"{prefix}{n:02d}").exists():
        n += 1
    return f"{prefix}{n:02d}"


@transaction.atomic
def set_cat_event(claim, user, event=None, new_name="", new_type="OTHER", new_date=None):
    """Mark the claim as part of a catastrophe event (or clear it). A new event gets a unique code, e.g. CAT-2026-01."""
    if not event and new_name.strip():
        new_date = new_date or claim.date_of_loss
        event = CatEvent.objects.create(code=next_cat_code(new_date), name=new_name.strip(), event_type=new_type or "OTHER", date_from=new_date,
                                        created_by=user, is_demo=claim.is_demo)
    claim.cat_event = event
    claim.save(update_fields=["cat_event"])
    return event

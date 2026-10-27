"""Pricing, approval routing, issuance and endorsement logic for the motor system."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core import clock
from core.models import Currency, DocumentSequence, convert, to_base, user_role

from .models import (
    ApprovalLimit, Commission, CommissionRule, Cover, Discount, Driver, Endorsement, Fee, Policy, PolicyRisk,
    Product, ProductCover, Proposal, ProposalRisk, RateTable, Vehicle, Voucher, VoucherRedemption,
)

D = Decimal
ZERO = D("0")
HUNDRED = D("100")


def q3(x):
    return D(x).quantize(D("0.001"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Rating
# ---------------------------------------------------------------------------
def find_rate(cover, vehicle_type, usage, cc, seats, age):
    best = None
    for r in RateTable.objects.filter(cover=cover):
        if r.vehicle_type and r.vehicle_type != vehicle_type:
            continue
        if r.usage and r.usage != usage:
            continue
        if not (r.cc_min <= cc <= r.cc_max and r.seats_min <= seats <= r.seats_max and r.age_min <= age <= r.age_max):
            continue
        if best is None or r.specificity > best.specificity:
            best = r
    return best


def best_discount(kind, value):
    """Highest discount of ``kind`` whose threshold is <= value."""
    best = ZERO
    for d in Discount.objects.filter(kind=kind):
        if D(value) >= d.threshold and d.pct > best:
            best = d.pct
    return best


def period_factor(start, end):
    """Pro-rata factor on an annual premium. Cover runs from inception to expiry inclusive, so a 12-month
    policy (e.g. 1 Jan - 31 Dec) is exactly 1.0; a leap year is not charged extra."""
    days = (end - start).days + 1
    return min(D(max(days, 1)) / D(365), D(1))


def depreciated_value(base_value, base_date, as_of, pct):
    """Reducing-balance value: base x (1 - pct/100) ^ years elapsed (fractional years, 365.25-day year)."""
    if base_value is None or base_date is None:
        return None
    years = max((as_of - base_date).days, 0) / 365.25
    return D(str(round(float(base_value) * (1 - float(pct) / 100) ** years, 6)))


def vehicle_value(vehicle, as_of, product, currency, fx_rate=None, fallback_si=None, fallback_date=None):
    """Depreciated market value of ``vehicle`` on ``as_of`` in ``currency``.

    Returns (value, note) or (None, "") when no base value is known. ``fallback_*`` (a previous sum insured in policy
    currency and its date) are used for vehicles insured before depreciation tracking existed."""
    fx = D(fx_rate) if fx_rate is not None else currency.rate_to_base
    base, bdate = vehicle.base_value, vehicle.base_date
    if base is None and fallback_si is not None:
        base, bdate = D(fallback_si) * fx, fallback_date
    if base is None or bdate is None:
        return None, ""
    pct = product.depreciation_pct
    base_val = depreciated_value(base, bdate, as_of, pct)
    val = currency.quantize(base_val / fx)
    years = max((as_of - bdate).days, 0) / 365.25
    note = f"Depreciated {pct.normalize():f}% p.a. (reducing balance) over {years:.2f} yrs from {currency.quantize(D(base) / fx):,} on {bdate:%d %b %Y}"
    return val, note


@dataclass
class RiskInput:
    vehicle_type: str
    usage: str
    cc: int
    seats: int
    year: int
    sum_insured: Decimal  # policy currency
    rider_ids: list = field(default_factory=list)
    waived_ids: list = field(default_factory=list)


def rate_risk(product, risk: RiskInput, currency, fx_rate, *, start, end, ncd_years=0, fleet_size=1,
              extra_discount_pct=ZERO, voucher_pct=ZERO, voucher_code=""):
    """Return the premium breakdown for one risk. Amounts are in *policy currency*.

    FSA: third-party liability is a regulated tariff — it is charged exactly as per the tariff table
    with no discretionary discount; only the Own-Damage part and riders are discountable.
    """
    from .rules import usage_error
    if usage_error(risk.vehicle_type, risk.usage):
        raise ValueError(usage_error(risk.vehicle_type, risk.usage))
    age = max(0, start.year - risk.year)          # vehicle age at inception
    factor = period_factor(start, end)
    si_base = D(risk.sum_insured) * D(fx_rate) if product.includes_od else ZERO
    notes = []

    od_base = ZERO
    od_rate = None
    if product.includes_od:
        od_cover = Cover.objects.get(code="OD")
        od_rate = find_rate(od_cover, risk.vehicle_type, risk.usage, risk.cc, risk.seats, age)
        if od_rate is None:
            raise ValueError(f"No Own Damage rate for {risk.vehicle_type}/{risk.usage}/{risk.cc}cc/age {age}")
        od_base = max(si_base * od_rate.rate_pct / HUNDRED, od_rate.min_premium) * factor

    tp_cover = Cover.objects.get(code="TP")
    tp_rate = find_rate(tp_cover, risk.vehicle_type, risk.usage, risk.cc, risk.seats, age)
    if tp_rate is None:
        raise ValueError(f"No Third Party tariff for {risk.vehicle_type}/{risk.usage}/{risk.cc}cc/age {age}")
    tp_base = max(tp_rate.flat_premium, tp_rate.min_premium) * factor

    # Riders: mandatory (unless waived) + optional selected
    mandatory = {pc.cover_id: pc for pc in ProductCover.objects.filter(product=product, mandatory=True)}
    selected = set(risk.rider_ids) | set(mandatory)
    waived = set()
    for cid in risk.waived_ids:
        pc = mandatory.get(cid)
        if pc and pc.waivable and not pc.regulatory:
            waived.add(cid)
            selected.discard(cid)
    rider_base = ZERO
    rider_lines = []
    for cover in Cover.objects.filter(id__in=selected, kind="RIDER"):
        amt = max(si_base * cover.rate_pct / HUNDRED + cover.flat_amount, cover.min_premium) * factor
        rider_base += amt
        rider_lines.append({"code": cover.code, "name": cover.name, "amount": q3(amt),
                            "mandatory": cover.id in mandatory})

    # Discounts — only on OD + riders (never on regulated TP tariff)
    disc_lines = []
    discountable = od_base + rider_base
    total_disc_pct = ZERO
    if not product.is_tariff_regulated:
        ncd = best_discount(Discount.NCD, ncd_years)
        fleet = best_discount(Discount.FLEET, fleet_size)
        for label, pct in (("No Claim Discount", ncd), ("Fleet discount", fleet), ("Additional discount", D(extra_discount_pct))):
            if pct > 0:
                disc_lines.append({"label": label, "pct": pct})
                total_disc_pct += pct
    authority_pct = min(total_disc_pct, D("60"))     # what counts against the producer's discount authority
    voucher_applied = ZERO
    if voucher_pct and not product.is_tariff_regulated:
        voucher_applied = min(D(voucher_pct), max(D("60") - authority_pct, ZERO))
        if voucher_applied > 0:
            disc_lines.append({"label": f"Voucher {voucher_code}".strip(), "pct": voucher_applied, "voucher": True})
            total_disc_pct += voucher_applied
    total_disc_pct = min(total_disc_pct, D("60"))
    disc_base = discountable * total_disc_pct / HUNDRED
    od_after = od_base - (od_base * total_disc_pct / HUNDRED)
    rider_after = rider_base - (rider_base * total_disc_pct / HUNDRED)

    risk_prem_base = od_after + tp_base + rider_after
    expense_ratio = product.expense_ratio / HUNDRED
    if product.is_tariff_regulated:
        net_base = risk_prem_base           # tariff is the gross charge
        load_base = net_base - net_base / (1 + expense_ratio)
        risk_only_base = net_base - load_base
    else:
        risk_only_base = risk_prem_base
        load_base = risk_only_base * expense_ratio
        net_base = risk_only_base + load_base

    def pc(x):  # base -> policy currency
        return currency.quantize(D(x) / D(fx_rate))

    return {
        "od": pc(od_after), "tp": pc(tp_base), "riders": pc(rider_after), "discount": pc(disc_base),
        "risk_premium": pc(risk_only_base), "expense_load": pc(load_base), "net": pc(net_base),
        "net_base": q3(net_base), "od_base": q3(od_after), "tp_base": q3(tp_base), "rider_base": q3(rider_after),
        "disc_lines": disc_lines, "disc_pct": total_disc_pct, "authority_disc_pct": authority_pct,
        "voucher_pct": voucher_applied, "voucher_amount": pc(discountable * voucher_applied / HUNDRED),
        "discountable_base": discountable, "rider_lines": rider_lines,
        "waived": sorted(waived), "od_rate_pct": od_rate.rate_pct if od_rate else ZERO,
        "tp_tariff": q3(tp_rate.flat_premium), "age": age, "notes": notes,
    }


def validate_voucher(code, product, customer=None, on=None):
    """Return (voucher, error). Does not check the minimum premium (done while pricing)."""
    code = (code or "").strip().upper()
    if not code:
        return None, None
    v = Voucher.objects.filter(code=code).first()
    today = on or clock.today()
    if v is None:
        return None, f"Voucher {code} does not exist."
    if not v.active:
        return None, f"Voucher {code} is not active."
    if v.valid_from > today or v.valid_to < today:
        return None, f"Voucher {code} is valid {v.valid_from:%d %b %Y} to {v.valid_to:%d %b %Y}."
    if v.max_uses is not None and v.used_count >= v.max_uses:
        return None, f"Voucher {code} has been fully used."
    if product is not None and product.is_tariff_regulated:
        return None, "Vouchers cannot be applied to the FSA-regulated third-party tariff."
    if product is not None and v.products.exists() and not v.products.filter(pk=product.pk).exists():
        return None, f"Voucher {code} does not apply to {product.name}."
    if v.customer_id and customer is not None and v.customer_id != customer.pk:
        return None, f"Voucher {code} was issued to another customer."
    return v, None


def price_quote(product, currency, fx_rate, start, end, risks, *, ncd_years=0, extra_discount_pct=ZERO,
                policy_kind="ISSUANCE", voucher=None):
    """Price a whole quote. ``risks`` is a list of RiskInput. Returns totals + per-risk breakdown.

    A voucher reduces only the discountable part (Own Damage + riders); the regulated TP tariff is never discounted.
    A fixed-amount voucher is converted to a percentage of that discountable base."""
    lines = []
    totals = {k: ZERO for k in ("risk_premium", "expense_load", "net")}
    v_pct, v_error = ZERO, None
    kw = dict(start=start, end=end, ncd_years=ncd_years, fleet_size=len(risks), extra_discount_pct=extra_discount_pct)
    if voucher is not None:
        if product.is_tariff_regulated:
            v_error = "Vouchers cannot be applied to the FSA-regulated third-party tariff."
        else:
            first = [rate_risk(product, r, currency, fx_rate, **kw) for r in risks]
            disc_base = sum((l["discountable_base"] for l in first), ZERO)
            net_base = sum((l["net_base"] for l in first), ZERO)
            if net_base < voucher.min_net_premium:
                v_error = f"Voucher {voucher.code} needs a minimum premium of {voucher.min_net_premium} {settings.BASE_CURRENCY}."
            elif voucher.discount_type == Voucher.PERCENT:
                v_pct = voucher.value
            elif disc_base > 0:
                vc = voucher.currency or Currency.base()
                v_pct = min(D(voucher.value) * vc.rate_to_base / disc_base * HUNDRED, HUNDRED).quantize(D("0.0001"))
    for r in risks:
        line = rate_risk(product, r, currency, fx_rate, voucher_pct=v_pct, voucher_code=voucher.code if voucher else "", **kw)
        lines.append(line)
        for k in totals:
            totals[k] += line[k]
    fee_row = Fee.objects.filter(code=policy_kind).first()
    fees = convert(fee_row.amount, Currency.base(), currency) if fee_row else ZERO
    tax = currency.quantize((totals["net"] + fees) * settings.VAT_RATE)
    totals.update({"fees": fees, "tax": tax, "total": currency.quantize(totals["net"] + fees + tax), "lines": lines,
                   "discount_total": sum((l["discount"] for l in lines), ZERO), "voucher_error": v_error,
                   "voucher_discount": sum((l["voucher_amount"] for l in lines), ZERO)})
    return totals


def inputs_from_proposal(proposal):
    out = []
    for r in proposal.risks.select_related("vehicle"):
        v = r.vehicle
        out.append(RiskInput(v.vehicle_type, v.usage, v.cc, v.seats, v.year, r.sum_insured,
                             list(r.riders.values_list("id", flat=True)), list(r.waived_riders.values_list("id", flat=True))))
    return out


@transaction.atomic
def reprice_proposal(proposal):
    """Recalculate and persist the premium of a proposal and its risks."""
    risks = list(proposal.risks.select_related("vehicle"))
    from .rules import government_error, usage_error
    for r in risks:
        err = usage_error(r.vehicle.vehicle_type, r.vehicle.usage) or government_error(r.vehicle.usage, proposal.customer)
        if err:
            raise ValueError(f"{r.vehicle.make} {r.vehicle.model}: {err}")
    inputs = inputs_from_proposal(proposal)
    voucher = proposal.voucher
    if voucher is not None:
        voucher, _ = validate_voucher(voucher.code, proposal.product, proposal.customer)
    result = price_quote(proposal.product, proposal.currency, proposal.fx_rate, proposal.start_date, proposal.end_date,
                         inputs, ncd_years=proposal.ncd_years, extra_discount_pct=proposal.discount_pct, voucher=voucher)
    if proposal.voucher_id and (voucher is None or result["voucher_error"]):
        proposal.voucher = None                      # no longer valid: drop it rather than price with it
    proposal.voucher_discount = result["voucher_discount"]
    for risk, line in zip(risks, result["lines"]):
        risk.od_premium, risk.tp_premium, risk.rider_premium = line["od"], line["tp"], line["riders"]
        risk.discount_amount, risk.net_premium = line["discount"], line["net"]
        risk.breakdown = _jsonable(line)
        risk.save()
    proposal.risk_premium, proposal.expense_load = result["risk_premium"], result["expense_load"]
    proposal.net_premium, proposal.fees, proposal.tax = result["net"], result["fees"], result["tax"]
    proposal.total_payable = result["total"]
    proposal.save()
    return result


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    return obj


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
def next_quote_number():
    return f"QT/{clock.today().year}/{DocumentSequence.next('QUOTE'):06d}"


def next_policy_number():
    return f"MOT/{clock.today().year}/{DocumentSequence.next('POLICY'):06d}"


# ---------------------------------------------------------------------------
# Approval routing
# ---------------------------------------------------------------------------
def approval_reasons(proposal):
    """Reasons why this proposal needs approval beyond the producer's authority limits."""
    role = user_role(proposal.producer)
    limit = ApprovalLimit.objects.filter(role=role).first()
    reasons = []
    total_si_base = sum((to_base(r.sum_insured, proposal.currency) for r in proposal.risks.all()), ZERO)
    max_disc = max((r.breakdown.get("authority_disc_pct", r.breakdown.get("disc_pct", 0)) for r in proposal.risks.all()), default=0)
    fleet = proposal.risks.count()
    waived = any(r.waived_riders.exists() for r in proposal.risks.all())
    if limit is None:
        return ["No authority limit configured for this role"]
    if limit.max_sum_insured is not None and total_si_base > limit.max_sum_insured:
        reasons.append(f"Sum insured exceeds authority ({q3(total_si_base)} {settings.BASE_CURRENCY} > {limit.max_sum_insured})")
    if limit.max_discount_pct is not None and D(str(max_disc)) > limit.max_discount_pct:
        reasons.append(f"Discount {max_disc}% exceeds authority ({limit.max_discount_pct}%)")
    if limit.max_fleet_size is not None and fleet > limit.max_fleet_size:
        reasons.append(f"Fleet size {fleet} exceeds authority ({limit.max_fleet_size})")
    if waived:
        reasons.append("Mandatory rider waived — underwriter approval required")
    return reasons


def can_decide(user, proposal):
    """Whether ``user`` may approve/reject this proposal given its reasons."""
    role = user_role(user)
    if role == "ADMIN":
        return True
    limit = ApprovalLimit.objects.filter(role=role, can_approve=True).first()
    if not limit or proposal.producer_id == user.id:
        return False
    total_si_base = sum((to_base(r.sum_insured, proposal.currency) for r in proposal.risks.all()), ZERO)
    if limit.max_sum_insured is not None and total_si_base > limit.max_sum_insured:
        return False
    max_disc = max((D(str(r.breakdown.get("authority_disc_pct", r.breakdown.get("disc_pct", 0)))) for r in proposal.risks.all()), default=ZERO)
    if limit.max_discount_pct is not None and max_disc > limit.max_discount_pct:
        return False
    if any(r.waived_riders.exists() for r in proposal.risks.all()) and role != "UNDERWRITER":
        return False
    return True


class ValidationFailed(Exception):
    pass


def convert_to_proposal(proposal):
    """Validate mandatory fields, then route for approval or auto-approve (FRD 4.2/4.3)."""
    errors = []
    if proposal.is_expired:
        errors.append("The quote has expired; create a new quote or renew it.")
    if not proposal.risks.exists():
        errors.append("Add at least one vehicle.")
    c = proposal.customer
    if not (c.civil_id or c.cr_number):
        errors.append("Customer needs a Civil ID (individuals) or CR number (corporate).")
    if not c.phone:
        errors.append("Customer phone is required for SMS delivery.")
    for r in proposal.risks.select_related("vehicle"):
        v = r.vehicle
        if not (v.chassis_no and v.engine_no):
            errors.append(f"{v}: chassis and engine numbers are mandatory.")
        if proposal.product.includes_od and r.sum_insured <= 0:
            errors.append(f"{v}: sum insured is required for comprehensive cover.")
        for rider in r.riders.all():
            if not rider.allowed_for(proposal.product):
                errors.append(f"{v}: the rider '{rider.name}' can only be added to {rider.only_products} policies.")
        clash = PolicyRisk.objects.filter(chassis_no=v.chassis_no, active=True, policy__status__in=[Policy.ACTIVE, Policy.PENDING_FAC],
                                          policy__end_date__gte=proposal.start_date, policy__start_date__lte=proposal.end_date)
        if clash.exists():
            errors.append(f"{v}: already insured by policy {clash.first().policy.policy_no} for an overlapping period.")
    if errors:
        raise ValidationFailed(errors)
    reasons = approval_reasons(proposal)
    proposal.approval_reasons = reasons
    proposal.status = Proposal.PENDING if reasons else Proposal.APPROVED
    proposal.save(update_fields=["approval_reasons", "status", "updated_at"])
    return proposal


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------
@transaction.atomic
def issue_policy(proposal, user):
    """Issue a policy from an approved proposal; run treaty distribution and commissions."""
    from reinsurance.engine import distribute_policy

    if proposal.status != Proposal.APPROVED:
        raise ValidationFailed(["Only approved proposals can be issued."])
    policy = Policy.objects.create(
        policy_no=next_policy_number(), proposal=proposal, product=proposal.product, customer=proposal.customer,
        producer=proposal.producer, currency=proposal.currency, fx_rate=proposal.fx_rate, start_date=proposal.start_date,
        end_date=proposal.end_date, net_premium=proposal.net_premium, risk_premium=proposal.risk_premium,
        expense_load=proposal.expense_load, fees=proposal.fees, tax=proposal.tax, total_payable=proposal.total_payable,
        issued_by=user, voucher_discount=proposal.voucher_discount, flex=proposal.flex,
    )
    from . import phase1
    if user_role_is_producer(proposal.producer) and phase1.current_producer(proposal.customer) is None:
        phase1.assign_producer(proposal.customer, proposal.producer, user, "First policy placed through this intermediary")
    for pr in proposal.risks.select_related("vehicle").prefetch_related("drivers"):
        v = pr.vehicle
        if v.customer_id != policy.customer_id:       # the same vehicle profile moves to the new insured
            from .vehicles import transfer_vehicle
            transfer_vehicle(v, policy.customer, policy.start_date, user, f"Policy {policy.policy_no} issued to the new owner")
        risk = PolicyRisk.objects.create(
            policy=policy, vehicle=v, make=v.make, model=v.model, year=v.year, plate=v.registration, chassis_no=v.chassis_no,
            engine_no=v.engine_no, color=v.color, vehicle_type=v.vehicle_type, usage=v.usage, cc=v.cc, seats=v.seats,
            sum_insured=pr.sum_insured, od_premium=pr.od_premium, tp_premium=pr.tp_premium, rider_premium=pr.rider_premium,
            discount_amount=pr.discount_amount, net_premium=pr.net_premium, breakdown=pr.breakdown,
        )
        for d in pr.drivers.all():
            Driver.objects.create(policy_risk=risk, name=d.name, licence_no=d.licence_no, licence_issue_date=d.licence_issue_date,
                                  date_of_birth=d.date_of_birth, nationality=d.nationality, relationship=d.relationship)
        if v.base_value is None and proposal.product.includes_od and pr.sum_insured:
            v.base_value, v.base_date = q3(pr.sum_insured * proposal.fx_rate), proposal.start_date   # depreciation starts here
            v.save(update_fields=["base_value", "base_date"])
        riders = set(pr.riders.values_list("id", flat=True))
        riders |= set(ProductCover.objects.filter(product=proposal.product, mandatory=True).values_list("cover_id", flat=True))
        riders -= set(pr.waived_riders.values_list("id", flat=True))
        risk.riders.set(Cover.objects.filter(id__in=riders, kind="RIDER"))
    proposal.status = Proposal.ISSUED
    proposal.save(update_fields=["status", "updated_at"])
    if proposal.voucher_id:
        v, err = validate_voucher(proposal.voucher.code, proposal.product, proposal.customer)
        if err:
            raise ValidationFailed([err])
        VoucherRedemption.objects.create(voucher=v, policy=policy, customer=proposal.customer, amount=proposal.voucher_discount)
    distribute_policy(policy)
    compute_commissions(policy)
    from accounting import services as acc
    acc.on_policy_issued(policy, user)
    return policy


def commission_rule_for(policy):
    producer_type = policy.producer.profile.producer_type
    rule = CommissionRule.objects.filter(producer_type=producer_type, product=policy.product).first() \
        or CommissionRule.objects.filter(producer_type=producer_type, product__isnull=True).first()
    return rule


def compute_commissions(policy):
    """Commission is calculated at policy level (base, overrider) and aggregated per producer for statements."""
    rule = commission_rule_for(policy)
    if not rule:
        return
    base_net = to_base(policy.net_premium, policy.currency)
    pct = policy.producer.profile.commission_pct or rule.base_pct
    Commission.objects.create(policy=policy, producer=policy.producer, kind="BASE", pct=pct, amount=q3(base_net * pct / HUNDRED))
    mgr = policy.producer.profile.manager
    if mgr and rule.overrider_pct:
        Commission.objects.create(policy=policy, producer=mgr, kind="OVERRIDER", pct=rule.overrider_pct,
                                  amount=q3(base_net * rule.overrider_pct / HUNDRED), note=f"Team production of {policy.producer}")


def profit_commission(producer, start, end):
    """Profit commission = (Earned premium - incurred claims - expenses) x share %, if loss ratio is under threshold."""
    from claims.models import Claim

    policies = Policy.objects.filter(producer=producer, issued_at__date__gte=start, issued_at__date__lte=end)
    earned = sum((to_base(p.net_premium, p.currency) for p in policies), ZERO)
    incurred = sum((to_base(c.approved_repair_cost or c.reserve, c.policy.currency)
                    for c in Claim.objects.filter(policy__in=policies)), ZERO)
    expenses = sum((to_base(p.expense_load, p.currency) for p in policies), ZERO)
    rule = commission_rule_for(policies.first()) if policies.exists() else None
    if not rule or earned == 0:
        return {"earned": earned, "incurred": incurred, "expenses": expenses, "loss_ratio": ZERO, "profit": ZERO}
    lr = incurred / earned * HUNDRED
    profit = max(earned - incurred - expenses, ZERO) * rule.profit_share_pct / HUNDRED if lr <= rule.profit_loss_ratio_threshold else ZERO
    return {"earned": q3(earned), "incurred": q3(incurred), "expenses": q3(expenses), "loss_ratio": q3(lr), "profit": q3(profit)}


# ---------------------------------------------------------------------------
# Endorsements
# ---------------------------------------------------------------------------
def _remaining(policy, eff):
    total = max((policy.end_date - policy.start_date).days, 1)
    remaining = max((policy.end_date - eff).days, 0)
    return D(remaining) / D(total), remaining, total


def _fee(code, currency):
    f = Fee.objects.filter(code=code).first()
    return convert(f.amount, Currency.base(), currency) if f else ZERO


def _next_endorsement_no(policy):
    n = policy.endorsements.count() + 1
    return f"{policy.policy_no}/E{n:02d}"


def quote_endorsement(policy, etype, effective, risk=None, **d):
    """Compute the financial impact of an endorsement without saving it."""
    cur = policy.currency
    frac, remaining, total = _remaining(policy, effective)
    adj = ZERO
    fee_code = "ENDORSEMENT"
    if etype == Endorsement.EXTENSION:
        new_end = d["new_end_date"]
        extra = (new_end - policy.end_date).days
        if extra <= 0:
            raise ValidationFailed(["New end date must be after the current expiry."])
        adj = cur.quantize(policy.net_premium / D(total) * D(extra))
    elif etype == Endorsement.CANCELLATION:
        if effective < policy.start_date or effective > policy.end_date:
            raise ValidationFailed(["Cancellation date must be within the policy period."])
        adj = -cur.quantize(policy.net_premium * frac)
    elif etype == Endorsement.TRANSFER:
        fee_code = "TRANSFER"
    elif etype == Endorsement.USAGE:
        new_usage = d["new_usage"]
        inp = RiskInput(risk.vehicle_type, new_usage, risk.cc, risk.seats, risk.year, risk.sum_insured,
                        list(risk.riders.values_list("id", flat=True)))
        old = RiskInput(risk.vehicle_type, risk.usage, risk.cc, risk.seats, risk.year, risk.sum_insured,
                        list(risk.riders.values_list("id", flat=True)))
        kw = dict(start=policy.start_date, end=policy.end_date)
        new_line = rate_risk(policy.product, inp, cur, policy.fx_rate, **kw)
        old_line = rate_risk(policy.product, old, cur, policy.fx_rate, **kw)
        adj = cur.quantize((new_line["net"] - old_line["net"]) * frac)
    elif etype == Endorsement.ADDITIONS:
        value = D(d["accessories_value"])
        inp_new = RiskInput(risk.vehicle_type, risk.usage, risk.cc, risk.seats, risk.year, risk.sum_insured + value,
                            list(risk.riders.values_list("id", flat=True)))
        inp_old = RiskInput(risk.vehicle_type, risk.usage, risk.cc, risk.seats, risk.year, risk.sum_insured,
                            list(risk.riders.values_list("id", flat=True)))
        kw = dict(start=policy.start_date, end=policy.end_date)
        adj = cur.quantize((rate_risk(policy.product, inp_new, cur, policy.fx_rate, **kw)["net"]
                            - rate_risk(policy.product, inp_old, cur, policy.fx_rate, **kw)["net"]) * frac)
    elif etype == Endorsement.REGNO:
        code, num = (d.get("new_plate_code") or "").upper(), d.get("new_plate_number") or ""
        clash = Vehicle.objects.filter(plate_code=code, plate_number=num).exclude(pk=risk.vehicle_id)
        if clash.exists():
            raise ValidationFailed([f"Registration {num} {code} already belongs to another vehicle ({clash.first().chassis_no})."])
    fee = _fee(fee_code, cur) if etype != Endorsement.CANCELLATION else ZERO
    tax = cur.quantize((adj + fee) * settings.VAT_RATE)
    return {"adjustment": adj, "fee": fee, "tax": tax, "total": cur.quantize(adj + fee + tax), "remaining_days": remaining}


@transaction.atomic
def apply_endorsement(policy, etype, effective, user, risk=None, **d):
    if policy.status not in (Policy.ACTIVE, Policy.PENDING_FAC):
        raise ValidationFailed(["Only active policies can be endorsed."])
    q = quote_endorsement(policy, etype, effective, risk, **d)
    details = {k: (v.isoformat() if isinstance(v, date) else str(v)) for k, v in d.items()}
    if etype == Endorsement.EXTENSION:
        policy.end_date = d["new_end_date"]
    elif etype == Endorsement.CANCELLATION:
        policy.status = Policy.CANCELLED
        policy.cancelled_on = effective
        _clawback(policy, q["adjustment"])
    elif etype == Endorsement.TRANSFER:
        details["from_customer"] = policy.customer.name
        policy.customer = d["new_customer"]
        details["new_customer"] = d["new_customer"].name
        details["new_customer_id"] = d["new_customer"].id
        from .vehicles import transfer_vehicle
        transfer_vehicle(risk.vehicle, d["new_customer"], effective, user, f"Transfer endorsement on {policy.policy_no}")
    elif etype == Endorsement.ENGINE:
        details["old"] = risk.engine_no
        risk.engine_no = d["new_engine_no"]
        risk.vehicle.engine_no = d["new_engine_no"]
        risk.vehicle.save(update_fields=["engine_no"])
        risk.save()
    elif etype == Endorsement.COLOR:
        details["old"] = risk.color
        risk.color = d["new_color"]
        risk.vehicle.color = d["new_color"]
        risk.vehicle.save(update_fields=["color"])
        risk.save()
    elif etype == Endorsement.USAGE:
        details["old"] = risk.usage
        risk.usage = d["new_usage"]
        risk.vehicle.usage = d["new_usage"]
        risk.vehicle.save(update_fields=["usage"])
        risk.save()
    elif etype == Endorsement.ADDITIONS:
        risk.accessories_value += D(d["accessories_value"])
        risk.sum_insured += D(d["accessories_value"])
        risk.save()
    elif etype == Endorsement.REGNO:
        code, num = (d.get("new_plate_code") or "").upper(), d.get("new_plate_number") or ""
        details["old"] = risk.plate
        v = risk.vehicle
        v.plate_code, v.plate_number = code, num
        v.save(update_fields=["plate_code", "plate_number"])
        risk.plate = v.registration
        risk.save()
    elif etype == Endorsement.ADDDRIVER:
        dob = d["driver_dob"]
        details["young_driver"] = (clock.today().year - dob.year - ((clock.today().month, clock.today().day) < (dob.month, dob.day))) < settings.YOUNG_DRIVER_AGE
    policy.version += 1
    policy.net_premium = policy.currency.quantize(policy.net_premium + q["adjustment"])
    policy.fees += q["fee"]
    policy.tax += q["tax"]
    policy.total_payable = policy.currency.quantize(policy.total_payable + q["total"])
    policy.save()
    endo = Endorsement.objects.create(
        number=_next_endorsement_no(policy), policy=policy, risk=risk, endorsement_type=etype, effective_date=effective,
        details=details, premium_adjustment=q["adjustment"], fee=q["fee"], tax=q["tax"], total=q["total"],
        version_after=policy.version, created_by=user,
    )
    if etype == Endorsement.ADDDRIVER:
        Driver.objects.create(policy_risk=risk, name=d["driver_name"], licence_no=d["driver_licence"], date_of_birth=d["driver_dob"],
                              relationship=d.get("driver_relationship") or "OTHER", nationality=d.get("driver_nationality", ""),
                              added_on=effective, endorsement=endo)
    if etype != Endorsement.CANCELLATION and q["adjustment"] != 0:
        from reinsurance.engine import distribute_policy
        distribute_policy(policy, replace=True)
    from accounting import services as acc
    acc.on_endorsement(endo, user)
    return endo


def _clawback(policy, refund_adj):
    """Claw back commission pro-rata on cancellation (addendum 3.1)."""
    if policy.net_premium == 0:
        return
    ratio = abs(refund_adj) / policy.net_premium
    for c in policy.commissions.filter(kind__in=["BASE", "OVERRIDER"]):
        Commission.objects.create(policy=policy, producer=c.producer, kind="CLAWBACK", pct=c.pct,
                                  amount=-q3(c.amount * ratio), note=f"Clawback on cancellation of {policy.policy_no}")


# ---------------------------------------------------------------------------
# Renewal
# ---------------------------------------------------------------------------
@transaction.atomic
def renew_policy_as_quote(policy, user):
    """On-click renewal: a new quote pre-filled with the expiring policy's data (FRD 7.2).

    For comprehensive cover the sum insured is the *depreciated* market value of each vehicle (reducing balance at the
    product's % p.a.), never above the previous sum insured. Additional drivers and flex values are carried over."""
    start = max(policy.end_date + timedelta(days=1), clock.today())
    end = start + timedelta(days=365) - timedelta(days=1)
    cur = policy.currency
    p = Proposal.objects.create(
        number=next_quote_number(), status=Proposal.QUOTE, product=policy.product, customer=policy.customer, producer=user,
        currency=cur, fx_rate=cur.rate_to_base, start_date=start, end_date=end,
        valid_until=clock.today() + timedelta(days=policy.product.quote_validity_days),
        ncd_years=0, renewal_of=policy, flex=policy.flex,
    )
    had_claims = policy.claims.exists()
    for r in policy.risks.filter(active=True).select_related("vehicle").prefetch_related("drivers"):
        si, note = r.sum_insured, ""
        if policy.product.includes_od:
            val, note = vehicle_value(r.vehicle, start, policy.product, cur, cur.rate_to_base, fallback_si=r.sum_insured,
                                      fallback_date=policy.start_date)
            if val is not None:
                si = min(val, r.sum_insured) if r.sum_insured else val
        pr = ProposalRisk.objects.create(proposal=p, vehicle=r.vehicle, sum_insured=si, si_note=note[:160])
        pr.riders.set(r.riders.all())
        for d in r.drivers.all():
            Driver.objects.create(proposal_risk=pr, name=d.name, licence_no=d.licence_no, licence_issue_date=d.licence_issue_date,
                                  date_of_birth=d.date_of_birth, nationality=d.nationality, relationship=d.relationship)
    p.ncd_years = 0 if had_claims else 1
    p.save(update_fields=["ncd_years"])
    reprice_proposal(p)
    return p


def user_role_is_producer(user):
    from core.models import user_role
    return user_role(user) in ("AGENT", "BROKER")

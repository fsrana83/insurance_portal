"""Property & Casualty business rules: rating, approval, payment terms, issue, endorsements, reinsurance distribution."""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core import clock
from core.models import Currency, DocumentSequence, convert, to_base, user_role

from .models import (PncClass, PncCession, PncCredit, PncEndorsement, PncInstallment, PncPolicy, PncReceipt, PncRisk)

D = Decimal
ZERO = D("0")
H = D(100)
MAX_INSTALLMENTS = 12
APPROVERS = ("PRODUCTION_MANAGER", "UNDERWRITER", "ADMIN")
CREDIT_APPROVERS = ("CREDIT_CONTROL", "FINANCE", "ADMIN")
BASES = ("CASH", "CREDIT")


class PncError(Exception):
    pass


def q(cur, x):
    return cur.quantize(D(x))


def seq(prefix):
    return f"{prefix}/{clock.today().year}/{DocumentSequence.next(prefix):06d}"


def base(policy, amount):
    return Currency.base().quantize(D(amount) * D(policy.fx_rate))


# ---- rating ---------------------------------------------------------------------------------------------------
def term_factor(policy, start=None, end=None):
    days = ((end or policy.end_date) - (start or policy.start_date)).days + 1
    return D(1) if 364 <= days <= 366 else D(max(days, 1)) / D(365)


def risk_annual(policy, risk):
    """Annual premium of a risk: sum insured x rate x (1 + loading), not less than the class minimum."""
    cls, cur = policy.pnc_class, policy.currency
    rate = risk.rate_pct or cls.rate_pct
    prem = D(risk.sum_insured) * rate / H * (D(1) + D(risk.loading_pct) / H)
    return q(cur, max(prem, convert(cls.min_premium, Currency.base(), cur)))


def rate_policy(policy):
    cur = policy.currency
    f = term_factor(policy)
    net = ZERO
    for r in policy.risks.filter(active=True):
        r.rate_pct = r.rate_pct or policy.pnc_class.rate_pct
        r.annual_premium = risk_annual(policy, r)
        r.premium = q(cur, r.annual_premium * f)
        r.save(update_fields=["rate_pct", "annual_premium", "premium"])
        net += r.premium
    policy.net_premium = q(cur, net)
    policy.fees = convert(settings.PNC_POLICY_FEE, Currency.base(), cur) if net else ZERO
    policy.tax = q(cur, (policy.net_premium + policy.fees) * settings.VAT_RATE)
    policy.total_payable = policy.net_premium + policy.fees + policy.tax
    policy.save(update_fields=["net_premium", "fees", "tax", "total_payable"])
    return policy


def approval_reasons(policy, user=None):
    cls, out = policy.pnc_class, []
    if cls.referral:
        out.append(f"{cls.name} is always referred to an underwriter.")
    total_si = sum((to_base(r.sum_insured, policy.currency) for r in policy.risks.filter(active=True)), ZERO)
    if total_si > settings.PNC_AGENT_AUTHORITY:
        out.append(f"Total sum insured {total_si} {settings.BASE_CURRENCY} exceeds the agent authority of {settings.PNC_AGENT_AUTHORITY}.")
    for r in policy.risks.filter(active=True):
        if abs(D(r.loading_pct)) > settings.PNC_LOADING_LIMIT:
            out.append(f"Risk {r.number}: loading / discount of {r.loading_pct}% is beyond {settings.PNC_LOADING_LIMIT}%.")
    return out


def can_decide(user, policy):
    return user_role(user) in APPROVERS and (user.pk != policy.producer_id or user.is_superuser or user_role(user) == "UNDERWRITER")


@transaction.atomic
def submit(policy, user):
    if policy.status not in (PncPolicy.QUOTE, PncPolicy.REJECTED):
        raise PncError("This quote cannot be submitted.")
    if not policy.risks.filter(active=True).exists():
        raise PncError("Add at least one risk.")
    if policy.end_date <= policy.start_date:
        raise PncError("The expiry date must be after the start date.")
    rate_policy(policy)
    reasons = approval_reasons(policy, user)
    if reasons and user_role(user) not in APPROVERS:
        policy.status, policy.approval_reasons = PncPolicy.PENDING, reasons
    else:
        policy.status, policy.approval_reasons, policy.decision_by = PncPolicy.APPROVED, reasons, user if reasons else None
    policy.save(update_fields=["status", "approval_reasons", "decision_by"])
    return policy


@transaction.atomic
def decide(policy, user, approve, note=""):
    if policy.status != PncPolicy.PENDING:
        raise PncError("Only a quote waiting for approval can be decided.")
    if not can_decide(user, policy):
        raise PncError("You cannot decide this quote.")
    if not approve and not note.strip():
        raise PncError("Give a reason when rejecting.")
    policy.status = PncPolicy.APPROVED if approve else PncPolicy.REJECTED
    policy.decision_by, policy.decision_note = user, note[:300]
    policy.save(update_fields=["status", "decision_by", "decision_note"])
    return policy


# ---- payment terms ------------------------------------------------------------------------------------------------
def request_credit(policy, user, installments, advance=0, note=""):
    if policy.status != PncPolicy.APPROVED:
        raise PncError("Credit can be requested once the quote is approved.")
    n = int(installments)
    if not 1 <= n <= MAX_INSTALLMENTS:
        raise PncError(f"Instalments must be between 1 and {MAX_INSTALLMENTS}.")
    advance = q(policy.currency, advance or 0)
    if advance < 0 or advance >= policy.total_payable:
        raise PncError("The advance must be below the total premium.")
    PncCredit.objects.update_or_create(policy=policy, defaults=dict(installments=n, advance_amount=advance, status=PncCredit.PENDING, requested_by=user,
                                                                     request_note=note[:200], decided_by=None, decided_on=None, decision_note=""))
    return policy.credit


def decide_credit(credit, user, approve, note=""):
    if user_role(user) not in CREDIT_APPROVERS:
        raise PncError("Only Credit Control or Finance can decide credit.")
    if credit.requested_by_id == user.id and not user.is_superuser:
        raise PncError("A second person must decide credit; you requested it.")
    if credit.status != PncCredit.PENDING:
        raise PncError("This request was already decided.")
    if not approve and not note.strip():
        raise PncError("Give a reason for rejecting credit.")
    credit.status = PncCredit.APPROVED if approve else PncCredit.REJECTED
    credit.decided_by, credit.decided_on, credit.decision_note = user, timezone.now(), note[:200]
    credit.save()
    return credit


def build_schedule(policy, n, advance=0, start=None):
    start = start or clock.today()
    cur = policy.currency
    advance = q(cur, advance or 0)
    policy.installments.all().delete()
    rows = []
    if advance > 0:
        rows.append(PncInstallment(policy=policy, number=0, due_date=start, amount=advance))
    rest = policy.total_payable - advance
    each = q(cur, rest / n)
    for i in range(1, n + 1):
        rows.append(PncInstallment(policy=policy, number=i, due_date=start + timedelta(days=30 * i), amount=each if i < n else rest - each * (n - 1)))
    PncInstallment.objects.bulk_create(rows)


def schedule_status(policy, today=None, paid=None):
    """[(instalment, paid, balance, days overdue)], receipts applied oldest first."""
    today = today or clock.today()
    left = policy.paid_amount if paid is None else paid
    out = []
    for it in policy.installments.all():
        took = min(left, it.amount)
        left -= took
        rem = it.amount - took
        out.append((it, took, rem, max((today - it.due_date).days, 0) if rem > 0 else 0))
    return out


METHODS = ("CASH", "CHEQUE", "TRANSFER", "POS")


def record_receipt(policy, amount, method, user=None, reference="", narration="", received_on=None):
    from . import ledger
    amount = q(policy.currency, amount)
    if method not in METHODS:
        raise PncError("Choose how the money was received.")
    if method in ("CHEQUE", "TRANSFER") and not reference:
        raise PncError("Enter the cheque / transfer reference.")
    if amount <= 0:
        raise PncError("Enter an amount above zero.")
    if amount > policy.balance_due + D("0.0005"):
        raise PncError(f"The amount exceeds the balance due ({policy.balance_due}).")
    r = PncReceipt.objects.create(policy=policy, number=seq("PRV"), amount=amount, method=method, reference=reference, narration=narration[:160],
                                  received_on=received_on or clock.today(), created_by=user, is_demo=policy.is_demo)
    ledger.on_receipt(policy, r, user)
    return r


def check_terms(policy, basis, method="", reference=""):
    """Amount that must be received now to issue under the chosen basis (raises PncError if the terms are not met)."""
    if not getattr(settings, "REQUIRE_PAYMENT_OR_CREDIT", True):
        return ZERO
    if basis == "CASH":
        if method not in METHODS:
            raise PncError("Choose how the premium was received: a receipt is required to issue on a cash basis.")
        if method in ("CHEQUE", "TRANSFER") and not reference:
            raise PncError("Enter the cheque / transfer reference of the receipt.")
        return policy.total_payable
    if basis == "CREDIT":
        cr = getattr(policy, "credit", None)
        if not cr or cr.status != PncCredit.APPROVED:
            raise PncError("Credit Control approval is required to issue on credit.")
        if cr.advance_amount > 0:
            if method not in METHODS:
                raise PncError(f"Collect the advance of {cr.advance_amount} and choose how it was received.")
            if method in ("CHEQUE", "TRANSFER") and not reference:
                raise PncError("Enter the cheque / transfer reference of the advance receipt.")
        return cr.advance_amount
    raise PncError("Choose cash (receipt) or credit (Credit Control approval).")


@transaction.atomic
def issue(policy, user, basis="CASH", method="", reference="", fac_reference=""):
    from . import ledger
    if policy.status != PncPolicy.APPROVED:
        raise PncError("Only an approved quote can be issued.")
    due_now = check_terms(policy, basis, method, reference)
    rate_policy(policy)
    policy.policy_no = seq("PNC")
    policy.status, policy.issued_by, policy.issued_at = PncPolicy.ACTIVE, user, timezone.now()
    policy.save(update_fields=["policy_no", "status", "issued_by", "issued_at"])
    if fac_reference:
        policy.fac_reference = fac_reference.strip()[:80]
        policy.save(update_fields=["fac_reference"])
    distribute(policy)
    if policy.cessions.filter(is_facultative=True).exists() and not policy.fac_reference and getattr(settings, "REQUIRE_FAC_CONFIRMATION", True):
        raise PncError("Part of the sum insured exceeds treaty capacity. Enter the confirmation reference of the signed facultative placement before issuing.")
    ledger.sync_policy(policy, user, "ISSUE", f"Policy {policy.policy_no} issued to {policy.customer.name}")
    if due_now > 0 and method:
        record_receipt(policy, due_now, method, user, reference, "Premium receipt at issue" if basis == "CASH" else "Advance at issue")
    if basis == "CREDIT":
        cr = policy.credit
        build_schedule(policy, cr.installments, cr.advance_amount)
    return policy


# ---- reinsurance distribution --------------------------------------------------------------------------------------
def _groups_ok(treaty, policy):
    groups = [g.strip().upper() for g in (treaty.pnc_groups or "").split(",") if g.strip()]
    return not groups or policy.pnc_class.group in groups


@transaction.atomic
def distribute(policy):
    """Quota share, then surplus on the sum insured (retention and capacity), then facultative for the excess. Base currency."""
    from reinsurance.engine import active_treaties
    from reinsurance.models import Treaty

    policy.cessions.all().delete()
    treaties = [t for t in active_treaties(policy.start_date, [Treaty.QUOTA, Treaty.SURPLUS], business="PNC") if _groups_ok(t, policy)]
    for risk in policy.risks.filter(active=True):
        si = to_base(risk.sum_insured, policy.currency)
        retained_si, retained_prem = si, to_base(risk.premium, policy.currency)
        for t in treaties:
            if t.treaty_type == Treaty.QUOTA:
                pct = t.cession_pct / H
                PncCession.objects.create(policy=policy, risk=risk, treaty=t, treaty_type="QUOTA", rank=t.rank, sum_insured_ceded=q(Currency.base(), retained_si * pct),
                                          premium_ceded=q(Currency.base(), retained_prem * pct), cession_pct=t.cession_pct)
                retained_si -= retained_si * pct
                retained_prem -= retained_prem * pct
            elif t.treaty_type == Treaty.SURPLUS and retained_si > t.retention > 0:
                surplus = retained_si - t.retention
                ceded = min(surplus, t.capacity) if t.capacity else surplus
                ratio = ceded / retained_si
                PncCession.objects.create(policy=policy, risk=risk, treaty=t, treaty_type="SURPLUS", rank=t.rank, sum_insured_ceded=q(Currency.base(), ceded),
                                          premium_ceded=q(Currency.base(), retained_prem * ratio), cession_pct=q(Currency.base(), ratio * H), note=f"Retention {t.retention}")
                over = surplus - ceded
                retained_prem -= retained_prem * ratio
                retained_si -= ceded
                if over > 0:
                    fr = over / (retained_si + over)
                    PncCession.objects.create(policy=policy, risk=risk, treaty=None, treaty_type="FAC", rank=t.rank + 1, sum_insured_ceded=q(Currency.base(), over),
                                              premium_ceded=q(Currency.base(), retained_prem * fr), cession_pct=q(Currency.base(), fr * H), is_facultative=True,
                                              note="Exceeds treaty capacity: facultative placement required")
                    retained_prem -= retained_prem * fr
                    retained_si -= over


# ---- endorsements --------------------------------------------------------------------------------------------------
def _remaining(policy, effective):
    """Fraction of the policy period still to run from the effective date, 0-1."""
    total = policy.period_days
    left = (policy.end_date - effective).days + 1
    return D(min(max(left, 0), total)) / D(total)


@transaction.atomic
def endorse(policy, user, etype, effective, description="", risk=None, new_si=None, new_end=None, risk_data=None, conditions=None):
    from . import ledger
    if policy.status != PncPolicy.ACTIVE:
        raise PncError("Only an active policy can be endorsed.")
    if not (policy.start_date <= effective <= policy.end_date):
        raise PncError("The effective date must fall inside the policy period.")
    cur, adj = policy.currency, ZERO
    f = _remaining(policy, effective)
    desc = description
    if etype == "SI_CHANGE":
        if not risk or not new_si or D(new_si) <= 0:
            raise PncError("Choose the risk and enter the new sum insured.")
        old = D(risk.premium)
        risk.sum_insured = q(cur, new_si)
        risk.annual_premium = risk_annual(policy, risk)
        new_full = q(cur, risk.annual_premium * term_factor(policy))
        adj = q(cur, (new_full - old) * f)
        risk.premium = old + adj
        risk.save()
        desc = desc or f"Risk {risk.number}: sum insured changed to {risk.sum_insured}"
    elif etype == "ADD_RISK":
        d = risk_data or {}
        n = (policy.risks.order_by("-number").first().number if policy.risks.exists() else 0) + 1
        risk = PncRisk(policy=policy, number=n, description=d["description"], location=d.get("location", ""), sum_insured=q(cur, d["sum_insured"]),
                       value_at_risk=q(cur, d.get("value_at_risk") or 0), loading_pct=d.get("loading_pct") or 0, details=d.get("details") or {})
        risk.rate_pct = policy.pnc_class.rate_pct
        risk.annual_premium = risk_annual(policy, risk)
        adj = q(cur, risk.annual_premium * term_factor(policy) * f)
        risk.premium = adj
        risk.save()
        desc = desc or f"Risk {n} added: {risk.description}"
    elif etype == "REMOVE_RISK":
        if not risk or not risk.active:
            raise PncError("Choose an active risk to remove.")
        if policy.risks.filter(active=True).count() < 2:
            raise PncError("A policy needs at least one risk: cancel the policy instead.")
        adj = -q(cur, risk.premium * f)
        risk.premium += adj
        risk.active = False
        risk.save()
        desc = desc or f"Risk {risk.number} removed"
    elif etype == "EXTEND":
        if not new_end or new_end <= policy.start_date:
            raise PncError("Enter a new expiry date after the start date.")
        old_end = policy.end_date
        annual = sum((D(r.annual_premium) for r in policy.risks.filter(active=True)), ZERO)
        adj = q(cur, annual * D((new_end - old_end).days) / D(365))
        policy.end_date = new_end
        desc = desc or f"Expiry moved from {old_end:%d %b %Y} to {new_end:%d %b %Y}"
    elif etype == "DETAILS":
        if conditions is not None:
            policy.conditions = conditions
        desc = desc or "Details / conditions amended"
    elif etype == "CANCEL":
        adj = -q(cur, policy.net_premium * f)
        policy.status, policy.cancelled_on = PncPolicy.CANCELLED, effective
        desc = desc or f"Cancelled with effect from {effective:%d %b %Y}"
    else:
        raise PncError("Unknown endorsement type.")
    tax = q(cur, adj * settings.VAT_RATE)
    e = PncEndorsement.objects.create(policy=policy, number=seq("PNE"), endorsement_type=etype, effective_date=effective, description=desc[:250],
                                      premium_adjustment=adj, fee=ZERO, tax=tax, total=adj + tax, created_by=user)
    policy.net_premium += adj
    policy.tax += tax
    policy.total_payable += adj + tax
    policy.version += 1
    policy.save()
    distribute(policy)
    ledger.sync_policy(policy, user, "ENDORSE", f"Endorsement {e.number}: {e.get_endorsement_type_display()}")
    return e


# ---- claims -----------------------------------------------------------------------------------------------------------
CLAIM_HANDLERS = ("CLAIMS_HANDLER", "CLAIMS_MANAGER", "ADMIN")
APPROVAL_LIMIT = D("25000")      # base currency: above this a claims manager must approve


def deductible_for(claim, loss):
    """Deductible on a loss: the risk's own deductible if set, otherwise class percentage subject to the class minimum."""
    cls, cur = claim.policy.pnc_class, claim.policy.currency
    if claim.risk.deductible:
        return min(D(claim.risk.deductible), D(loss))
    d = max(D(loss) * cls.deductible_pct / H, convert(cls.deductible_min, Currency.base(), cur))
    return q(cur, min(d, D(loss)))


def average_factor(claim):
    """Under-insurance (average): sum insured / value at risk when the class applies average and the value is higher."""
    r = claim.risk
    if claim.policy.pnc_class.average_applies and r.value_at_risk and r.value_at_risk > r.sum_insured > 0:
        return (D(r.sum_insured) / D(r.value_at_risk)).quantize(D("0.0001"), rounding=ROUND_HALF_UP)
    return D(1)


def salvage_total(claim):
    return sum((D(r.amount) for r in claim.recoveries.exclude(recovery_type="REINSURANCE")), ZERO)


def check_cover(policy, risk, loss_date):
    if not policy.is_issued:
        raise PncError("The policy has not been issued.")
    if risk.policy_id != policy.pk or not risk.active:
        raise PncError("Choose an active risk of this policy.")
    if not (policy.start_date <= loss_date <= policy.end_date):
        raise PncError("The date of loss falls outside the policy period.")
    if policy.status == PncPolicy.CANCELLED and policy.cancelled_on and loss_date >= policy.cancelled_on:
        raise PncError("The policy was cancelled before the loss.")
    if loss_date > clock.today():
        raise PncError("The date of loss is in the future.")


@transaction.atomic
def register_claim(policy, risk, user, date_of_loss, cause, reserve, description="", location="", reported_on=None, is_legal=False):
    from . import ledger
    from .models import PncClaim
    check_cover(policy, risk, date_of_loss)
    if D(reserve) <= 0:
        raise PncError("Enter the estimated loss (reserve).")
    dup = PncClaim.objects.filter(policy=policy, risk=risk, date_of_loss=date_of_loss, cause=cause).exclude(status=PncClaim.REJECTED).first()
    if dup:
        raise PncError(f"Possible duplicate of claim {dup.claim_no} (same risk, date and cause).")
    c = PncClaim.objects.create(claim_no=seq("PCL"), policy=policy, risk=risk, date_of_loss=date_of_loss, reported_on=reported_on or clock.today(), cause=cause,
                                reserve=q(policy.currency, reserve), description=description, location=location, is_legal=is_legal, registered_by=user, is_demo=policy.is_demo)
    ledger.sync_claim(c, user, "registered")
    return c


def assert_open(claim):
    if claim.locked:
        raise PncError(f"Claim {claim.claim_no} is {claim.get_status_display().lower()}: reopen it to make changes.")


@transaction.atomic
def set_reserve(claim, user, reserve):
    from . import ledger
    assert_open(claim)
    if claim.status in (claim.APPROVED,):
        raise PncError("An approved claim's amount is fixed; reopen it to change.")
    claim.reserve = q(claim.policy.currency, reserve)
    claim.save(update_fields=["reserve"])
    ledger.sync_claim(claim, user, "reserve revised")


@transaction.atomic
def assess(claim, user, assessed_loss, adjuster=None, report_on=None):
    from . import ledger
    assert_open(claim)
    if claim.status not in (claim.OPEN, claim.ASSESSED):
        raise PncError("This claim cannot be assessed in its current status.")
    if D(assessed_loss) <= 0:
        raise PncError("Enter the assessed loss.")
    claim.assessed_loss = q(claim.policy.currency, assessed_loss)
    claim.average_factor = average_factor(claim)
    adjusted = q(claim.policy.currency, claim.assessed_loss * claim.average_factor)
    claim.deductible_applied = deductible_for(claim, adjusted)
    cap = D(claim.risk.sum_insured)
    claim.net_payable = max(min(adjusted - claim.deductible_applied, cap), ZERO)
    claim.adjuster, claim.adjuster_report_on, claim.status = adjuster, report_on or clock.today(), claim.ASSESSED
    claim.save()
    ledger.sync_claim(claim, user, "assessed")
    return claim


@transaction.atomic
def approve(claim, user):
    from . import ledger
    if claim.status != claim.ASSESSED:
        raise PncError("Assess the claim first.")
    if user_role(user) not in CLAIM_HANDLERS:
        raise PncError("You cannot approve claims.")
    if to_base(claim.net_payable, claim.policy.currency) > APPROVAL_LIMIT and user_role(user) not in ("CLAIMS_MANAGER", "ADMIN"):
        raise PncError(f"Claims above {APPROVAL_LIMIT} {settings.BASE_CURRENCY} need a claims manager.")
    if claim.registered_by_id == user.id and not user.is_superuser and to_base(claim.net_payable, claim.policy.currency) > APPROVAL_LIMIT:
        raise PncError("A second person must approve this claim.")
    claim.status, claim.approved_by = claim.APPROVED, user
    claim.save(update_fields=["status", "approved_by"])
    ledger.sync_claim(claim, user, "approved")
    return claim


@transaction.atomic
def reject_claim(claim, user, reason):
    from . import ledger
    assert_open(claim)
    if not reason.strip():
        raise PncError("Give a reason for rejecting the claim.")
    claim.status, claim.reopen_reason = claim.REJECTED, reason[:200]
    claim.net_payable = ZERO
    claim.save(update_fields=["status", "reopen_reason", "net_payable"])
    ledger.sync_claim(claim, user, "rejected")


@transaction.atomic
def pay(claim, user, amount, payee, stage="INTERIM", reference="", paid_on=None):
    from . import ledger
    from .models import PncClaimPayment
    if claim.status != claim.APPROVED:
        raise PncError("Payments are made on approved claims only.")
    amount = q(claim.policy.currency, amount)
    if amount <= 0 or amount > claim.outstanding + D("0.0005"):
        raise PncError(f"Enter an amount up to the outstanding {claim.outstanding}.")
    if stage == "FINAL" and amount != claim.outstanding:
        raise PncError("A final payment must clear the outstanding amount.")
    if reference and claim.payments.filter(reference=reference, payee=payee, amount=amount).exists():
        raise PncError("A payment with the same reference, payee and amount already exists: duplicate payments are blocked.")
    p = PncClaimPayment.objects.create(claim=claim, stage=stage, amount=amount, payee=payee, reference=reference, paid_on=paid_on or clock.today())
    ledger.on_payment(claim, p, user)
    from . import nonmotor
    nonmotor.book_recoveries(claim, p)
    if claim.outstanding <= 0:
        claim.status = claim.SETTLED
        claim.save(update_fields=["status"])
        ledger.sync_claim(claim, user, "settled")
    return p


@transaction.atomic
def add_recovery(claim, user, rtype, amount, received_on=None, note=""):
    from . import ledger
    from .models import PncRecovery
    amount = q(claim.policy.currency, amount)
    if amount <= 0:
        raise PncError("Enter an amount above zero.")
    r = PncRecovery.objects.create(claim=claim, recovery_type=rtype, amount=amount, received_on=received_on or clock.today(), note=note[:150])
    ledger.on_recovery(claim, r, user)
    if rtype != "REINSURANCE":
        ledger.sync_claim(claim, user, "recovery")
    return r


@transaction.atomic
def close(claim, user):
    from . import ledger
    if claim.status not in (claim.SETTLED, claim.APPROVED):
        raise PncError("Only a settled claim can be closed.")
    if claim.status == claim.APPROVED:
        if claim.outstanding > 0:
            raise PncError("The claim still has an outstanding amount.")
    claim.status = claim.CLOSED
    claim.save(update_fields=["status"])
    ledger.sync_claim(claim, user, "closed")


@transaction.atomic
def reopen(claim, user, reason):
    from . import ledger
    if not claim.locked:
        raise PncError("The claim is not locked.")
    if not reason.strip():
        raise PncError("Give a reason for reopening.")
    claim.status = claim.APPROVED if (claim.approved_by_id and claim.net_payable) else (claim.ASSESSED if claim.assessed_loss else claim.OPEN)
    claim.reopen_count += 1
    claim.reopen_reason = reason[:200]
    claim.save()
    ledger.sync_claim(claim, user, "reopened")


# ---- claim reinsurance -----------------------------------------------------------------------------------------------
def incurred_base(claim):
    if claim.status == claim.REJECTED:
        return ZERO
    amt = claim.net_payable if claim.status in (claim.APPROVED, claim.SETTLED, claim.CLOSED) else max(claim.reserve - deductible_for(claim, claim.reserve), ZERO)
    return to_base(amt, claim.policy.currency)


def claim_layers(claim, loss_base):
    """(treaty, kind, amount): quota share, then surplus (by the cession ratios of the risk), then per-risk XoL on what remains. Base currency."""
    from reinsurance.engine import active_treaties
    from reinsurance.models import Treaty
    net = max(D(loss_base) - to_base(salvage_total(claim), claim.policy.currency), ZERO)
    out = []
    if net <= 0:
        return out
    prem = to_base(claim.risk.premium, claim.policy.currency)
    remaining = net
    if prem:
        ces = [c for c in claim.policy.cessions.select_related("treaty").order_by("rank", "id") if not c.is_facultative and c.treaty_id and c.risk_id == claim.risk_id]
        for kind in ("QUOTA", "SURPLUS"):
            layer = {}
            for c in ces:
                if c.treaty_type == kind:
                    layer[c.treaty] = layer.get(c.treaty, ZERO) + D(c.premium_ceded) / prem
            tot = sum(layer.values(), ZERO)
            scale = D(1) / tot if tot > 1 else D(1)
            base_amt = remaining
            for t, f in layer.items():
                amt = min(net * f * scale, base_amt)
                if kind == "QUOTA" and t.qs_limit:
                    amt = min(amt, t.qs_limit)
                amt = amt.quantize(D("0.001"), rounding=ROUND_HALF_UP)
                if amt > 0:
                    out.append((t, kind, amt))
                    remaining -= amt
    if net > 0:
        for t in active_treaties(claim.date_of_loss, [Treaty.XOL], business="PNC"):
            if t.xol_kind != Treaty.RISK or not _groups_ok(t, claim.policy):
                continue
            subject = net if t.inuring == "GROSS" else remaining          # contractual inuring order, per treaty
            if t.xol_retention and subject > t.xol_retention:
                amt = subject - t.xol_retention
                if t.xol_limit:
                    amt = min(amt, t.xol_limit)
                cap = net - sum((a for _, _, a in out), ZERO)                 # never recover more than the loss in total
                amt = min(amt, max(cap, ZERO))
                if amt > 0:
                    out.append((t, "XOL", amt.quantize(D("0.001"), rounding=ROUND_HALF_UP)))
    return out


def ri_total(claim, loss_base):
    return sum((a for _, _, a in claim_layers(claim, loss_base)), ZERO)


def retained_loss(claim):
    """Net retained loss in base currency after salvage and reinsurance."""
    base_amt = incurred_base(claim)
    net = max(base_amt - to_base(salvage_total(claim), claim.policy.currency), ZERO)
    return max(net - ri_total(claim, base_amt), ZERO)

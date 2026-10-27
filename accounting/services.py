"""Double-entry postings for the motor book, laid out for IFRS 17 (premium allocation approach, PAA).

What is modelled
    * Insurance contract liability, remaining coverage (LRC): premium written -> LRC; released to revenue over the cover period.
    * Premiums receivable / cash (receipts) and output VAT.
    * Insurance acquisition cash flows (commission): expensed as incurred when the coverage period is <= 1 year
      (IFRS 17.59(a), ``IFRS17_EXPENSE_IACF``) or deferred and amortised.
    * Reinsurance contracts held: ceded premium -> asset for remaining coverage, allocated to expense over the cover period;
      claim recoveries -> reinsurance asset for incurred claims.
    * Incurred claims: reserve / approved amount -> liability for incurred claims (LIC); payments settle LIC;
      salvage / subrogation recoveries reduce the claims expense.

What is NOT modelled (needs actuarial input): discounting, the risk adjustment, loss components for onerous contracts,
reinsurance non-performance risk, and the contractual service margin (not applicable under the PAA).
All amounts are posted in the base currency (OMR) at the policy's booking rate.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from core.models import Currency, DocumentSequence

from .models import Account, FinancialDocument, JournalEntry, JournalLine

D = Decimal
ZERO = D("0")

CHART = [
    ("1110", "Cash and bank", "ASSET", "Cash and cash equivalents"),
    ("1120", "Premiums receivable", "ASSET", "Insurance contracts: part of the LRC carrying amount"),
    ("1130", "Deferred acquisition costs", "ASSET", "Insurance contracts: part of the LRC carrying amount"),
    ("1210", "Reinsurance contract assets - remaining coverage", "ASSET", "Reinsurance contract assets (ARC)"),
    ("1230", "Reinsurance recoverable - incurred claims", "ASSET", "Reinsurance contract assets (AIC)"),
    ("2110", "Insurance contract liabilities - remaining coverage (LRC)", "LIABILITY", "Insurance contract liabilities (LRC)"),
    ("2120", "Insurance contract liabilities - incurred claims (LIC)", "LIABILITY", "Insurance contract liabilities (LIC)"),
    ("2210", "VAT output payable", "LIABILITY", "Other liabilities"),
    ("2220", "Commission payable", "LIABILITY", "Other payables"),
    ("2230", "Reinsurance premium payable", "LIABILITY", "Other payables"),
    ("4110", "Insurance revenue", "INCOME", "Insurance revenue"),
    ("4210", "Amounts recoverable from reinsurers", "INCOME", "Net expenses from reinsurance contracts held"),
    ("4300", "Policy fee income", "INCOME", "Other income"),
    ("5110", "Incurred claims and other insurance service expenses", "EXPENSE", "Insurance service expenses"),
    ("5120", "Insurance acquisition cash flows", "EXPENSE", "Insurance service expenses"),
    ("5130", "Allocation of reinsurance premiums", "EXPENSE", "Net expenses from reinsurance contracts held"),
]

from core.clock import posting_date, today  # noqa: F401  (re-exported)


def q3(x):
    return D(x).quantize(D("0.001"), rounding=ROUND_HALF_UP)


def ensure_accounts():
    if Account.objects.count() >= len(CHART):
        return
    for code, name, cat, line in CHART:
        Account.objects.update_or_create(code=code, defaults=dict(name=name, category=cat, ifrs17_line=line))


def _base(amount, policy):
    return q3(D(amount) * D(policy.fx_rate))


def post(entry_type, narration, lines, *, policy=None, claim=None, source="", user=None, on=None):
    """Create a balanced journal entry. ``lines`` = [(account_code, 'D'|'C', amount_base)]; zero amounts are dropped."""
    ensure_accounts()
    lines = [(c, side, q3(a)) for c, side, a in lines if a and q3(a) != 0]
    if not lines:
        return None
    # negative amounts flip the side
    norm = []
    for c, side, a in lines:
        if a < 0:
            side, a = ("C" if side == "D" else "D"), -a
        norm.append((c, side, a))
    dr = sum((a for _, s, a in norm if s == "D"), ZERO)
    cr = sum((a for _, s, a in norm if s == "C"), ZERO)
    if dr != cr:
        raise ValueError(f"Unbalanced journal ({narration}): debit {dr} credit {cr}")
    pol = policy or (claim.policy if claim else None)
    entry = JournalEntry.objects.create(
        number=f"JV/{(on or today()).year}/{DocumentSequence.next('JV'):07d}", date=on or today(), entry_type=entry_type,
        narration=narration[:200], policy=policy or (claim.policy if claim else None), claim=claim, source=source[:40],
        currency=pol.currency if pol else None, fx_rate=pol.fx_rate if pol else 1, created_by=user,
        is_demo=bool(pol and pol.is_demo),
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account_id=c, debit=a if s == "D" else ZERO, credit=a if s == "C" else ZERO) for c, s, a in norm
    ])
    return entry


def _signed(code, credit_amount):
    """Line helper: positive = credit, negative = debit."""
    return (code, "C", credit_amount)


def _balance_line(lines, code="1120"):
    """Add the receivable (or return-premium) line that balances ``lines`` (only credits positive / debits via sign)."""
    cr = sum((a for _, s, a in lines if s == "C"), ZERO)
    dr = sum((a for _, s, a in lines if s == "D"), ZERO)
    diff = cr - dr
    return lines + [(code, "D", diff)]


# ---------------------------------------------------------------------------
# Documents (debit note / credit note / VAT invoice)
# ---------------------------------------------------------------------------
def _doc_no(prefix):
    return f"{prefix}/{today().year}/{DocumentSequence.next(prefix):06d}"


def issue_documents(policy, endorsement=None):
    cur = policy.currency
    if endorsement is None:
        net, fees, vat, total, desc = policy.net_premium, policy.fees, policy.tax, policy.total_payable, f"Motor insurance premium {policy.policy_no}"
        if endorsement is None and policy.endorsements.exists():  # not expected at issuance
            pass
    else:
        net, fees, vat, total = endorsement.premium_adjustment, endorsement.fee, endorsement.tax, endorsement.total
        desc = f"{endorsement.get_endorsement_type_display()} {endorsement.number}"
    if total == 0 and net == 0 and fees == 0:
        return []
    kinds = [FinancialDocument.DEBIT_NOTE, FinancialDocument.INVOICE] if total > 0 else [FinancialDocument.CREDIT_NOTE]
    out = []
    for k in kinds:
        sign = 1 if k != FinancialDocument.CREDIT_NOTE else -1
        out.append(FinancialDocument.objects.create(
            doc_type=k, number=_doc_no(k), issued_on=today(), policy=policy, endorsement=endorsement, customer=policy.customer,
            currency=cur, fx_rate=policy.fx_rate, net_premium=net * sign, fees=fees * sign, vat=vat * sign, total=total * sign,
            description=desc[:160], is_demo=policy.is_demo))
    return out


# ---------------------------------------------------------------------------
# Premium side
# ---------------------------------------------------------------------------
def _posted(policy, code, side, types=None):
    qs = JournalLine.objects.filter(entry__policy=policy, account_id=code)
    if types:
        qs = qs.filter(entry__entry_type__in=types)
    a = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (a["d"] or ZERO) if side == "D" else (a["c"] or ZERO)


def _sync_reinsurance_and_commission(policy, user, source):
    """Post the *difference* between what the policy now cedes / pays and what has been booked."""
    ceded = sum((D(c.premium_ceded) for c in policy.cessions.all()), ZERO)
    booked_ri = _posted(policy, "2230", "C", ["ISSUE", "ENDORSE"]) - _posted(policy, "2230", "D", ["ISSUE", "ENDORSE"])
    comm = sum((D(c.amount) for c in policy.commissions.exclude(kind="PROFIT")), ZERO)
    booked_cm = _posted(policy, "2220", "C", ["ISSUE", "ENDORSE"]) - _posted(policy, "2220", "D", ["ISSUE", "ENDORSE"])
    lines = []
    d_ri, d_cm = q3(ceded - booked_ri), q3(comm - booked_cm)
    if d_ri:
        lines += [("1210", "D", d_ri), ("2230", "C", d_ri)]
    if d_cm:
        lines += [("5120" if settings.IFRS17_EXPENSE_IACF else "1130", "D", d_cm), ("2220", "C", d_cm)]
    return lines


@transaction.atomic
def on_policy_issued(policy, user=None):
    net, fees, tax = _base(policy.net_premium, policy), _base(policy.fees, policy), _base(policy.tax, policy)
    lines = [("2110", "C", net), ("4300", "C", fees), ("2210", "C", tax)]
    lines = _balance_line(lines)
    lines += _sync_reinsurance_and_commission(policy, user, policy.policy_no)
    entry = post(JournalEntry.ISSUE, f"Policy {policy.policy_no} issued to {policy.customer.name}", lines, policy=policy,
                 source=policy.policy_no, user=user)
    issue_documents(policy)
    return entry


@transaction.atomic
def on_endorsement(endo, user=None):
    policy = endo.policy
    adj, fee, tax = _base(endo.premium_adjustment, policy), _base(endo.fee, policy), _base(endo.tax, policy)
    lines = [("2110", "C", adj), ("4300", "C", fee), ("2210", "C", tax)]
    lines = [(c, s, a) for c, s, a in lines if a]
    lines = _balance_line(lines) if lines else []
    lines += _sync_reinsurance_and_commission(policy, user, endo.number)
    entry = post(JournalEntry.ENDORSE, f"Endorsement {endo.number}: {endo.get_endorsement_type_display()}", lines, policy=policy,
                 source=endo.number, user=user)
    issue_documents(policy, endo)
    return entry


@transaction.atomic
def on_receipt(payment, user=None):
    amt = payment.base_amount
    return post(JournalEntry.RECEIPT, f"Receipt {payment.receipt_no} from {payment.customer.name}",
                [("1110", "D", amt), ("1120", "C", amt)], policy=payment.policy, source=payment.receipt_no or "", user=user)


# ---------------------------------------------------------------------------
# Revenue recognition (PAA: straight-line over the coverage period)
# ---------------------------------------------------------------------------
def earned_fraction(policy, through):
    total_days = max((policy.end_date - policy.start_date).days + 1, 1)
    if policy.status == "CANCELLED" and policy.cancelled_on and through >= policy.cancelled_on:
        return D(1)
    elapsed = min(max((through - policy.start_date).days + 1, 0), total_days)
    return D(elapsed) / D(total_days)


@transaction.atomic
def recognise_policy(policy, through, user=None):
    net_base = _base(policy.net_premium, policy)
    frac = earned_fraction(policy, through)
    target = q3(net_base * frac)
    posted = _posted(policy, "4110", "C", ["EARN"]) - _posted(policy, "4110", "D", ["EARN"])
    delta = q3(target - posted)
    # reinsurance allocation follows the same fraction of what is ceded
    ceded = sum((D(c.premium_ceded) for c in policy.cessions.all()), ZERO)
    ri_target = q3(ceded * frac)
    ri_posted = _posted(policy, "5130", "D", ["EARN"]) - _posted(policy, "5130", "C", ["EARN"])
    d_ri = q3(ri_target - ri_posted)
    lines = [("2110", "D", delta), ("4110", "C", delta), ("5130", "D", d_ri), ("1210", "C", d_ri)]
    if not settings.IFRS17_EXPENSE_IACF:
        comm = sum((D(c.amount) for c in policy.commissions.exclude(kind="PROFIT")), ZERO)
        am_target = q3(comm * frac)
        am_posted = _posted(policy, "5120", "D", ["EARN"]) - _posted(policy, "5120", "C", ["EARN"])
        d_am = q3(am_target - am_posted)
        lines += [("5120", "D", d_am), ("1130", "C", d_am)]
    return post(JournalEntry.EARN, f"Revenue recognition to {through:%d %b %Y}: {policy.policy_no}", lines, policy=policy,
                source=f"to {through.isoformat()}", user=user, on=through)


def recognise_revenue(through, user=None, progress=None):
    """Post earned premium for every policy up to ``through``. Idempotent: only the missing amount is posted."""
    from motor.models import Policy

    n = 0
    qs = Policy.objects.filter(start_date__lte=through).select_related("currency")
    for policy in qs.iterator(chunk_size=200):
        if recognise_policy(policy, through, user):
            n += 1
        if progress:
            progress(n)
    from pnc import ledger as pnc_ledger
    n += pnc_ledger.recognise_all(through, user)
    return n


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------
def _claim_posted(claim, code, side, types):
    qs = JournalLine.objects.filter(entry__claim=claim, account_id=code, entry__entry_type__in=types)
    a = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (a["d"] or ZERO) if side == "D" else (a["c"] or ZERO)


@transaction.atomic
def sync_claim(claim, user=None, memo=""):
    """Bring incurred claims (LIC) and the reinsurance recoverable in line with the claim's current position."""
    from claims.models import Claim
    from claims.services import reinsurance_share

    policy = claim.policy
    if claim.status == Claim.REJECTED:
        incurred = ZERO
    elif claim.status in (Claim.APPROVED, Claim.SETTLED, Claim.CLOSED):
        incurred = claim.net_payable
    else:
        incurred = max(claim.reserve - claim.excess_applied, ZERO)
    target = _base(incurred, policy)
    posted = _claim_posted(claim, "5110", "D", ["CLAIM"]) - _claim_posted(claim, "5110", "C", ["CLAIM"])
    delta = q3(target - posted)
    from claims.services import reinsurance_target

    ri_target = q3(reinsurance_target(claim, target)) if claim.status != Claim.REJECTED else ZERO
    ri_posted = _claim_posted(claim, "1230", "D", ["CLAIM"]) - _claim_posted(claim, "1230", "C", ["CLAIM"])
    d_ri = q3(ri_target - ri_posted)
    lines = [("5110", "D", delta), ("2120", "C", delta), ("1230", "D", d_ri), ("4210", "C", d_ri)]
    entry = post(JournalEntry.CLAIM, f"Claim {claim.claim_no}: incurred position ({claim.get_status_display()})" + (f" - {memo}" if memo else ""), lines, claim=claim,
                 source=claim.claim_no, user=user)
    if claim.status == Claim.CLOSED:      # release any LIC left over once the file is financially closed
        lic = (_claim_posted(claim, "2120", "C", ["CLAIM", "CLAIM_PAY", "RECOVERY"])
               - _claim_posted(claim, "2120", "D", ["CLAIM", "CLAIM_PAY", "RECOVERY"]))
        if lic > 0:
            post(JournalEntry.CLAIM, f"Claim {claim.claim_no}: release of remaining LIC on closure",
                 [("2120", "D", lic), ("5110", "C", lic)], claim=claim, source=claim.claim_no, user=user)
    return entry


@transaction.atomic
def on_claim_payment(claim, payment, user=None):
    amt = _base(payment.amount, claim.policy)
    if payment.stage == "DELAY":
        lines = [("5110", "D", amt), ("1110", "C", amt)]
    else:
        lines = [("2120", "D", amt), ("1110", "C", amt)]
    return post(JournalEntry.CLAIM_PAY, f"Claim {claim.claim_no}: {payment.get_stage_display()} to {payment.payee}", lines, claim=claim,
                source=claim.claim_no, user=user)


@transaction.atomic
def on_recovery(claim, amount, label, user=None):
    """Cash recovered (salvage / subrogation): reduces the claims expense."""
    amt = _base(amount, claim.policy)
    return post(JournalEntry.RECOVERY, f"Claim {claim.claim_no}: {label}", [("1110", "D", amt), ("5110", "C", amt)], claim=claim,
                source=claim.claim_no, user=user)


@transaction.atomic
def on_overpayment_refund(claim, amount, user=None):
    """Cash returned by the payee after an overpayment: clears the debit left on the claims liability."""
    amt = _base(amount, claim.policy)
    return post(JournalEntry.RECOVERY, f"Claim {claim.claim_no}: overpayment refunded", [("1110", "D", amt), ("2120", "C", amt)],
                claim=claim, source=claim.claim_no, user=user)


@transaction.atomic
def on_excess_refund(claim, amount, user=None):
    amt = _base(amount, claim.policy)
    return post(JournalEntry.RECOVERY, f"Claim {claim.claim_no}: excess refunded to insured", [("5110", "D", amt), ("1110", "C", amt)],
                claim=claim, source=claim.claim_no, user=user)


@transaction.atomic
def on_reinsurance_cash(claim, amount, user=None):
    """Cash received from reinsurers settles the reinsurance asset for incurred claims."""
    amt = _base(amount, claim.policy)
    return post(JournalEntry.RECOVERY, f"Claim {claim.claim_no}: reinsurance recovery received", [("1110", "D", amt), ("1230", "C", amt)],
                claim=claim, source=claim.claim_no, user=user)


@transaction.atomic
def on_commission_paid(settlement, user=None):
    """Commission paid to an agent / broker settles the commission payable (2220)."""
    amt = q3(settlement.amount)
    return post(JournalEntry.COMM_PAY, f"Commission {settlement.number} paid to {settlement.producer.get_username()}",
                [("2220", "D", amt), ("1110", "C", amt)], source=settlement.number, user=user)


@transaction.atomic
def on_ri_premium_paid(treaty, amount, source, user=None, label="deposit premium instalment"):
    """Premium paid to the reinsurer settles the reinsurance premium payable (2230)."""
    amt = q3(amount)
    return post(JournalEntry.RI_PAY, f"Treaty {treaty.reference}: {label}", [("2230", "D", amt), ("1110", "C", amt)], source=source, user=user)


@transaction.atomic
def on_xol_minimum_topup(treaty, amount, source, user=None):
    """Premium accrued for an XoL treaty is lifted to the minimum premium at expiry."""
    amt = q3(amount)
    return post(JournalEntry.ENDORSE, f"Treaty {treaty.reference}: top-up to minimum premium", [("1210", "D", amt), ("2230", "C", amt)],
                source=source, user=user)

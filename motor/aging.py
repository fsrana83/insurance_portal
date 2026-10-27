"""Receivables and payables ageing (premiums and claims), detail and summary, as at a date. All amounts in base currency.

Receivables: premium due from insureds (by instalment when on credit), reinsurers' share of paid claims, subrogation, overpayments, XoL recoveries.
Payables:    reinsurance deposit premium, broker commission due, claims approved but not yet paid.
"""
from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import Sum

from core.models import to_base

D = Decimal
ZERO = D("0")
BANDS = [("Not yet due", None), ("1-30", 30), ("31-60", 60), ("61-90", 90), ("91-180", 180), ("181-365", 365), ("Over 365", 10 ** 6)]
RI_TERMS_DAYS = 30
CLAIM_TERMS_DAYS = 30


def bucket(due, as_at):
    age = (as_at - due).days
    if age <= 0:
        return 0, age
    for i, (_, hi) in enumerate(BANDS[1:], 1):
        if age <= hi:
            return i, age
    return len(BANDS) - 1, age


def _row(ref, party, doc_date, due, amount, as_at, note=""):
    i, age = bucket(due, as_at)
    cells = [ZERO] * len(BANDS)
    cells[i] = D(amount)
    return [ref, party, doc_date, due, D(max(age, 0)), *cells, D(amount), note]


def _cols(first="Document"):
    return [(first, "t"), ("Party", "t"), ("Document date", "d"), ("Due date", "d"), ("Days overdue", "i")] + [(b, "n") for b, _ in BANDS] + [("Total (OMR)", "n"), ("Note", "t")]


# ---- receivables -----------------------------------------------------------------------------------------------
def premium_receivable(as_at):
    from motor.credit import schedule_status
    from motor.models import Policy

    rows = []
    pols = Policy.objects.filter(issued_at__date__lte=as_at).exclude(status="CANCELLED").select_related("customer", "currency").prefetch_related("payments", "installments")
    for p in pols:
        paid = sum((D(x.amount) for x in p.payments.all() if x.status == "PAID" and x.paid_at and x.paid_at.date() <= as_at), ZERO)
        fx = D(p.fx_rate)
        if p.installments.all():
            left = paid
            for it in p.installments.all():
                took = min(left, it.amount)
                left -= took
                rem = it.amount - took
                if rem > D("0.0005"):
                    rows.append(_row(p.policy_no, p.customer.name, p.issued_at.date(), it.due_date, rem * fx, as_at, "Advance" if it.number == 0 else f"Instalment {it.number}"))
        else:
            rem = D(p.total_payable) - paid
            if rem > D("0.0005"):
                rows.append(_row(p.policy_no, p.customer.name, p.issued_at.date(), p.issued_at.date(), rem * fx, as_at, "Due on issue"))
    return rows


def claims_receivable(as_at):
    from claims.models import Claim, Subrogation, TreatyRecovery
    from reinsurance.models import AggRecovery

    rows = []
    for r in TreatyRecovery.objects.filter(posted_on__lte=as_at).select_related("claim"):
        if r.due > D("0.0005"):
            rows.append(_row(r.journal_no, r.treaty_label or r.treaty_ref, r.posted_on, r.posted_on + timedelta(days=RI_TERMS_DAYS), r.due, as_at,
                             f"Reinsurers' share, {r.claim.claim_no}"))
    for a in AggRecovery.objects.all():
        if a.due > D("0.0005") and a.computed_on <= as_at:
            rows.append(_row(f"{a.treaty.reference}", a.label, a.computed_on, a.computed_on + timedelta(days=RI_TERMS_DAYS), a.due, as_at, f"{a.get_kind_display() if hasattr(a, 'get_kind_display') else a.kind} XoL recovery"))
    for s in Subrogation.objects.exclude(recovery_status__in=["RECOVERED", "WRITTEN_OFF"]).select_related("claim__policy__currency"):
        open_amt = to_base(max(s.reserve_amount - s.amount_recovered, ZERO), s.claim.policy.currency)
        d0 = s.claim.file_finalised_on or s.claim.date_of_loss
        if open_amt > 0 and d0 <= as_at:
            rows.append(_row(s.claim.claim_no, s.liable_insurer or s.liable_party, d0, d0 + timedelta(days=60), open_amt, as_at, "Subrogation from the at-fault party"))
    for c in Claim.objects.filter(overpaid__gt=0).select_related("policy__currency", "policy__customer"):
        d0 = c.reopened_on or c.file_finalised_on or c.date_of_loss
        rows.append(_row(c.claim_no, c.policy.customer.name, d0, d0 + timedelta(days=CLAIM_TERMS_DAYS), to_base(c.overpaid, c.policy.currency), as_at, "Overpayment to recover"))
    return rows


# ---- payables ------------------------------------------------------------------------------------------------------
def premium_payable(as_at):
    from motor import commissions
    from reinsurance.models import MDPInstallment

    rows = []
    for i in MDPInstallment.objects.filter(due_date__lte=as_at + timedelta(days=365)).select_related("treaty"):
        paid_ok = i.paid and (i.paid_on is None or i.paid_on <= as_at)
        if not paid_ok and i.due_date <= as_at + timedelta(days=30):
            rows.append(_row(i.treaty.reference, i.treaty.reinsurer.name, i.treaty.effective_from, i.due_date, i.amount, as_at, "XoL deposit premium instalment"))
    for u in User.objects.filter(policies__isnull=False).distinct():
        for line, amt in commissions.due_lines(u, upto=as_at):
            d0 = line.policy.issued_at.date()
            rows.append(_row(line.policy.policy_no, u.get_username(), d0, d0 + timedelta(days=30), amt, as_at, "Commission payable on collected premium"))
    return rows


def claims_payable(as_at):
    from claims.models import Claim

    rows = []
    for c in Claim.objects.filter(status__in=["APPROVED", "SETTLED"]).select_related("policy__currency", "policy__customer"):
        left = c.net_payable - c.total_paid
        d0 = c.file_finalised_on or c.date_of_loss
        if left > D("0.0005") and d0 <= as_at:
            rows.append(_row(c.claim_no, c.policy.customer.name, d0, d0 + timedelta(days=CLAIM_TERMS_DAYS), to_base(left, c.policy.currency), as_at, "Approved, not yet paid (FSA: 30 days)"))
    return rows


def build(as_at):
    """OrderedDict of sections: name -> (side, rows)."""
    return OrderedDict([
        ("Premium receivable", ("Receivable", premium_receivable(as_at))),
        ("Claims receivable (reinsurance, subrogation, overpayments)", ("Receivable", claims_receivable(as_at))),
        ("Premium payable (reinsurance premium, commission)", ("Payable", premium_payable(as_at))),
        ("Claims payable", ("Payable", claims_payable(as_at))),
    ])

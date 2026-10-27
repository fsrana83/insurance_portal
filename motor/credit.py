"""Payment terms at issue: a receipt (cash basis) or Credit Control approval (credit basis, up to 12 instalments, optional advance)."""
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core import clock
from core.models import user_role

from .models import CreditApproval, PolicyInstallment, Proposal

D = Decimal
MAX_INSTALLMENTS = 12
APPROVERS = ("CREDIT_CONTROL", "FINANCE", "ADMIN")


class CreditError(Exception):
    pass


def request_credit(proposal, user, installments, advance=0, note=""):
    if proposal.status != Proposal.APPROVED:
        raise CreditError("Credit can be requested once the proposal is approved.")
    n = int(installments)
    if not 1 <= n <= MAX_INSTALLMENTS:
        raise CreditError(f"Instalments must be between 1 and {MAX_INSTALLMENTS}.")
    advance = proposal.currency.quantize(D(advance or 0))
    if advance < 0 or advance >= proposal.total_payable:
        raise CreditError("The advance must be below the total premium.")
    from . import phase1
    ca = CreditApproval.objects.filter(proposal=proposal).first()
    if ca and ca.status == CreditApproval.PENDING:
        raise CreditError("A credit request is already waiting for Credit Control.")
    if ca and ca.status == CreditApproval.APPROVED:
        raise CreditError("Credit is already approved for this proposal.")
    fields = dict(installments=n, advance_amount=advance, status=CreditApproval.PENDING, requested_by=user, request_note=note[:200],
                  decided_by=None, decided_on=None, decision_note="")
    if ca is None:
        ca = CreditApproval.objects.create(proposal=proposal, uid=phase1.next_credit_uid(), **fields)
    else:
        if ca.status == CreditApproval.REJECTED:             # a new request after a decline gets its own UID; the earlier one is kept in the history
            phase1.archive_request(ca)
            ca.uid = phase1.next_credit_uid()
        for k, v in fields.items():
            setattr(ca, k, v)
        ca.requested_on = timezone.now()
        ca.save()
    return ca


def decide(ca, user, approve, note=""):
    if user_role(user) not in APPROVERS:
        raise CreditError("Only Credit Control or Finance can decide credit.")
    if ca.requested_by_id == user.id and not user.is_superuser:
        raise CreditError("A second person must decide credit; you requested it.")
    if ca.status != CreditApproval.PENDING:
        raise CreditError("This request was already decided.")
    if not approve and not note.strip():
        raise CreditError("Give a reason for rejecting credit.")
    ca.status = CreditApproval.APPROVED if approve else CreditApproval.REJECTED
    ca.decided_by, ca.decided_on, ca.decision_note = user, timezone.now(), note[:200]
    ca.save()
    return ca


def build_schedule(policy, n, advance=0, start=None):
    """Advance (number 0, due at issue) then ``n`` monthly instalments; the last one absorbs rounding."""
    start = start or clock.today()
    cur = policy.currency
    advance = cur.quantize(D(advance or 0))
    policy.installments.all().delete()
    rows = []
    if advance > 0:
        rows.append(PolicyInstallment(policy=policy, number=0, due_date=start, amount=advance))
    rest = policy.total_payable - advance
    each = cur.quantize(rest / n)
    for i in range(1, n + 1):
        amt = each if i < n else rest - each * (n - 1)
        rows.append(PolicyInstallment(policy=policy, number=i, due_date=start + timedelta(days=30 * i), amount=amt))
    PolicyInstallment.objects.bulk_create(rows)
    return rows


def schedule_status(policy, today=None):
    """[(installment, paid, due_left, days_overdue)] with receipts applied oldest first."""
    today = today or clock.today()
    left = policy.paid_amount
    out = []
    for it in policy.installments.all():
        paid = min(left, it.amount)
        left -= paid
        rem = it.amount - paid
        out.append((it, paid, rem, max((today - it.due_date).days, 0) if rem > 0 else 0))
    return out


def check_terms(proposal, basis, method="", reference="", receipt_amount=None):
    """Raise CreditError unless the payment terms allow issuing. Returns the amount to receive now."""
    from django.conf import settings
    if not getattr(settings, "REQUIRE_PAYMENT_OR_CREDIT", True):
        return D(0)
    if basis == "CASH":
        if not method:
            raise CreditError("Choose how the premium was received: a receipt is required to issue on a cash basis.")
        if method == "ONLINE":
            raise CreditError("Online payments need a policy first: use cash, cheque, transfer or card here, or issue on credit.")
        if method in ("CHEQUE", "TRANSFER") and not reference:
            raise CreditError("Enter the cheque / transfer reference of the receipt.")
        return proposal.total_payable
    if basis == "CREDIT":
        ca = getattr(proposal, "credit", None)
        if not ca or ca.status != CreditApproval.APPROVED:
            raise CreditError("Credit Control approval is required to issue on credit.")
        if ca.advance_amount > 0:
            if not method:
                raise CreditError(f"Collect the advance of {ca.advance_amount} and choose how it was received.")
            if method == "ONLINE":
                raise CreditError("Record the advance as cash, cheque, transfer or card.")
            if method in ("CHEQUE", "TRANSFER") and not reference:
                raise CreditError("Enter the cheque / transfer reference of the advance receipt.")
        return ca.advance_amount
    raise CreditError("Choose cash (receipt) or credit (Credit Control approval).")


@transaction.atomic
def issue_with_terms(proposal, user, basis, method="", reference=""):
    """Issue the policy under the chosen payment terms: record the receipt (cash / advance) and build the instalment schedule."""
    from payments import services as ps
    from . import services

    due_now = check_terms(proposal, basis, method, reference)
    policy = services.issue_policy(proposal, user)
    if due_now > 0 and method and method != "ONLINE":
        ps.record_receipt(policy, due_now, method, user, reference=reference, narration="Premium receipt at issue" if basis == "CASH" else "Advance at issue")
    if basis == "CREDIT":
        ca = proposal.credit
        ca.policy = policy
        ca.save(update_fields=["policy"])
        build_schedule(policy, ca.installments, ca.advance_amount)
    return policy

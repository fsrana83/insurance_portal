"""Release 10, policy side: renewal window, vehicle conflicts, broker / agent assignment history, credit request UIDs and refer-back,
post-dated cheques and finance interests."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core import clock
from core.models import DocumentSequence, user_role

from .models import (CreditApproval, CreditRequestLog, Customer, FinanceInterest, Policy, PolicyRisk, PostDatedCheque, ProducerAssignment, Proposal, Vehicle)

D = Decimal
RENEWAL_WINDOW_DAYS = getattr(settings, "RENEWAL_WINDOW_DAYS", 30)


class Phase1Error(Exception):
    pass


# ---- renewal ----------------------------------------------------------------------------------------------------------
def renewal_error(policy):
    """Early renewal is accepted from 30 days before expiry; the renewal starts the day after the existing policy ends."""
    if policy.status == Policy.CANCELLED:
        return "A cancelled policy cannot be renewed."
    if policy.status == Policy.ACTIVE and (policy.end_date - clock.today()).days > RENEWAL_WINDOW_DAYS:
        opens = policy.end_date - timedelta(days=RENEWAL_WINDOW_DAYS)
        return f"Renewal opens {RENEWAL_WINDOW_DAYS} days before expiry: from {opens:%d %b %Y}."
    return ""


def vehicle_conflicts(vehicle, start, end, exclude_policy=None):
    """Active policies already covering this vehicle during [start, end]. A conflict is flagged for review, not blocked."""
    qs = PolicyRisk.objects.filter(vehicle=vehicle, active=True, policy__status=Policy.ACTIVE, policy__start_date__lte=end, policy__end_date__gte=start)
    if exclude_policy is not None:
        qs = qs.exclude(policy=exclude_policy)
    return [r.policy for r in qs.select_related("policy")]


# ---- broker / agent assignment ------------------------------------------------------------------------------------------
def current_producer(customer):
    a = customer.producer_history.filter(to_date__isnull=True).select_related("producer").first()
    return a.producer if a else None


@transaction.atomic
def assign_producer(customer, producer, user, note="", on=None):
    """Keep the customer with the same broker / agent until a new one is assigned. The earlier assignment is closed, never deleted."""
    on = on or clock.today()
    cur = customer.producer_history.filter(to_date__isnull=True).first()
    if cur and cur.producer_id == producer.pk:
        return cur
    if cur:
        cur.to_date = on
        cur.save(update_fields=["to_date"])
    return ProducerAssignment.objects.create(customer=customer, producer=producer, from_date=on, note=note[:200], assigned_by=user)


# ---- credit requests ------------------------------------------------------------------------------------------------------
def next_credit_uid():
    return f"CR-{clock.today().year}-{DocumentSequence.next('CREDITREQ'):05d}"


def archive_request(ca):
    CreditRequestLog.objects.create(proposal=ca.proposal, uid=ca.uid or "", installments=ca.installments, advance_amount=ca.advance_amount, status=ca.status,
                                    requested_by=ca.requested_by, requested_on=ca.requested_on, decided_by=ca.decided_by, decision_note=ca.decision_note)


def refer_back(ca, user, note):
    from .credit import APPROVERS, CreditError
    if user_role(user) not in APPROVERS:
        raise CreditError("Only Credit Control or Finance can refer a request back.")
    if ca.status != CreditApproval.PENDING:
        raise CreditError("Only a request awaiting a decision can be referred back.")
    if not note.strip():
        raise CreditError("Say what the requester must correct.")
    ca.status, ca.decided_by, ca.decided_on, ca.decision_note = CreditApproval.REFERRED, user, timezone.now(), note[:200]
    ca.save()
    return ca


# ---- post-dated cheques ---------------------------------------------------------------------------------------------------
def add_pdc(proposal, user, cheque_no, bank, amount, cheque_date, installment_no=None, received_on=None, note=""):
    amount = proposal.currency.quantize(D(amount))
    if amount <= 0:
        raise Phase1Error("Enter the cheque amount.")
    if not cheque_no or not bank:
        raise Phase1Error("Cheque number and bank are required.")
    if PostDatedCheque.objects.filter(cheque_no=cheque_no, bank__iexact=bank).exclude(status=PostDatedCheque.RETURNED).exists():
        raise Phase1Error("This cheque is already recorded.")
    policy = getattr(proposal, "policy", None) if proposal.status == Proposal.ISSUED else None
    return PostDatedCheque.objects.create(proposal=proposal, policy=policy, installment_no=installment_no or None, cheque_no=cheque_no, bank=bank, amount=amount,
                                          cheque_date=cheque_date, received_on=received_on or clock.today(), note=note[:150], created_by=user, is_demo=proposal.is_demo)


@transaction.atomic
def set_pdc_status(pdc, status, user):
    from payments import services as ps
    ok = {PostDatedCheque.RECEIVED: [PostDatedCheque.DEPOSITED, PostDatedCheque.RETURNED], PostDatedCheque.DEPOSITED: [PostDatedCheque.CLEARED, PostDatedCheque.BOUNCED],
          PostDatedCheque.BOUNCED: [PostDatedCheque.DEPOSITED, PostDatedCheque.RETURNED]}
    if status not in ok.get(pdc.status, []):
        raise Phase1Error(f"A cheque that is {pdc.get_status_display().lower()} cannot be marked {dict(PostDatedCheque.STATUSES)[status].lower()}.")
    if status == PostDatedCheque.DEPOSITED and pdc.cheque_date > clock.today():
        raise Phase1Error("The cheque is post-dated: it can be deposited on or after its date.")
    if status == PostDatedCheque.CLEARED:
        policy = pdc.policy or getattr(pdc.proposal, "policy", None)
        if policy is None:
            raise Phase1Error("The policy has not been issued yet.")
        pay = ps.record_receipt(policy, min(pdc.amount, policy.balance_due), "CHEQUE", user, reference=pdc.cheque_no, narration=f"Post-dated cheque {pdc.cheque_no} cleared")
        pdc.payment, pdc.policy = pay, policy
    pdc.status = status
    pdc.save()
    return pdc


# ---- finance interests -------------------------------------------------------------------------------------------------------
def add_finance(vehicle, user, financier, contract_no="", amount=0, policy=None, from_date=None, note=""):
    if not financier.strip():
        raise Phase1Error("Enter the financier's name.")
    if vehicle.finance_interests.filter(to_date__isnull=True, financier__iexact=financier.strip(), contract_no=contract_no).exists():
        raise Phase1Error("This finance interest is already recorded.")
    return FinanceInterest.objects.create(vehicle=vehicle, policy=policy, financier=financier.strip()[:100], contract_no=contract_no[:40], financed_amount=D(amount or 0),
                                          from_date=from_date or clock.today(), note=note[:200], created_by=user)


def release_finance(fi, on=None):
    fi.to_date = on or clock.today()
    fi.save(update_fields=["to_date"])


def finance_at(vehicle, on):
    """Finance interests applicable on a date (the accident date, for a claim)."""
    return list(vehicle.finance_interests.filter(from_date__lte=on).filter(Q(to_date__isnull=True) | Q(to_date__gt=on)))

"""Cover-level claim heads: estimate, approve (with limits), and pay in parts through a credit note that Accounts releases."""
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from core import clock
from core.models import user_role

from .models import Claim, ClaimHead, ClaimPayment, PaymentRelease
from .services import ClaimError, q

ZERO = Decimal("0")
CLAIMS = {"ADMIN", "CLAIMS_HANDLER", "CLAIMS_MANAGER"}


def head_total(claim):
    return sum((h.net for h in claim.heads.all()), ZERO)


def od_net(claim):
    """The own-damage part that follows the 70/30 repair staging (everything except approved heads)."""
    return max(claim.net_payable - head_total(claim), ZERO)


def _sync(claim, delta):
    claim.net_payable = max(claim.net_payable + delta, ZERO)
    claim.save(update_fields=["net_payable"])


@transaction.atomic
def add_head(claim, head, estimate, description="", excess=ZERO, recovery=ZERO, payee=""):
    if claim.locked:
        raise ClaimError("This claim is read only. Reopen it to add a head.")
    estimate = Decimal(estimate or 0)
    if estimate <= 0:
        raise ClaimError("Enter an estimate above zero.")
    if head == ClaimHead.OD:
        raise ClaimError("Own damage is handled by the main claim, not as a head.")
    return ClaimHead.objects.create(claim=claim, head=head, description=description[:160], estimate=estimate, excess=Decimal(excess or 0),
                                    recovery=Decimal(recovery or 0), payee=payee or claim.policy.customer.name)


@transaction.atomic
def approve_head(head, amount, user):
    claim = head.claim
    if user_role(user) not in CLAIMS:
        raise ClaimError("Only claims staff can approve a head.")
    if head.status == ClaimHead.APPROVED:
        raise ClaimError("This head is already approved.")
    amount = Decimal(amount or 0)
    if amount <= 0:
        raise ClaimError("Enter the approved amount.")
    limit = settings.CLAIM_HEAD_LIMITS.get(head.head, ZERO)
    applied = ZERO
    if limit and amount > limit:
        amount, applied = limit, limit
    head.approved_amount, head.limit_applied, head.status = q(claim.policy.currency, amount), applied, ClaimHead.APPROVED
    head.save()
    _sync(claim, head.net)
    return head


@transaction.atomic
def reject_head(head, user):
    if head.status == ClaimHead.APPROVED and head.requested:
        raise ClaimError("Payments were already requested on this head.")
    if head.status == ClaimHead.APPROVED:
        _sync(head.claim, -head.net)
    head.status = ClaimHead.REJECTED
    head.save(update_fields=["status"])
    return head


@transaction.atomic
def issue_release(head, amount, user):
    """Claims raises a credit note for a partial or full payment of the head."""
    if head.status != ClaimHead.APPROVED:
        raise ClaimError("Approve the head before releasing a payment.")
    amount = Decimal(amount or 0)
    if amount <= 0:
        raise ClaimError("Enter the amount to release.")
    if amount > head.balance:
        raise ClaimError(f"Only {head.balance} is left to release on this head.")
    n = PaymentRelease.objects.count() + 1
    while PaymentRelease.objects.filter(number=f"CN-{clock.today():%y}-{n:05d}").exists():
        n += 1
    return PaymentRelease.objects.create(head=head, number=f"CN-{clock.today():%y}-{n:05d}", amount=q(head.claim.policy.currency, amount),
                                         payee=head.payee, issued_on=clock.today(), issued_by=user)


@transaction.atomic
def disburse(release, user, reference=""):
    """Accounts pays the credit note: this records the claim payment, posts the ledger and the reinsurance share."""
    if user_role(user) not in {"ADMIN", "FINANCE"}:
        raise ClaimError("Only Accounts can disburse a credit note.")
    if release.status != PaymentRelease.ISSUED:
        raise ClaimError("This credit note is not awaiting payment.")
    claim = release.head.claim
    pay = ClaimPayment.objects.create(claim=claim, stage="HEAD", amount=release.amount, paid_on=clock.today(), payee=release.payee,
                                      reference=reference or release.number)
    claim.total_paid = claim.payments.aggregate(s=Sum("amount"))["s"] or ZERO
    if claim.status == Claim.APPROVED and claim.total_paid >= claim.net_payable:
        claim.status = Claim.SETTLED
    claim.save()
    release.status, release.paid_on, release.paid_by, release.payment = PaymentRelease.PAID, clock.today(), user, pay
    release.save()
    from accounting import services as acc
    from .services import post_treaty_recovery
    acc.on_claim_payment(claim, pay)
    post_treaty_recovery(claim, pay)
    return pay


def cash_limit(claim):
    """Cash in lieu of repair may not exceed the configured % of the lowest garage quotation."""
    quotes = [e.total for e in claim.estimates.all() if getattr(e, "total", 0)]
    if not quotes:
        return None
    return q(claim.policy.currency, min(quotes) * settings.CASH_SETTLEMENT_PCT / 100)

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounting import services as acc
from core.models import DocumentSequence

from .gateways import get_gateway
from .models import Payment

D = Decimal


class PaymentError(Exception):
    pass


def _no(prefix):
    return f"{prefix}/{acc.today().year}/{DocumentSequence.next(prefix):06d}"


def _check_amount(policy, amount):
    amount = policy.currency.quantize(D(amount))
    if amount <= 0:
        raise PaymentError("Enter an amount greater than zero.")
    outstanding = policy.total_payable - policy.paid_amount
    if amount > outstanding + D("0.0005"):
        raise PaymentError(f"The amount exceeds the balance due ({outstanding}).")
    return amount


def _new(policy, amount, method, user, **kw):
    return Payment.objects.create(
        number=_no("PAY"), policy=policy, customer=policy.customer, currency=policy.currency, fx_rate=policy.fx_rate, amount=amount,
        method=method, created_by=user, is_demo=policy.is_demo, **kw)


@transaction.atomic
def record_receipt(policy, amount, method, user, reference="", payer_name="", narration="", paid_at=None):
    """Money received over the counter / by cheque / transfer / POS: creates a paid receipt voucher straight away."""
    amount = _check_amount(policy, amount)
    if method == Payment.ONLINE:
        raise PaymentError("Use 'Request online payment' for gateway payments.")
    p = _new(policy, amount, method, user, reference=reference, payer_name=payer_name, narration=narration)
    return mark_paid(p, paid_at=paid_at, user=user)


@transaction.atomic
def request_online_payment(policy, amount, user, narration=""):
    amount = _check_amount(policy, amount)
    from django.conf import settings
    return _new(policy, amount, Payment.ONLINE, user, gateway=settings.PAYMENT_GATEWAY, narration=narration,
                expires_at=Payment.default_expiry())


@transaction.atomic
def mark_paid(payment, *, paid_at=None, user=None, gateway_txn_id="", card_last4="", reference=""):
    if payment.status == Payment.PAID:
        return payment
    payment.status = Payment.PAID
    payment.paid_at = paid_at or timezone.now()
    payment.receipt_no = _no("RV")
    if gateway_txn_id:
        payment.gateway_txn_id = gateway_txn_id
    if card_last4:
        payment.card_last4 = card_last4
    if reference:
        payment.reference = reference
    payment.failure_reason = ""
    payment.save()
    acc.on_receipt(payment, user)
    return payment


def pay_online(payment, number, expiry, cvv, holder):
    """Run the gateway for a pending online payment. Returns the GatewayResult."""
    if payment.status != Payment.PENDING:
        raise PaymentError("This payment request is no longer open.")
    if payment.is_expired:
        payment.status = Payment.EXPIRED
        payment.save(update_fields=["status"])
        raise PaymentError("This payment link has expired. Ask for a new one.")
    result = get_gateway(payment.gateway or None).charge(payment, number, expiry, cvv, holder)
    if result.success:
        mark_paid(payment, gateway_txn_id=result.txn_id, card_last4=result.last4, reference=result.txn_id)
        payment.payer_name = holder[:100]
        payment.save(update_fields=["payer_name"])
    else:
        payment.failure_reason = result.message[:120]
        payment.card_last4 = result.last4
        payment.save(update_fields=["failure_reason", "card_last4"])
    return result


def cancel(payment):
    if payment.status != Payment.PENDING:
        raise PaymentError("Only open requests can be cancelled.")
    payment.status = Payment.CANCELLED
    payment.save(update_fields=["status"])

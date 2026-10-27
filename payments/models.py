import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from core.models import Currency


def new_token():
    return secrets.token_urlsafe(24)


class Payment(models.Model):
    """A receipt (money received) or an online-payment request. Only PAID rows count towards the policy balance."""

    CASH, CHEQUE, TRANSFER, POS, ONLINE = "CASH", "CHEQUE", "TRANSFER", "POS", "ONLINE"
    METHODS = [(CASH, "Cash"), (CHEQUE, "Cheque"), (TRANSFER, "Bank transfer"), (POS, "Card (POS machine)"), (ONLINE, "Online payment")]
    PENDING, PAID, FAILED, CANCELLED, EXPIRED = "PENDING", "PAID", "FAILED", "CANCELLED", "EXPIRED"
    STATUSES = [(PENDING, "Awaiting payment"), (PAID, "Paid"), (FAILED, "Failed"), (CANCELLED, "Cancelled"), (EXPIRED, "Expired")]

    number = models.CharField(max_length=30, unique=True)
    receipt_no = models.CharField(max_length=30, unique=True, null=True, blank=True)
    token = models.CharField(max_length=40, unique=True, default=new_token, editable=False)
    policy = models.ForeignKey("motor.Policy", on_delete=models.PROTECT, related_name="payments")
    customer = models.ForeignKey("motor.Customer", on_delete=models.PROTECT, related_name="payments")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT)
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal("1"))
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    method = models.CharField(max_length=10, choices=METHODS)
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING, db_index=True)
    reference = models.CharField("Cheque / transfer / gateway reference", max_length=60, blank=True)
    payer_name = models.CharField(max_length=100, blank=True)
    narration = models.CharField(max_length=160, blank=True)
    gateway = models.CharField(max_length=20, blank=True)
    gateway_txn_id = models.CharField(max_length=60, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)
    failure_reason = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.receipt_no or self.number

    @property
    def is_expired(self):
        return self.status == self.PENDING and self.expires_at and self.expires_at < timezone.now()

    @property
    def base_amount(self):
        return Currency.base().quantize(self.amount * self.fx_rate)

    @staticmethod
    def default_expiry():
        return timezone.now() + timedelta(days=settings.PAYMENT_LINK_VALIDITY_DAYS)

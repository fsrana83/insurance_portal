from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models

from core.models import Currency


class Account(models.Model):
    ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = "ASSET", "LIABILITY", "EQUITY", "INCOME", "EXPENSE"
    CATEGORIES = [(ASSET, "Asset"), (LIABILITY, "Liability"), (EQUITY, "Equity"), (INCOME, "Income"), (EXPENSE, "Expense")]
    code = models.CharField(max_length=8, primary_key=True)
    name = models.CharField(max_length=80)
    category = models.CharField(max_length=9, choices=CATEGORIES)
    ifrs17_line = models.CharField(max_length=80, blank=True, help_text="Where it is presented under IFRS 17")

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} {self.name}"

    @property
    def normal_side(self):
        return "D" if self.category in (self.ASSET, self.EXPENSE) else "C"


class JournalEntry(models.Model):
    ISSUE, ENDORSE, RECEIPT, EARN, CLAIM, CLAIM_PAY, RECOVERY = "ISSUE", "ENDORSE", "RECEIPT", "EARN", "CLAIM", "CLAIM_PAY", "RECOVERY"
    COMM_PAY, RI_PAY, XOL_REC = "COMM_PAY", "RI_PAY", "XOL_REC"
    TYPES = [
        (ISSUE, "Policy issuance"), (ENDORSE, "Endorsement"), (RECEIPT, "Premium receipt"), (EARN, "Revenue recognition"),
        (CLAIM, "Claim incurred"), (CLAIM_PAY, "Claim payment"), (RECOVERY, "Recovery"), (COMM_PAY, "Commission payment"), (RI_PAY, "Reinsurance premium payment"), (XOL_REC, "XoL aggregate recovery"),
    ]
    number = models.CharField(max_length=24, unique=True)
    date = models.DateField(db_index=True)
    entry_type = models.CharField(max_length=10, choices=TYPES, db_index=True)
    narration = models.CharField(max_length=200)
    policy = models.ForeignKey("motor.Policy", null=True, blank=True, on_delete=models.CASCADE, related_name="journal_entries")
    claim = models.ForeignKey("claims.Claim", null=True, blank=True, on_delete=models.CASCADE, related_name="journal_entries")
    source = models.CharField(max_length=40, blank=True, help_text="Document that caused the entry (endorsement / receipt / claim no.)")
    currency = models.ForeignKey(Currency, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal("1"))
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    book = models.CharField(max_length=5, default="MOTOR", db_index=True, help_text="MOTOR or PNC: which business book the entry belongs to")
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["date", "id"]
        verbose_name_plural = "journal entries"

    def __str__(self):
        return self.number

    @property
    def total(self):
        return sum((l.debit for l in self.lines.all()), Decimal("0"))


class JournalLine(models.Model):
    """Amounts are always in the base currency (OMR); the entry keeps the transaction currency / rate for reference."""

    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    credit = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    memo = models.CharField(max_length=80, blank=True)

    class Meta:
        indexes = [models.Index(fields=["account", "entry"])]


class FinancialDocument(models.Model):
    """Debit note, credit note or VAT tax invoice raised by the system. Immutable once created."""

    DEBIT_NOTE, CREDIT_NOTE, INVOICE = "DN", "CN", "INV"
    TYPES = [(DEBIT_NOTE, "Debit note"), (CREDIT_NOTE, "Credit note"), (INVOICE, "VAT tax invoice")]
    doc_type = models.CharField(max_length=3, choices=TYPES, db_index=True)
    number = models.CharField(max_length=24, unique=True)
    issued_on = models.DateField()
    policy = models.ForeignKey("motor.Policy", on_delete=models.CASCADE, related_name="fin_documents")
    endorsement = models.ForeignKey("motor.Endorsement", null=True, blank=True, on_delete=models.SET_NULL, related_name="fin_documents")
    customer = models.ForeignKey("motor.Customer", on_delete=models.PROTECT, related_name="+")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="+")
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8)
    net_premium = models.DecimalField(max_digits=14, decimal_places=3)
    fees = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    vat = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    total = models.DecimalField(max_digits=14, decimal_places=3)
    description = models.CharField(max_length=160, blank=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-issued_on", "-id"]

    def __str__(self):
        return self.number

    @property
    def vat_base(self):
        """VAT amount in OMR (the Oman VAT regulations require it on foreign-currency invoices)."""
        return Currency.base().quantize(self.vat * self.fx_rate)

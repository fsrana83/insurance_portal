"""Property & Casualty: fire and property, marine, engineering, liability and miscellaneous accident classes.

Shares customers, currencies, treaties, the ledger, catastrophe events, credit control rules and dual confirmation with the motor system."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models
from django.db.models import Sum

from core.models import Currency

D = Decimal
ZERO = D("0")

GROUPS = [("FIRE", "Fire & Property"), ("MARINE", "Marine"), ("ENGINEERING", "Engineering"), ("LIABILITY", "Liability"), ("MEDICAL", "Medical"), ("MISC", "Miscellaneous accident")]


class PncClass(models.Model):
    """A class of business with its tariff and the questions asked about each risk."""
    code = models.CharField(max_length=14, unique=True)
    name = models.CharField(max_length=80)
    group = models.CharField(max_length=12, choices=GROUPS)
    si_label = models.CharField("Sum insured is called", max_length=60, default="Sum insured")
    rate_pct = models.DecimalField("Tariff rate %", max_digits=7, decimal_places=4, default=D("0.2"), help_text="Annual rate per 100 of the sum insured / limit")
    min_premium = models.DecimalField(max_digits=12, decimal_places=3, default=D("25"), help_text="Minimum annual premium per risk (base currency)")
    deductible_pct = models.DecimalField("Deductible % of loss", max_digits=5, decimal_places=2, default=D("0"))
    deductible_min = models.DecimalField("Minimum deductible", max_digits=12, decimal_places=3, default=D("0"))
    commission_pct = models.DecimalField(max_digits=5, decimal_places=2, default=D("10"), help_text="Paid to agents and brokers")
    referral = models.BooleanField(default=False, help_text="Always goes to an underwriter for approval")
    average_applies = models.BooleanField("Average (underinsurance) clause", default=False)
    fields = models.JSONField(default=list, blank=True, help_text="Questions about the risk: [{key, label, type, choices}]")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["group", "name"]
        verbose_name = "P&C class"

    def __str__(self):
        return self.name


class PncPolicy(models.Model):
    QUOTE, PENDING, APPROVED, ACTIVE, CANCELLED, EXPIRED, REJECTED = "QUOTE", "PENDING", "APPROVED", "ACTIVE", "CANCELLED", "EXPIRED", "REJECTED"
    STATUSES = [(QUOTE, "Quote"), (PENDING, "Awaiting approval"), (APPROVED, "Approved"), (ACTIVE, "Active"), (CANCELLED, "Cancelled"), (EXPIRED, "Expired"), (REJECTED, "Rejected")]
    quote_no = models.CharField(max_length=24, unique=True)
    fac_reference = models.CharField("Facultative confirmation", max_length=80, blank=True, help_text="Reference of the signed facultative placement; required to issue when the sum insured exceeds treaty capacity")
    policy_no = models.CharField(max_length=24, unique=True, null=True, blank=True)
    pnc_class = models.ForeignKey(PncClass, on_delete=models.PROTECT, related_name="policies")
    customer = models.ForeignKey("motor.Customer", on_delete=models.PROTECT, related_name="pnc_policies")
    producer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="pnc_policies")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT)
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8, default=D("1"))
    status = models.CharField(max_length=10, choices=STATUSES, default=QUOTE, db_index=True)
    start_date = models.DateField()
    end_date = models.DateField()
    interest = models.CharField("Interest insured / business description", max_length=200, blank=True)
    conditions = models.TextField("Special conditions and warranties", blank=True)
    net_premium = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    fees = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    tax = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    total_payable = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    approval_reasons = models.JSONField(default=list, blank=True)
    decision_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decision_note = models.CharField(max_length=300, blank=True)
    issued_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    issued_at = models.DateTimeField(null=True, blank=True)
    cancelled_on = models.DateField(null=True, blank=True)
    version = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "P&C policy"
        verbose_name_plural = "P&C policies"

    @property
    def number(self):
        return self.policy_no or self.quote_no

    def __str__(self):
        return self.number

    @property
    def is_editable(self):
        return self.status in (self.QUOTE, self.REJECTED)

    @property
    def is_issued(self):
        return self.status in (self.ACTIVE, self.CANCELLED, self.EXPIRED)

    @property
    def paid_amount(self):
        return self.receipts.aggregate(s=Sum("amount"))["s"] or ZERO

    @property
    def balance_due(self):
        return self.total_payable - self.paid_amount

    @property
    def payment_status(self):
        paid = self.paid_amount
        return "UNPAID" if paid <= 0 else "PAID" if paid >= self.total_payable else "PARTIAL"

    @property
    def sum_insured(self):
        return self.risks.filter(active=True).aggregate(s=Sum("sum_insured"))["s"] or ZERO

    @property
    def period_days(self):
        return (self.end_date - self.start_date).days + 1


class PncRisk(models.Model):
    policy = models.ForeignKey(PncPolicy, on_delete=models.CASCADE, related_name="risks")
    number = models.PositiveSmallIntegerField(default=1)
    description = models.CharField(max_length=200)
    location = models.CharField(max_length=200, blank=True)
    sum_insured = models.DecimalField("Sum insured / limit", max_digits=16, decimal_places=3)
    value_at_risk = models.DecimalField(max_digits=16, decimal_places=3, default=ZERO, help_text="Full value, for the average clause. Leave 0 if not known")
    rate_pct = models.DecimalField(max_digits=7, decimal_places=4, default=ZERO)
    loading_pct = models.DecimalField("Loading / (discount) %", max_digits=6, decimal_places=2, default=ZERO, help_text="Negative = discount")
    loading_reason = models.CharField(max_length=120, blank=True)
    deductible = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO, help_text="Fixed deductible per loss; 0 = class default")
    annual_premium = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    premium = models.DecimalField("Premium for the period", max_digits=14, decimal_places=3, default=ZERO)
    details = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["policy_id", "number"]

    def __str__(self):
        return f"{self.policy.number} #{self.number} {self.description}"


class PncEndorsement(models.Model):
    TYPES = [("SI_CHANGE", "Change of sum insured"), ("ADD_RISK", "Add a risk"), ("REMOVE_RISK", "Remove a risk"), ("EXTEND", "Change of expiry date"),
             ("DETAILS", "Change of details / conditions"), ("CANCEL", "Cancellation")]
    policy = models.ForeignKey(PncPolicy, on_delete=models.CASCADE, related_name="endorsements")
    number = models.CharField(max_length=24, unique=True)
    endorsement_type = models.CharField(max_length=12, choices=TYPES)
    effective_date = models.DateField()
    description = models.CharField(max_length=250, blank=True)
    premium_adjustment = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    fee = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    tax = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    total = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def get_endorsement_type_display_safe(self):
        return self.get_endorsement_type_display()


class PncReceipt(models.Model):
    METHODS = [("CASH", "Cash"), ("CHEQUE", "Cheque"), ("TRANSFER", "Bank transfer"), ("POS", "Card (POS)")]
    policy = models.ForeignKey(PncPolicy, on_delete=models.CASCADE, related_name="receipts")
    number = models.CharField(max_length=30, unique=True)
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    method = models.CharField(max_length=10, choices=METHODS)
    reference = models.CharField(max_length=60, blank=True)
    narration = models.CharField(max_length=160, blank=True)
    received_on = models.DateField()
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["received_on", "id"]


class PncCredit(models.Model):
    """Credit basis: Credit Control approves instalments (1 to 12) and an optional advance before the policy is issued."""
    PENDING, APPROVED, REJECTED = "PENDING", "APPROVED", "REJECTED"
    STATUSES = [(PENDING, "Awaiting credit control"), (APPROVED, "Approved"), (REJECTED, "Rejected")]
    policy = models.OneToOneField(PncPolicy, on_delete=models.CASCADE, related_name="credit")
    installments = models.PositiveSmallIntegerField(default=3)
    advance_amount = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING, db_index=True)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    requested_on = models.DateTimeField(auto_now_add=True)
    request_note = models.CharField(max_length=200, blank=True)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_on = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=200, blank=True)


class PncInstallment(models.Model):
    policy = models.ForeignKey(PncPolicy, on_delete=models.CASCADE, related_name="installments")
    number = models.PositiveSmallIntegerField()
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        ordering = ["policy_id", "number"]
        unique_together = ("policy", "number")


class PncCession(models.Model):
    """Reinsurance share of one risk, stored in base currency (same idea as motor cessions)."""
    policy = models.ForeignKey(PncPolicy, on_delete=models.CASCADE, related_name="cessions")
    risk = models.ForeignKey(PncRisk, null=True, blank=True, on_delete=models.CASCADE, related_name="cessions")
    treaty = models.ForeignKey("reinsurance.Treaty", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    treaty_type = models.CharField(max_length=8)
    rank = models.PositiveSmallIntegerField(default=0)
    sum_insured_ceded = models.DecimalField(max_digits=16, decimal_places=3, default=ZERO)
    premium_ceded = models.DecimalField(max_digits=16, decimal_places=3, default=ZERO)
    cession_pct = models.DecimalField(max_digits=7, decimal_places=3, default=ZERO)
    is_facultative = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["policy_id", "risk_id", "rank", "id"]


# ---- claims -----------------------------------------------------------------------------------------------------
class PncClaim(models.Model):
    OPEN, ASSESSED, APPROVED, SETTLED, CLOSED, REJECTED = "OPEN", "ASSESSED", "APPROVED", "SETTLED", "CLOSED", "REJECTED"
    STATUSES = [(OPEN, "Open"), (ASSESSED, "Assessed"), (APPROVED, "Approved"), (SETTLED, "Settled"), (CLOSED, "Closed"), (REJECTED, "Rejected")]
    LOCKED = (SETTLED, CLOSED, REJECTED)
    CAUSES = [("FIRE", "Fire"), ("LIGHTNING", "Lightning / explosion"), ("FLOOD", "Flood / storm"), ("THEFT", "Theft / burglary"), ("MACHINERY", "Machinery breakdown"),
              ("CONSTRUCTION", "Construction / erection damage"), ("MARINE", "Marine casualty / cargo damage"), ("LIABILITY_PD", "Third-party property damage"),
              ("LIABILITY_BI", "Third-party bodily injury"), ("EMPLOYEE", "Employee injury / illness"), ("PROFESSIONAL", "Professional negligence"), ("OTHER", "Other")]
    claim_no = models.CharField(max_length=24, unique=True)
    policy = models.ForeignKey(PncPolicy, on_delete=models.PROTECT, related_name="claims")
    risk = models.ForeignKey(PncRisk, on_delete=models.PROTECT, related_name="claims")
    date_of_loss = models.DateField()
    reported_on = models.DateField()
    cause = models.CharField(max_length=14, choices=CAUSES, default="OTHER")
    description = models.TextField(blank=True)
    location = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=OPEN, db_index=True)
    reserve = models.DecimalField("Reserve (gross loss estimate)", max_digits=16, decimal_places=3, default=ZERO)
    adjuster = models.ForeignKey("claims.Provider", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    adjuster_report_on = models.DateField(null=True, blank=True)
    assessed_loss = models.DecimalField(max_digits=16, decimal_places=3, default=ZERO)
    average_factor = models.DecimalField(max_digits=7, decimal_places=4, default=D("1"))
    deductible_applied = models.DecimalField(max_digits=16, decimal_places=3, default=ZERO)
    net_payable = models.DecimalField(max_digits=16, decimal_places=3, default=ZERO)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cat_event = models.ForeignKey("claims.CatEvent", null=True, blank=True, on_delete=models.SET_NULL, related_name="pnc_claims")
    is_legal = models.BooleanField(default=False)
    third_party_liable = models.BooleanField(default=False)
    reopen_count = models.PositiveSmallIntegerField(default=0)
    reopen_reason = models.CharField(max_length=200, blank=True)
    registered_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "P&C claim"

    def __str__(self):
        return self.claim_no

    @property
    def locked(self):
        return self.status in self.LOCKED

    @property
    def total_paid(self):
        return self.payments.aggregate(s=Sum("amount"))["s"] or ZERO

    @property
    def outstanding(self):
        return max(self.net_payable - self.total_paid, ZERO) if self.status in (self.APPROVED, self.SETTLED) else ZERO


class PncClaimPayment(models.Model):
    STAGES = [("INTERIM", "Interim payment"), ("FINAL", "Final payment")]
    claim = models.ForeignKey(PncClaim, on_delete=models.CASCADE, related_name="payments")
    stage = models.CharField(max_length=8, choices=STAGES)
    amount = models.DecimalField(max_digits=16, decimal_places=3)
    payee = models.CharField(max_length=120)
    reference = models.CharField(max_length=60, blank=True)
    paid_on = models.DateField()

    class Meta:
        ordering = ["paid_on", "id"]


class PncRecovery(models.Model):
    TYPES = [("SALVAGE", "Salvage"), ("SUBROGATION", "Subrogation"), ("REINSURANCE", "Reinsurance cash")]
    claim = models.ForeignKey(PncClaim, on_delete=models.CASCADE, related_name="recoveries")
    recovery_type = models.CharField(max_length=12, choices=TYPES)
    amount = models.DecimalField(max_digits=16, decimal_places=3)
    received_on = models.DateField()
    note = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["received_on", "id"]


# ---- medical, cargo declarations, reserve authority, reinsurer recoveries (non-motor update) ----------------------------
class PncMember(models.Model):
    """A covered person on a medical or group medical policy (the census)."""
    SELF, SPOUSE, CHILD, PARENT, OTHER = "SELF", "SPOUSE", "CHILD", "PARENT", "OTHER"
    RELATIONS = [(SELF, "Employee / principal"), (SPOUSE, "Spouse"), (CHILD, "Child"), (PARENT, "Parent"), (OTHER, "Other")]
    policy = models.ForeignKey(PncPolicy, on_delete=models.CASCADE, related_name="members")
    member_no = models.CharField(max_length=24)
    name = models.CharField(max_length=100)
    relation = models.CharField(max_length=8, choices=RELATIONS, default=SELF)
    category = models.CharField(max_length=40, blank=True, help_text="Benefit category")
    dob = models.DateField(null=True, blank=True)
    eligible_from = models.DateField()
    eligible_to = models.DateField(null=True, blank=True, help_text="Set when the member is removed; cover ends on this date")

    class Meta:
        ordering = ["policy_id", "member_no"]
        unique_together = [("policy", "member_no")]

    def eligible_on(self, d):
        return self.eligible_from <= d and (self.eligible_to is None or d <= self.eligible_to)

    @property
    def limit(self):
        risk = self.policy.risks.filter(active=True).first()
        return risk.sum_insured if risk else ZERO

    @property
    def used(self):
        return self.preauths.filter(status__in=["APPROVED", "SETTLED"]).aggregate(s=models.Sum("approved_amount"))["s"] or ZERO

    @property
    def remaining(self):
        return max(self.limit - self.used, ZERO)


class PncPreauth(models.Model):
    """Cashless pre-authorisation. It earmarks benefit limit; it is not the final settlement."""
    REQUESTED, APPROVED, DECLINED, SETTLED = "REQUESTED", "APPROVED", "DECLINED", "SETTLED"
    STATUSES = [(REQUESTED, "Requested"), (APPROVED, "Approved"), (DECLINED, "Declined"), (SETTLED, "Settled")]
    member = models.ForeignKey(PncMember, on_delete=models.CASCADE, related_name="preauths")
    number = models.CharField(max_length=24, unique=True)
    provider = models.CharField(max_length=100)
    service_date = models.DateField()
    diagnosis_code = models.CharField(max_length=20, blank=True)
    requested_amount = models.DecimalField(max_digits=14, decimal_places=3)
    approved_amount = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    actual_amount = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    invoice_no = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=REQUESTED)
    note = models.CharField(max_length=200, blank=True)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class PncDeclaration(models.Model):
    """A shipment declared under an open cargo cover."""
    policy = models.ForeignKey(PncPolicy, on_delete=models.CASCADE, related_name="declarations")
    risk = models.ForeignKey(PncRisk, on_delete=models.PROTECT, related_name="declarations")
    number = models.CharField(max_length=24, unique=True)
    declared_on = models.DateField()
    voyage = models.CharField(max_length=120)
    conveyance = models.CharField(max_length=80, blank=True)
    invoice_no = models.CharField(max_length=40, blank=True)
    value = models.DecimalField(max_digits=16, decimal_places=3)
    premium = models.DecimalField(max_digits=14, decimal_places=3, default=ZERO)
    cancelled = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-declared_on", "-id"]


class PncReserveChange(models.Model):
    """Every reserve revision: reason, who asked, who approved. Larger changes wait for a manager."""
    PENDING, APPLIED, REJECTED = "PENDING", "APPLIED", "REJECTED"
    claim = models.ForeignKey(PncClaim, on_delete=models.CASCADE, related_name="reserve_changes")
    old_reserve = models.DecimalField(max_digits=16, decimal_places=3)
    new_reserve = models.DecimalField(max_digits=16, decimal_places=3)
    reason = models.CharField(max_length=200)
    status = models.CharField(max_length=10, default=PENDING)
    requested_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class PncRiRecovery(models.Model):
    """Reinsurance recoverable on one claim payment, tracked per reinsurer / treaty: estimated, agreed, billed, disputed, collected."""
    ESTIMATED, AGREED, BILLED, DISPUTED, COLLECTED = "ESTIMATED", "AGREED", "BILLED", "DISPUTED", "COLLECTED"
    STATUSES = [(ESTIMATED, "Estimated"), (AGREED, "Agreed"), (BILLED, "Billed"), (DISPUTED, "Disputed"), (COLLECTED, "Collected")]
    claim = models.ForeignKey(PncClaim, on_delete=models.CASCADE, related_name="ri_recoveries")
    payment = models.ForeignKey("pnc.PncClaimPayment", null=True, blank=True, on_delete=models.SET_NULL, related_name="ri_recoveries")
    treaty = models.ForeignKey("reinsurance.Treaty", on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=8)
    amount = models.DecimalField("Recoverable (base currency)", max_digits=16, decimal_places=3)
    status = models.CharField(max_length=10, choices=STATUSES, default=ESTIMATED)
    billed_on = models.DateField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)
    collected = models.DecimalField(max_digits=16, decimal_places=3, default=ZERO)
    collected_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    @property
    def outstanding(self):
        return max(self.amount - self.collected, ZERO)

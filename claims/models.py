from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models


class Provider(models.Model):
    SURVEYOR, GARAGE, HOSPITAL, LEGAL = "SURVEYOR", "GARAGE", "HOSPITAL", "LEGAL"
    TYPES = [
        (SURVEYOR, "Surveyor / Loss adjuster"),
        (GARAGE, "Garage / Repairer"),
        (HOSPITAL, "Hospital / Medical"),
        (LEGAL, "Legal counsel"),
    ]
    provider_type = models.CharField(max_length=10, choices=TYPES)
    name = models.CharField(max_length=100)
    governorate = models.CharField(max_length=40, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    preferred = models.BooleanField(default=False, help_text="Preferred (network) panel member")
    specialities = models.CharField(max_length=120, blank=True, help_text="e.g. THEFT, COLLISION")
    room_cap_per_day = models.DecimalField(max_digits=10, decimal_places=3, default=Decimal("0"), help_text="Hospitals")
    average_score = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0"))
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["provider_type", "-preferred", "name"]

    def __str__(self):
        return self.name

    def refresh_score(self):
        from django.db.models import Avg

        a = self.ratings.aggregate(a=Avg("overall"))["a"]
        self.average_score = Decimal(str(round(a or 0, 2)))
        self.save(update_fields=["average_score"])


class CatEvent(models.Model):
    """A catastrophe (flood, cyclone, earthquake ...). Claims flagged with the event are aggregated for cat excess-of-loss recoveries."""
    TYPES = [("FLOOD", "Flood"), ("CYCLONE", "Cyclone / storm"), ("EARTHQUAKE", "Earthquake"), ("FIRE", "Fire / explosion"), ("SANDSTORM", "Sandstorm"), ("OTHER", "Other")]
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    event_type = models.CharField(max_length=12, choices=TYPES, default="OTHER")
    date_from = models.DateField()
    date_to = models.DateField(null=True, blank=True)
    description = models.CharField(max_length=250, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-date_from", "code"]

    def __str__(self):
        return f"{self.code} {self.name}"


class Claim(models.Model):
    OPEN, ASSESSED, APPROVED, SETTLED, CLOSED, REJECTED = "OPEN", "ASSESSED", "APPROVED", "SETTLED", "CLOSED", "REJECTED"
    STATUSES = [
        (OPEN, "Open"),
        (ASSESSED, "Assessed"),
        (APPROVED, "Approved"),
        (SETTLED, "Settled"),
        (CLOSED, "Financially closed"),
        (REJECTED, "Rejected"),
    ]
    LOSS_TYPES = [
        ("COLLISION", "Collision"),
        ("THEFT", "Theft"),
        ("FIRE", "Fire"),
        ("NATURAL", "Natural disaster / weather"),
        ("TP_PROPERTY", "Third party property damage"),
        ("TP_INJURY", "Third party bodily injury"),
        ("OTHER", "Other"),
    ]
    claim_no = models.CharField(max_length=30, unique=True)
    policy = models.ForeignKey("motor.Policy", on_delete=models.PROTECT, related_name="claims")
    risk = models.ForeignKey("motor.PolicyRisk", on_delete=models.PROTECT, related_name="claims")
    status = models.CharField(max_length=10, choices=STATUSES, default=OPEN)
    date_of_loss = models.DateField()
    loss_type = models.CharField(max_length=12, choices=LOSS_TYPES)
    cause_of_loss = models.TextField()
    location = models.CharField(max_length=120, blank=True)
    governorate = models.CharField(max_length=40, blank=True)
    police_report_no = models.CharField(max_length=40, blank=True)
    estimated_damage = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))

    driver_age = models.PositiveSmallIntegerField(null=True, blank=True)
    young_driver = models.BooleanField(default=False)
    undisclosed_driver = models.BooleanField(default=False)
    third_party_liable = models.BooleanField(default=False, help_text="Flag potential subrogation")
    large_loss = models.BooleanField(default=False)

    # all stored in POLICY currency
    reserve = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    approved_repair_cost = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    excess_applied = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    excess_breakdown = models.JSONField(default=dict, blank=True)
    net_payable = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    total_paid = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    is_total_loss = models.BooleanField(default=False)
    depreciated_value = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0"),
        help_text="Market value at date of loss (reducing balance), policy currency. Set when marked total loss.")
    driver = models.ForeignKey("motor.Driver", null=True, blank=True, on_delete=models.SET_NULL, related_name="claims")
    damage_zones = models.JSONField(default=dict, blank=True, help_text="{zone_id: severity 1-3} from the damage map")
    damage_notes = models.CharField(max_length=200, blank=True)
    ai_estimate = models.JSONField(default=dict, blank=True, help_text="Last AI repair-cost prediction")
    flex = models.JSONField(default=dict, blank=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    garage = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.SET_NULL, related_name="repair_claims")
    surveyor = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.SET_NULL, related_name="survey_claims")
    surveyor_assigned_on = models.DateField(null=True, blank=True)
    surveyor_report_on = models.DateField(null=True, blank=True)

    file_finalised_on = models.DateField(null=True, blank=True, help_text="Starts the FSA 30-day repair clock")
    repair_completed_on = models.DateField(null=True, blank=True)
    repair_delay_days = models.PositiveSmallIntegerField(default=0)
    delay_compensation = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))

    # legal, excess, estimate and reopen tracking
    is_legal = models.BooleanField(default=False, db_index=True, help_text="The claim is in a legal process (see Legal actions)")
    legal_since = models.DateField(null=True, blank=True)
    excess_collected = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Motor vehicle excess collected from the insured")
    excess_collected_on = models.DateField(null=True, blank=True)
    excess_waiver_reason = models.CharField(max_length=200, blank=True)
    auto_estimate = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Total cost estimated from the details provided")
    auto_estimate_detail = models.JSONField(default=dict, blank=True)
    estimate_overridden = models.BooleanField(default=False, help_text="The reserve was set by a person and is not recalculated automatically")
    cat_event = models.ForeignKey(CatEvent, null=True, blank=True, on_delete=models.SET_NULL, related_name="claims")
    overpaid = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Paid above the final invoiced cost; to be recovered")
    reopen_count = models.PositiveSmallIntegerField(default=0)
    reopened_on = models.DateField(null=True, blank=True)
    reopen_reason = models.CharField(max_length=200, blank=True)
    registered_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.claim_no

    @property
    def currency(self):
        return self.policy.currency

    # states in which the claim is read only until it is reopened
    LOCKED = (SETTLED, CLOSED, REJECTED)

    @property
    def locked(self):
        return self.status in self.LOCKED

    @property
    def gross_incurred(self):
        return self.approved_repair_cost or self.reserve

    @property
    def recoveries_total(self):
        r = self.recoveries.aggregate(t=models.Sum("amount"))["t"] or Decimal("0")
        return r

    @property
    def surveyor_days(self):
        if not self.surveyor_assigned_on:
            return None
        from datetime import date

        end = self.surveyor_report_on or date.today()
        return (end - self.surveyor_assigned_on).days


class ClaimDocument(models.Model):
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=20, choices=[("PHOTO", "Photo"), ("POLICE", "Police report"), ("OTHER", "Other")])
    file = models.FileField(upload_to="claims/%Y/%m/")
    uploaded_at = models.DateTimeField(auto_now_add=True)


class ClaimPayment(models.Model):
    STAGES = [("INTERIM", "Pre-repair (70%)"), ("FINAL", "Post-repair (30%)"), ("FULL", "Full settlement"), ("DELAY", "Delay compensation"), ("HEAD", "Cover head payment")]
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="payments")
    stage = models.CharField(max_length=8, choices=STAGES)
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    paid_on = models.DateField()
    payee = models.CharField(max_length=100)
    reference = models.CharField(max_length=40, blank=True)


class Recovery(models.Model):
    SALVAGE, SUBROGATION, REINSURANCE, OVERPAYMENT = "SALVAGE", "SUBROGATION", "REINSURANCE", "OVERPAYMENT"
    TYPES = [(SALVAGE, "Salvage"), (SUBROGATION, "Subrogation"), (REINSURANCE, "Reinsurance"), (OVERPAYMENT, "Overpayment refund")]
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="recoveries")
    recovery_type = models.CharField(max_length=14, choices=TYPES)
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    received_on = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=150, blank=True)


class Subrogation(models.Model):
    DEMAND, NEGOTIATION, LITIGATION, RECOVERED, WRITTEN_OFF = (
        "DEMAND_SENT", "IN_NEGOTIATION", "IN_LITIGATION", "RECOVERED", "WRITTEN_OFF",
    )
    STATUSES = [
        (DEMAND, "Demand sent"),
        (NEGOTIATION, "In negotiation"),
        (LITIGATION, "In litigation"),
        (RECOVERED, "Recovered"),
        (WRITTEN_OFF, "Written off"),
    ]
    claim = models.OneToOneField(Claim, on_delete=models.CASCADE, related_name="subrogation")
    recovery_status = models.CharField(max_length=16, choices=STATUSES, default=DEMAND)
    liable_party = models.CharField(max_length=120)
    liable_insurer = models.CharField(max_length=120, blank=True)
    liable_insurer_email = models.EmailField(blank=True)
    their_policy_no = models.CharField(max_length=60, blank=True)
    their_vehicle = models.CharField(max_length=80, blank=True, help_text="Make / model / plate of the at-fault vehicle")
    their_claim_ref = models.CharField(max_length=60, blank=True)
    reserve_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    amount_recovered = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    excess_refunded = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    demand_sent_on = models.DateField(null=True, blank=True)
    legal_counsel = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.SET_NULL, limit_choices_to={"provider_type": "LEGAL"})
    note = models.CharField(max_length=200, blank=True)


class ProviderRating(models.Model):
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="ratings")
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="ratings")
    timeliness_score = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0"))
    cost_score = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0"))
    quality_score = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0"))
    customer_score = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    rework = models.BooleanField(default=False)
    overall = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0"))
    estimated_days = models.PositiveSmallIntegerField(default=0)
    actual_days = models.PositiveSmallIntegerField(default=0)
    estimate_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    final_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))

    class Meta:
        unique_together = ("provider", "claim")


class RepairEstimate(models.Model):
    LABOUR_ONLY, PARTS_ONLY, LABOUR_PARTS, FIXED = "LABOUR", "PARTS", "BOTH", "FIXED"
    BASES = [(LABOUR_ONLY, "Labour only"), (PARTS_ONLY, "Parts only"), (LABOUR_PARTS, "Labour + parts"), (FIXED, "Fixed amount as quoted")]
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="estimates")
    garage = models.ForeignKey(Provider, on_delete=models.PROTECT)
    pricing_basis = models.CharField(max_length=6, choices=BASES, default=LABOUR_PARTS)
    labour_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    parts_cost = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    fixed_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Lump sum as quoted by the garage")
    estimated_days = models.PositiveSmallIntegerField(default=7)
    exceeds_market = models.BooleanField(default=False)
    supplementary = models.BooleanField(default=False, help_text="Adds to the garage's earlier estimate instead of replacing it")
    superseded = models.BooleanField(default=False)
    invoice_no = models.CharField(max_length=40, blank=True)
    invoice_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    invoice_date = models.DateField(null=True, blank=True)
    settled = models.BooleanField(default=False)
    settled_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    @property
    def total(self):
        b = self.pricing_basis
        if b == self.LABOUR_ONLY:
            return self.labour_amount
        if b == self.PARTS_ONLY:
            return self.parts_cost
        if b == self.FIXED:
            return self.fixed_amount
        return self.labour_amount + self.parts_cost

    def __str__(self):
        return f"{self.garage.name} {self.get_pricing_basis_display()} {self.total}"


class SubroNotice(models.Model):
    """Letters to the at-fault party's insurer: notice of loss with damage details, repair confirmation, demand for payment."""
    OPENING, CONFIRMATION, DEMAND = "OPENING", "CONFIRMATION", "DEMAND"
    KINDS = [(OPENING, "Notice of claim and damage details"), (CONFIRMATION, "Repair confirmation"), (DEMAND, "Demand notice for payment")]
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="notices")
    kind = models.CharField(max_length=12, choices=KINDS)
    to_name = models.CharField(max_length=120)
    to_email = models.EmailField(blank=True)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)
    sent_on = models.DateTimeField(null=True, blank=True)
    delivery = models.CharField(max_length=60, blank=True, help_text="How it left the system: emailed / no address (draft)")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["created_at", "id"]


class LegalAction(models.Model):
    TYPES = [("DEMAND", "Demand / notice before action"), ("FILED", "Case filed"), ("HEARING", "Hearing"), ("EXPERT", "Expert / committee referral"),
             ("JUDGMENT", "Judgment"), ("APPEAL", "Appeal"), ("EXECUTION", "Execution of judgment"), ("SETTLEMENT", "Out of court settlement"), ("OTHER", "Other")]
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="legal_actions")
    action_type = models.CharField(max_length=10, choices=TYPES)
    action_date = models.DateField()
    court = models.CharField(max_length=100, blank=True)
    case_no = models.CharField(max_length=50, blank=True)
    counsel = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.SET_NULL, limit_choices_to={"provider_type": "LEGAL"}, related_name="legal_actions")
    amount_claimed = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    legal_cost = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    outcome = models.CharField(max_length=200, blank=True)
    next_date = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["action_date", "id"]


class Injury(models.Model):
    ROLES = [("INSURED", "Insured / policyholder"), ("DRIVER", "Driver of the insured vehicle"), ("PASSENGER", "Passenger in the insured vehicle"),
             ("TP_DRIVER", "Third-party driver"), ("TP_PASSENGER", "Third-party passenger"), ("PEDESTRIAN", "Pedestrian / other")]
    SEVERITIES = [("MINOR", "Minor"), ("MODERATE", "Moderate"), ("SEVERE", "Severe"), ("DISABILITY", "Permanent disability"), ("DEATH", "Death")]
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="injuries")
    person_name = models.CharField(max_length=100)
    role = models.CharField(max_length=14, choices=ROLES)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    severity = models.CharField(max_length=10, choices=SEVERITIES)
    injury_description = models.CharField(max_length=200, blank=True)
    hospital = models.ForeignKey(Provider, null=True, blank=True, on_delete=models.SET_NULL, limit_choices_to={"provider_type": "HOSPITAL"}, related_name="+")
    date_of_death = models.DateField(null=True, blank=True)
    compensation_reserve = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Blood money (diyya) / compensation, policy currency")
    medical_cost = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    @property
    def exposure(self):
        return self.compensation_reserve + self.medical_cost


class TreatyRecovery(models.Model):
    """Reinsurers' share of a claim payment, by treaty. Created automatically when a payment is made; settled by the cash received."""
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="treaty_recoveries")
    payment = models.ForeignKey(ClaimPayment, null=True, blank=True, on_delete=models.CASCADE, related_name="treaty_recoveries",
                                help_text="Empty for an adjustment row (reversal on closing, salvage)")
    journal_no = models.CharField(max_length=24, db_index=True)
    treaty = models.ForeignKey("reinsurance.Treaty", null=True, blank=True, on_delete=models.PROTECT, related_name="claim_recoveries")
    treaty_ref = models.CharField(max_length=40, db_index=True, help_text="Treaty reference, FAC or XOL")
    treaty_label = models.CharField(max_length=120, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=3, help_text="Base currency")
    received = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    posted_on = models.DateField()
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["posted_on", "id"]

    @property
    def due(self):
        return self.amount - self.received


class LetterOfGuarantee(models.Model):
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="guarantees")
    hospital = models.ForeignKey(Provider, on_delete=models.PROTECT, limit_choices_to={"provider_type": "HOSPITAL"})
    patient_name = models.CharField(max_length=100)
    guaranteed_amount = models.DecimalField(max_digits=14, decimal_places=3)
    issued_on = models.DateField(auto_now_add=True)


class MedicalBill(models.Model):
    claim = models.ForeignKey(LetterOfGuarantee, on_delete=models.CASCADE, related_name="bills")
    description = models.CharField(max_length=120)
    days = models.PositiveSmallIntegerField(default=1)
    rate_charged = models.DecimalField(max_digits=10, decimal_places=3)
    flagged = models.BooleanField(default=False)
    flag_reason = models.CharField(max_length=120, blank=True)


class ClaimAdjustment(models.Model):
    """Log of a financial adjustment to a claim, e.g. the estimate reversed to the invoiced amount when the claim is closed."""
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="adjustments")
    created_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=20, default="ESTIMATE_REVERSAL")
    description = models.CharField(max_length=250)
    before = models.DecimalField(max_digits=14, decimal_places=3)
    after = models.DecimalField(max_digits=14, decimal_places=3)
    delta = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        ordering = ["-created_at", "-id"]


class ClaimHead(models.Model):
    """One cover-level head of a claim (own damage, third party property, PA, medical, towage, ...) with its own estimate, excess,
    approval and partial payments. Approved heads add to the claim's net payable; payments go through a credit note released by Accounts."""
    OD, TPPD, TPBI, PA, MEDICAL, AMBULANCE, TOWAGE, OTHER = "OD", "TPPD", "TPBI", "PA", "MEDICAL", "AMBULANCE", "TOWAGE", "OTHER"
    HEADS = [(OD, "Own damage"), (TPPD, "Third party property damage"), (TPBI, "Third party bodily injury / death"),
             (PA, "Personal accident"), (MEDICAL, "Medical expenses"), (AMBULANCE, "Ambulance"), (TOWAGE, "Towage / recovery"), (OTHER, "Other")]
    PENDING, APPROVED, REJECTED = "PENDING", "APPROVED", "REJECTED"
    STATUSES = [(PENDING, "Estimated"), (APPROVED, "Approved"), (REJECTED, "Rejected")]
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="heads")
    head = models.CharField(max_length=10, choices=HEADS)
    description = models.CharField(max_length=160, blank=True)
    estimate = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    excess = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    recovery = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Salvage, depreciation or client recovery deducted")
    approved_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    limit_applied = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING)
    payee = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    @property
    def net(self):
        """What the company pays on this head once approved: approved amount less excess and recoveries."""
        if self.status != self.APPROVED:
            return Decimal("0")
        return max(self.approved_amount - self.excess - self.recovery, Decimal("0"))

    @property
    def paid(self):
        return self.releases.filter(status="PAID").aggregate(s=models.Sum("amount"))["s"] or Decimal("0")

    @property
    def requested(self):
        return self.releases.exclude(status="CANCELLED").aggregate(s=models.Sum("amount"))["s"] or Decimal("0")

    @property
    def balance(self):
        return self.net - self.requested


class PaymentRelease(models.Model):
    """Credit note raised by Claims for a (partial) head payment. Accounts disburses it, which creates the claim payment."""
    ISSUED, PAID, CANCELLED = "ISSUED", "PAID", "CANCELLED"
    STATUSES = [(ISSUED, "Awaiting Accounts"), (PAID, "Paid"), (CANCELLED, "Cancelled")]
    head = models.ForeignKey(ClaimHead, on_delete=models.CASCADE, related_name="releases")
    number = models.CharField(max_length=24, unique=True)
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    payee = models.CharField(max_length=100)
    status = models.CharField(max_length=10, choices=STATUSES, default=ISSUED)
    issued_on = models.DateField()
    paid_on = models.DateField(null=True, blank=True)
    issued_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    paid_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    payment = models.OneToOneField(ClaimPayment, null=True, blank=True, on_delete=models.SET_NULL, related_name="release")

    class Meta:
        ordering = ["-id"]

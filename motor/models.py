from datetime import date
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from core.models import Currency

VEHICLE_TYPES = [
    ("SALOON", "Saloon / Sedan"),
    ("SUV", "SUV / 4x4"),
    ("PICKUP", "Pickup"),
    ("VAN", "Van / Minibus"),
    ("BUS", "Bus"),
    ("TRUCK", "Truck / Lorry"),
    ("MOTORCYCLE", "Motorcycle"),
    ("TRAILER", "Trailer / Heavy equipment"),
]
USAGES = [
    ("PRIVATE", "Private"),
    ("COMMERCIAL", "Commercial"),
    ("TAXI", "Taxi"),
    ("RENTAL", "Rental / Self-drive"),
    ("DRIVING_SCHOOL", "Driving school"),
    ("GOVERNMENT", "Government"),
    ("AGRICULTURAL", "Agricultural / Construction"),
]
FUEL_TYPES = [("PETROL", "Petrol"), ("DIESEL", "Diesel"), ("ELECTRIC", "Electric"), ("HYBRID", "Hybrid")]
COVER_KINDS = [("OD", "Own Damage"), ("TP", "Third Party"), ("RIDER", "Rider / Extension")]


class Product(models.Model):
    code = models.CharField(max_length=12, unique=True)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    includes_od = models.BooleanField(default=True, help_text="Comprehensive covers Own Damage + Third Party")
    is_tariff_regulated = models.BooleanField(
        default=False,
        help_text="FSA-approved tariff: premium comes only from the tariff table, no discretionary discounts",
    )
    expense_ratio = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("20.00"), help_text="%")
    quote_validity_days = models.PositiveSmallIntegerField(default=30)
    standard_excess = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("50.000"), help_text="Base currency"
    )
    young_driver_excess = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("100.000"), help_text="Imposed / young driver, base currency"
    )
    undisclosed_driver_excess = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("150.000"), help_text="Base currency"
    )
    tp_limit = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("10000.000"),
        help_text="Third-party limit used for reinsurance (base currency). Set per the unified policy wording.",
    )
    depreciation_pct = models.DecimalField(
        "Depreciation % p.a.", max_digits=5, decimal_places=2, default=Decimal("15.00"),
        help_text="Reducing-balance rate used to value the vehicle at renewal and on a total-loss claim",
    )
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Cover(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=6, choices=COVER_KINDS)
    description = models.CharField(max_length=200, blank=True)
    rate_pct = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal("0"), help_text="% of sum insured (riders)"
    )
    flat_amount = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0"), help_text="Flat fee in base currency (riders)"
    )
    min_premium = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))
    only_products = models.CharField(max_length=60, blank=True, help_text="Product codes this rider can be sold with, comma separated (blank = all)")

    def __str__(self):
        return self.name

    def allowed_for(self, product):
        codes = [c.strip().upper() for c in self.only_products.split(",") if c.strip()]
        return not codes or product.code.upper() in codes


class ProductCover(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="product_covers")
    cover = models.ForeignKey(Cover, on_delete=models.CASCADE)
    mandatory = models.BooleanField(default=True)
    waivable = models.BooleanField(default=False, help_text="Underwriter may waive with a reason code")
    regulatory = models.BooleanField(default=False, help_text="Required by the FSA unified policy: never waivable")

    class Meta:
        unique_together = ("product", "cover")

    def __str__(self):
        return f"{self.product} / {self.cover}"


class RateTable(models.Model):
    """Base rates. Blank dimensions mean 'any'. The most specific matching row wins."""

    cover = models.ForeignKey(Cover, on_delete=models.CASCADE, limit_choices_to={"kind__in": ["OD", "TP"]})
    vehicle_type = models.CharField(max_length=12, choices=VEHICLE_TYPES, blank=True)
    usage = models.CharField(max_length=16, choices=USAGES, blank=True)
    cc_min = models.PositiveIntegerField(default=0)
    cc_max = models.PositiveIntegerField(default=99999)
    seats_min = models.PositiveSmallIntegerField(default=0)
    seats_max = models.PositiveSmallIntegerField(default=99)
    age_min = models.PositiveSmallIntegerField(default=0, help_text="Vehicle age in years")
    age_max = models.PositiveSmallIntegerField(default=99)
    rate_pct = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"), help_text="% of sum insured")
    flat_premium = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal("0"), help_text="Flat premium in base currency (TP tariff)"
    )
    min_premium = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"))

    def __str__(self):
        return f"{self.cover.code} {self.vehicle_type or '*'}/{self.usage or '*'} cc{self.cc_min}-{self.cc_max}"

    @property
    def specificity(self):
        return (
            (2 if self.vehicle_type else 0)
            + (2 if self.usage else 0)
            + (1 if self.cc_min > 0 or self.cc_max < 99999 else 0)
            + (1 if self.seats_min > 0 or self.seats_max < 99 else 0)
            + (1 if self.age_min > 0 or self.age_max < 99 else 0)
        )


class Discount(models.Model):
    NCD, FLEET = "NCD", "FLEET"
    KINDS = [
        (NCD, "No Claim Discount (threshold = claim-free years)"),
        (FLEET, "Fleet discount (threshold = number of vehicles)"),
    ]
    kind = models.CharField(max_length=6, choices=KINDS)
    threshold = models.DecimalField(max_digits=12, decimal_places=3)
    pct = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        ordering = ["kind", "threshold"]

    def __str__(self):
        return f"{self.kind} ≥ {self.threshold}: {self.pct}%"


class Fee(models.Model):
    code = models.CharField(max_length=20, unique=True)  # ISSUANCE / ENDORSEMENT / TRANSFER
    name = models.CharField(max_length=60)
    amount = models.DecimalField(max_digits=12, decimal_places=3, help_text="Base currency")

    def __str__(self):
        return self.name


class ApprovalLimit(models.Model):
    """Authority limits per role. NULL = unlimited."""

    role = models.CharField(max_length=30, unique=True)
    max_sum_insured = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    max_discount_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    max_fleet_size = models.PositiveIntegerField(null=True, blank=True)
    can_approve = models.BooleanField(default=False)

    def __str__(self):
        return self.role


class CommissionRule(models.Model):
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.CASCADE)
    producer_type = models.CharField(max_length=10, choices=[("AGENT", "Agent"), ("BROKER", "Broker"), ("DIRECT", "Direct")])
    base_pct = models.DecimalField(max_digits=5, decimal_places=2)
    overrider_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    profit_share_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    profit_loss_ratio_threshold = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("60"))

    def __str__(self):
        return f"{self.producer_type} {self.base_pct}%"


class Customer(models.Model):
    INDIVIDUAL, CORPORATE = "INDIVIDUAL", "CORPORATE"
    customer_type = models.CharField(
        max_length=12, choices=[(INDIVIDUAL, "Individual"), (CORPORATE, "Corporate / Fleet")], default=INDIVIDUAL
    )
    name = models.CharField(max_length=120)
    civil_id = models.CharField(
        "Civil ID / Resident card", max_length=12, blank=True,
        validators=[RegexValidator(r"^\d{6,12}$", "Digits only (Oman civil ID is 8 digits).")],
    )
    cr_number = models.CharField("Commercial registration", max_length=20, blank=True)
    cr_registration_date = models.DateField("CR registration date", null=True, blank=True)
    cr_expiry_date = models.DateField("CR expiry date", null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    nationality = models.CharField(max_length=40, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=200, blank=True)
    government_owned = models.BooleanField("Government-owned", default=False, help_text="A government department or a government-owned company: required for government vehicles")
    flex = models.JSONField(default=dict, blank=True, help_text="Values of the admin-defined flex fields")
    is_demo = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def identifier(self):
        return self.civil_id or self.cr_number

    @property
    def age(self):
        if not self.date_of_birth:
            return None
        t = date.today()
        return t.year - self.date_of_birth.year - ((t.month, t.day) < (self.date_of_birth.month, self.date_of_birth.day))


chassis_validator = RegexValidator(
    r"^[A-HJ-NPR-Z0-9]{17}$", "Chassis / VIN must be 17 characters (letters except I, O, Q, and digits)."
)


class Vehicle(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="vehicles")
    make = models.CharField(max_length=40)
    model = models.CharField(max_length=40)
    year = models.PositiveSmallIntegerField()
    plate_code = models.CharField(max_length=4, blank=True, help_text="Omani plate letters")
    plate_number = models.CharField(max_length=10, blank=True)
    chassis_no = models.CharField(max_length=17, unique=True, validators=[chassis_validator])
    vehicle_key = models.CharField(
        max_length=90, unique=True, editable=False, default="",
        help_text="MAKE|MODEL|YEAR|CHASSIS: the vehicle's unique identity",
    )
    engine_no = models.CharField(max_length=30)
    color = models.CharField(max_length=30)
    vehicle_type = models.CharField(max_length=12, choices=VEHICLE_TYPES, default="SALOON")
    usage = models.CharField(max_length=16, choices=USAGES, default="PRIVATE")
    fuel_type = models.CharField(max_length=10, choices=FUEL_TYPES, default="PETROL")
    cc = models.PositiveIntegerField("Engine CC", default=1600)
    seats = models.PositiveSmallIntegerField(default=5)
    base_value = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True,
        help_text="Insured value (base currency) when first insured; depreciation runs from here",
    )
    base_date = models.DateField(null=True, blank=True)
    flex = models.JSONField(default=dict, blank=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["make", "model"]

    @staticmethod
    def make_key(make, model, year, chassis):
        return f"{make}|{model}|{year}|{chassis}".upper().replace(" ", "")[:90]

    def save(self, *a, **kw):
        self.chassis_no = (self.chassis_no or "").upper().replace(" ", "")
        self.vehicle_key = self.make_key(self.make, self.model, self.year, self.chassis_no)
        uf = kw.get("update_fields")
        if uf is not None and ("chassis_no" in uf or "make" in uf or "model" in uf or "year" in uf):
            kw["update_fields"] = list(set(uf) | {"vehicle_key", "chassis_no"})
        super().save(*a, **kw)

    def __str__(self):
        return f"{self.make} {self.model} {self.year} ({self.registration})"

    @property
    def registration(self):
        return f"{self.plate_number} {self.plate_code}".strip() or self.chassis_no[-6:]

    @property
    def age_years(self):
        return max(0, date.today().year - self.year)


class VehicleOwnership(models.Model):
    """Who owned (insured) a vehicle and when. The Vehicle record itself is never duplicated: a sale or transfer only
    closes one ownership and opens the next, so claims, policies and history stay on one profile."""

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="ownerships")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="ownerships")
    from_date = models.DateField()
    to_date = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=120, blank=True, help_text="How the vehicle came to this owner (first insured, policy, transfer endorsement ...)")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-from_date", "-id"]

    def __str__(self):
        return f"{self.vehicle.chassis_no} -> {self.customer.name}"

    @property
    def current(self):
        return self.to_date is None


class Proposal(models.Model):
    """A quotation which is converted to a proposal, approved, then issued as a policy."""

    QUOTE, PENDING, APPROVED, REJECTED, ISSUED, EXPIRED = "QUOTE", "PENDING", "APPROVED", "REJECTED", "ISSUED", "EXPIRED"
    PROPOSAL = "PROPOSAL"
    STATUSES = [
        (QUOTE, "Quote"),
        (PROPOSAL, "Proposal"),
        (PENDING, "Pending approval"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
        (ISSUED, "Issued"),
        (EXPIRED, "Expired"),
    ]

    number = models.CharField(max_length=30, unique=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=QUOTE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="proposals")
    producer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="proposals")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT)
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8, help_text="1 unit of currency in base currency")
    start_date = models.DateField()
    end_date = models.DateField()
    valid_until = models.DateField()
    ncd_years = models.PositiveSmallIntegerField("Claim-free years", default=0)
    voucher = models.ForeignKey("Voucher", null=True, blank=True, on_delete=models.SET_NULL, related_name="proposals")
    voucher_discount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    flex = models.JSONField(default=dict, blank=True)
    is_demo = models.BooleanField(default=False, db_index=True)
    discount_pct = models.DecimalField(
        "Additional discount %", max_digits=5, decimal_places=2, default=Decimal("0"), validators=[MinValueValidator(0)]
    )
    notes = models.TextField(blank=True)

    risk_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    expense_load = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    net_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    fees = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    tax = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    total_payable = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))

    approval_reasons = models.JSONField(default=list, blank=True)
    decision_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="decisions")
    decision_note = models.CharField(max_length=300, blank=True)
    renewal_of = models.ForeignKey("Policy", null=True, blank=True, on_delete=models.SET_NULL, related_name="renewals")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.number

    @property
    def is_editable(self):
        return self.status in (self.QUOTE, self.PROPOSAL, self.REJECTED)

    @property
    def is_expired(self):
        return self.status == self.QUOTE and self.valid_until < date.today()


class ProposalRisk(models.Model):
    proposal = models.ForeignKey(Proposal, on_delete=models.CASCADE, related_name="risks")
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT)
    sum_insured = models.DecimalField(max_digits=14, decimal_places=3, help_text="Policy currency")
    si_note = models.CharField(max_length=160, blank=True, help_text="How the sum insured was derived (e.g. depreciation)")
    riders = models.ManyToManyField(Cover, blank=True, related_name="+")
    waived_riders = models.ManyToManyField(Cover, blank=True, related_name="+")
    waiver_reason = models.CharField(max_length=200, blank=True)
    od_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    tp_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    rider_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    discount_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    net_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    breakdown = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.proposal.number} / {self.vehicle}"


class Policy(models.Model):
    ACTIVE, CANCELLED, EXPIRED, PENDING_FAC = "ACTIVE", "CANCELLED", "EXPIRED", "PENDING_FAC"
    STATUSES = [
        (ACTIVE, "Active"),
        (CANCELLED, "Cancelled"),
        (EXPIRED, "Expired"),
        (PENDING_FAC, "Awaiting facultative placement"),
    ]
    policy_no = models.CharField(max_length=30, unique=True)
    proposal = models.OneToOneField(Proposal, on_delete=models.PROTECT, related_name="policy")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="policies")
    producer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="policies")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT)
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8)
    status = models.CharField(max_length=12, choices=STATUSES, default=ACTIVE)
    start_date = models.DateField()
    end_date = models.DateField()
    version = models.PositiveSmallIntegerField(default=1)
    net_premium = models.DecimalField(max_digits=14, decimal_places=3)
    risk_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    expense_load = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    fees = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    tax = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    total_payable = models.DecimalField(max_digits=14, decimal_places=3)
    issued_at = models.DateTimeField(auto_now_add=True)
    issued_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="issued_policies")
    cancelled_on = models.DateField(null=True, blank=True)
    voucher_discount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    flex = models.JSONField(default=dict, blank=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-issued_at"]
        verbose_name_plural = "policies"

    @property
    def paid_amount(self):
        from django.db.models import Sum
        return self.payments.filter(status="PAID").aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def balance_due(self):
        return self.total_payable - self.paid_amount

    @property
    def payment_status(self):
        paid = self.paid_amount
        if paid <= 0:
            return "UNPAID"
        return "PAID" if paid >= self.total_payable else "PARTIAL"

    def __str__(self):
        return self.policy_no

    @property
    def days_to_expiry(self):
        return (self.end_date - date.today()).days

    @property
    def is_expiring(self):
        return self.status == self.ACTIVE and 0 <= self.days_to_expiry <= 45

    @property
    def total_sum_insured(self):
        return sum((r.sum_insured for r in self.risks.all()), Decimal("0"))


class PolicyRisk(models.Model):
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="risks")
    vehicle = models.ForeignKey(Vehicle, on_delete=models.PROTECT, related_name="risks_on_policies")
    # Snapshot of the vehicle at inception/endorsement (the vehicle master may change later)
    make = models.CharField(max_length=40)
    model = models.CharField(max_length=40)
    year = models.PositiveSmallIntegerField()
    plate = models.CharField(max_length=20, blank=True)
    chassis_no = models.CharField(max_length=17)
    engine_no = models.CharField(max_length=30)
    color = models.CharField(max_length=30)
    vehicle_type = models.CharField(max_length=12, choices=VEHICLE_TYPES)
    usage = models.CharField(max_length=16, choices=USAGES)
    cc = models.PositiveIntegerField()
    seats = models.PositiveSmallIntegerField()
    sum_insured = models.DecimalField(max_digits=14, decimal_places=3)
    accessories_value = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    riders = models.ManyToManyField(Cover, blank=True, related_name="+")
    od_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    tp_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    rider_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    discount_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    net_premium = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    breakdown = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.policy.policy_no} / {self.plate or self.chassis_no}"

    @property
    def base_sum_insured(self):
        return self.policy.currency.quantize(self.sum_insured) * self.policy.fx_rate

    def calculate_total_excess(self, driver_age=None, is_young_driver=False, undisclosed_driver=False):
        """Total excess in policy currency: standard + imposed (young driver) + undisclosed driver."""
        from core.models import convert

        product = self.policy.product
        base = Currency.base()
        cur = self.policy.currency
        parts = {"standard": product.standard_excess}
        young = is_young_driver or (driver_age is not None and driver_age < settings.YOUNG_DRIVER_AGE)
        if young:
            parts["imposed"] = product.young_driver_excess
        if undisclosed_driver:
            parts["undisclosed"] = product.undisclosed_driver_excess
        total_base = sum((Decimal(v) for v in parts.values()), Decimal("0"))
        # all parts are held in base currency; convert the total to policy currency
        return convert(total_base, base, cur), {k: convert(v, base, cur) for k, v in parts.items()}


class Endorsement(models.Model):
    EXTENSION, CANCELLATION, TRANSFER = "EXTENSION", "CANCELLATION", "TRANSFER"
    ENGINE, COLOR, USAGE, ADDITIONS = "ENGINE", "COLOR", "USAGE", "ADDITIONS"
    REGNO, ADDDRIVER = "REGNO", "ADDDRIVER"
    TYPES = [
        (EXTENSION, "Extension of period"),
        (CANCELLATION, "Cancellation (mid-term)"),
        (TRANSFER, "Transfer of ownership"),
        (ENGINE, "Change of engine"),
        (COLOR, "Change of color"),
        (USAGE, "Change of usage"),
        (ADDITIONS, "Custom additions (accessories)"),
        (REGNO, "Change of registration number"),
        (ADDDRIVER, "Add an additional driver"),
    ]
    number = models.CharField(max_length=34, unique=True)
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="endorsements")
    risk = models.ForeignKey(PolicyRisk, null=True, blank=True, on_delete=models.SET_NULL)
    endorsement_type = models.CharField(max_length=12, choices=TYPES)
    effective_date = models.DateField()
    details = models.JSONField(default=dict, blank=True)
    premium_adjustment = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    fee = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    tax = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    total = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    version_after = models.PositiveSmallIntegerField()
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.number


class Commission(models.Model):
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="commissions")
    producer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="commissions")
    kind = models.CharField(
        max_length=10, choices=[("BASE", "Base"), ("OVERRIDER", "Overrider"), ("PROFIT", "Profit"), ("CLAWBACK", "Clawback")]
    )
    pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    amount = models.DecimalField(max_digits=14, decimal_places=3, help_text="Base currency; negative for clawback")
    note = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    settled_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Paid to the producer so far (base currency)")
    settled_on = models.DateField(null=True, blank=True)
    settlement = models.ForeignKey("CommissionSettlement", null=True, blank=True, on_delete=models.SET_NULL, related_name="lines")

    def __str__(self):
        return f"{self.policy.policy_no} {self.kind} {self.amount}"


class CommissionSettlement(models.Model):
    """A payment of commission to an agent / broker. It settles commission on premium that has been collected."""

    number = models.CharField(max_length=24, unique=True)
    producer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="commission_settlements")
    paid_on = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=3, help_text="Base currency")
    reference = models.CharField(max_length=60, blank=True, help_text="Cheque / transfer reference")
    note = models.CharField(max_length=160, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-paid_on", "-id"]

    def __str__(self):
        return self.number


class PolicyDocument(models.Model):
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=20, default="SCHEDULE")
    sent_to = models.CharField(max_length=120, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)


class ReminderLog(models.Model):
    """Prevents duplicate renewal reminders (T-30, T-7, T-0)."""

    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="reminders")
    offset_days = models.PositiveSmallIntegerField()
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("policy", "offset_days")


class Driver(models.Model):
    """Additional (named) driver on a vehicle. Attached to the quote risk and copied to the policy risk at issuance."""

    RELATIONS = [("SPOUSE", "Spouse"), ("CHILD", "Son / daughter"), ("FAMILY", "Other family"), ("EMPLOYEE", "Employee"),
                 ("FRIEND", "Friend"), ("OTHER", "Other")]
    proposal_risk = models.ForeignKey(ProposalRisk, null=True, blank=True, on_delete=models.CASCADE, related_name="drivers")
    policy_risk = models.ForeignKey(PolicyRisk, null=True, blank=True, on_delete=models.CASCADE, related_name="drivers")
    name = models.CharField(max_length=100)
    licence_no = models.CharField("Driving licence no.", max_length=30)
    licence_issue_date = models.DateField(null=True, blank=True)
    date_of_birth = models.DateField()
    nationality = models.CharField(max_length=40, blank=True)
    relationship = models.CharField(max_length=10, choices=RELATIONS, default="OTHER")
    added_on = models.DateField(default=date.today)
    endorsement = models.ForeignKey("Endorsement", null=True, blank=True, on_delete=models.SET_NULL, related_name="drivers")

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.name} ({self.licence_no})"

    @property
    def age(self):
        t = date.today()
        b = self.date_of_birth
        return t.year - b.year - ((t.month, t.day) < (b.month, b.day))

    def age_on(self, d):
        b = self.date_of_birth
        return d.year - b.year - ((d.month, d.day) < (b.month, b.day))

    @property
    def is_young(self):
        return self.age < settings.YOUNG_DRIVER_AGE


class Voucher(models.Model):
    PERCENT, FIXED = "PERCENT", "FIXED"
    TYPES = [(PERCENT, "Percentage off"), (FIXED, "Fixed amount off")]
    code = models.CharField(max_length=24, unique=True)
    description = models.CharField(max_length=120, blank=True)
    discount_type = models.CharField(max_length=8, choices=TYPES, default=PERCENT)
    value = models.DecimalField(max_digits=10, decimal_places=3, help_text="% or an amount in the voucher currency")
    currency = models.ForeignKey(Currency, null=True, blank=True, on_delete=models.PROTECT,
                                 help_text="For fixed vouchers (blank = base currency)")
    valid_from = models.DateField(default=date.today)
    valid_to = models.DateField()
    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text="Blank = unlimited")
    min_net_premium = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("0"), help_text="Base currency")
    products = models.ManyToManyField(Product, blank=True, help_text="Blank = every non-regulated product")
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="vouchers",
                                 help_text="Restrict to one customer")
    active = models.BooleanField(default=True)
    issued_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.code

    def save(self, *a, **kw):
        self.code = (self.code or "").strip().upper()
        super().save(*a, **kw)

    @property
    def used_count(self):
        return self.redemptions.count()

    @property
    def label(self):
        if self.discount_type == self.PERCENT:
            return f"{self.value.normalize():f}% off"
        cur = self.currency or Currency.base()
        return f"{cur.code} {self.value.normalize():f} off"

    @property
    def status(self):
        today = date.today()
        if not self.active:
            return "Inactive"
        if self.valid_to < today:
            return "Expired"
        if self.valid_from > today:
            return "Scheduled"
        if self.max_uses is not None and self.used_count >= self.max_uses:
            return "Used up"
        return "Live"


class VoucherRedemption(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.PROTECT, related_name="redemptions")
    policy = models.OneToOneField(Policy, on_delete=models.CASCADE, related_name="voucher_redemption")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(max_digits=14, decimal_places=3, help_text="Policy currency")
    redeemed_at = models.DateTimeField(auto_now_add=True)


class DocumentShare(models.Model):
    """Expiring public link to a set of policy documents (used in WhatsApp / e-mail messages)."""

    token = models.CharField(max_length=40, unique=True, db_index=True)
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="shares")
    documents = models.JSONField(default=list, help_text="e.g. ['schedule', 'invoice', 'dn', 'receipt:12']")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    channel = models.CharField(max_length=12, blank=True)
    downloads = models.PositiveIntegerField(default=0)


class CreditApproval(models.Model):
    """Credit-basis issuance: a policy may be issued without full payment only after Credit Control approves instalments."""
    PENDING, APPROVED, REJECTED, REFERRED = "PENDING", "APPROVED", "REJECTED", "REFERRED"
    STATUSES = [(PENDING, "Awaiting credit control"), (APPROVED, "Approved"), (REJECTED, "Rejected"), (REFERRED, "Referred back")]
    uid = models.CharField("Request UID", max_length=24, blank=True, db_index=True)
    proposal = models.OneToOneField("motor.Proposal", on_delete=models.CASCADE, related_name="credit")
    policy = models.OneToOneField("motor.Policy", null=True, blank=True, on_delete=models.SET_NULL, related_name="credit")
    installments = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1)], help_text="1 to 12 monthly instalments")
    advance_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Optional advance collected at issue (policy currency)")
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING, db_index=True)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    requested_on = models.DateTimeField(auto_now_add=True)
    request_note = models.CharField(max_length=200, blank=True)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    decided_on = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=200, blank=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-requested_on"]

    @property
    def is_new(self):
        from django.utils import timezone
        return self.status == self.PENDING and (timezone.now() - self.requested_on).total_seconds() < 86400


class PolicyInstallment(models.Model):
    """Premium due dates of a credit policy. Number 0 is the advance (due at issue). Receipts settle instalments oldest first."""
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name="installments")
    number = models.PositiveSmallIntegerField()
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        ordering = ["policy_id", "number"]
        unique_together = ("policy", "number")


class VehicleMake(models.Model):
    name = models.CharField(max_length=40, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class VehicleModel(models.Model):
    make = models.ForeignKey(VehicleMake, on_delete=models.CASCADE, related_name="models")
    name = models.CharField(max_length=40)

    class Meta:
        ordering = ["make__name", "name"]
        unique_together = ("make", "name")

    def __str__(self):
        return f"{self.make.name} {self.name}"


class ProducerAssignment(models.Model):
    """Which broker / agent services a customer. The customer stays with the current one until a new one is assigned; history is kept."""
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="producer_history")
    producer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="customer_assignments")
    from_date = models.DateField()
    to_date = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    assigned_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-from_date", "-id"]

    @property
    def current(self):
        return self.to_date is None


class CreditRequestLog(models.Model):
    """Archive of an earlier credit request on a proposal (for example one that Credit Control declined)."""
    proposal = models.ForeignKey("motor.Proposal", on_delete=models.CASCADE, related_name="credit_history")
    uid = models.CharField(max_length=24)
    installments = models.PositiveSmallIntegerField()
    advance_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    status = models.CharField(max_length=10)
    requested_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    requested_on = models.DateTimeField()
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decision_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["requested_on"]


class PostDatedCheque(models.Model):
    """Post-dated cheques received towards an agreed instalment plan (one per instalment, optional)."""
    RECEIVED, DEPOSITED, CLEARED, BOUNCED, RETURNED = "RECEIVED", "DEPOSITED", "CLEARED", "BOUNCED", "RETURNED"
    STATUSES = [(RECEIVED, "Received"), (DEPOSITED, "Deposited"), (CLEARED, "Cleared"), (BOUNCED, "Bounced"), (RETURNED, "Returned to client")]
    proposal = models.ForeignKey("motor.Proposal", on_delete=models.CASCADE, related_name="pdcs")
    policy = models.ForeignKey(Policy, null=True, blank=True, on_delete=models.SET_NULL, related_name="pdcs")
    installment_no = models.PositiveSmallIntegerField(null=True, blank=True)
    cheque_no = models.CharField(max_length=30)
    bank = models.CharField(max_length=60)
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    cheque_date = models.DateField()
    received_on = models.DateField()
    status = models.CharField(max_length=10, choices=STATUSES, default=RECEIVED)
    payment = models.ForeignKey("payments.Payment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=150, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["cheque_date", "id"]


class FinanceInterest(models.Model):
    """Finance (lien / loss payee) on a vehicle or policy. A claim settlement pays the financier first, against its clearance letter."""
    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="finance_interests")
    policy = models.ForeignKey(Policy, null=True, blank=True, on_delete=models.SET_NULL, related_name="finance_interests")
    financier = models.CharField(max_length=100)
    contract_no = models.CharField(max_length=40, blank=True)
    financed_amount = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    from_date = models.DateField()
    to_date = models.DateField(null=True, blank=True, help_text="Set when the finance is released")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-from_date", "-id"]

    @property
    def active(self):
        return self.to_date is None

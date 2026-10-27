from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver


class Currency(models.Model):
    """Issuance currency. ``rate_to_base`` = how many base-currency units one unit is worth."""

    code = models.CharField(max_length=3, primary_key=True)
    name = models.CharField(max_length=60)
    symbol = models.CharField(max_length=8, help_text="Glyph shown next to amounts")
    symbol_fallback = models.CharField(
        max_length=8, blank=True, help_text="Shown when the device font has no glyph for the symbol"
    )
    decimals = models.PositiveSmallIntegerField(default=2)
    rate_to_base = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal("1"))
    is_base = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "currencies"
        ordering = ["-is_base", "code"]

    def __str__(self):
        return self.code

    @property
    def label(self):
        return f"{self.code} — {self.name}"

    def quantize(self, amount):
        q = Decimal(1).scaleb(-self.decimals)
        return Decimal(amount).quantize(q, rounding=ROUND_HALF_UP)

    @classmethod
    def base(cls):
        return cls.objects.get(code=settings.BASE_CURRENCY)


def convert(amount, from_cur, to_cur):
    """Convert ``amount`` between currencies through the base currency."""
    if from_cur.code == to_cur.code:
        return to_cur.quantize(amount)
    in_base = Decimal(amount) * from_cur.rate_to_base
    return to_cur.quantize(in_base / to_cur.rate_to_base)


def to_base(amount, currency):
    return Currency.base().quantize(Decimal(amount) * currency.rate_to_base)


class DocumentSequence(models.Model):
    name = models.CharField(max_length=40, unique=True)
    last = models.PositiveBigIntegerField(default=0)

    @classmethod
    def next(cls, name):
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(name=name)
            seq.last += 1
            seq.save(update_fields=["last"])
            return seq.last


class AuditLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=10)
    model = models.CharField(max_length=80, db_index=True)
    object_id = models.CharField(max_length=40)
    object_repr = models.CharField(max_length=200, blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]


class ChangeLog(models.Model):
    """Field level history of every change to policies, claims and their parts (who, when, old value, new value)."""
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    confirmed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=10)                 # create / update / delete
    model = models.CharField(max_length=60, db_index=True)   # e.g. claims.Claim
    object_id = models.CharField(max_length=40)
    object_repr = models.CharField(max_length=200, blank=True)
    ref = models.CharField(max_length=60, blank=True, db_index=True, help_text="Policy or claim number the change belongs to")
    field = models.CharField(max_length=60, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-timestamp", "-id"]


class PendingAction(models.Model):
    """Maker-checker: a change to a policy or claim waits here until a second person confirms it."""
    PENDING, DONE, REJECTED, FAILED = "PENDING", "DONE", "REJECTED", "FAILED"
    STATUSES = [(PENDING, "Waiting for confirmation"), (DONE, "Confirmed and applied"), (REJECTED, "Rejected"), (FAILED, "Confirmed but could not be applied")]
    number = models.CharField(max_length=20, unique=True)
    kind = models.CharField(max_length=10)                    # claim / policy
    object_id = models.PositiveIntegerField()
    ref = models.CharField(max_length=60)
    action = models.CharField(max_length=30)
    summary = models.CharField(max_length=250)
    payload = models.JSONField(default=dict)
    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING)
    requested_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]


ROLES = [
    ("ADMIN", "Admin"),
    ("AGENT", "Agent"),
    ("BROKER", "Broker"),
    ("PRODUCTION_MANAGER", "Production Manager"),
    ("UNDERWRITER", "Underwriter"),
    ("CLAIMS_HANDLER", "Claims Handler"),
    ("CLAIMS_MANAGER", "Claims Manager"),
    ("FINANCE", "Finance / Reinsurance"),
    ("CREDIT_CONTROL", "Credit Control"),
    ("CUSTOMER", "Customer"),
]
ROLE_LABELS = dict(ROLES)
GROUP_NAMES = {code: label for code, label in ROLES}


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    phone = models.CharField(max_length=30, blank=True)
    producer_type = models.CharField(
        max_length=10, choices=[("DIRECT", "Direct"), ("AGENT", "Agent"), ("BROKER", "Broker")], default="DIRECT"
    )
    commission_pct = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True, help_text="Overrides the product default"
    )
    manager = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="team_members"
    )
    customer = models.ForeignKey(
        "motor.Customer", null=True, blank=True, on_delete=models.SET_NULL, related_name="portal_users"
    )
    is_demo = models.BooleanField(default=False, help_text="Created by the demo-data loader")

    def __str__(self):
        return self.user.get_username()


@receiver(post_save, sender=User)
def create_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


def user_role(user):
    """Resolve the user's primary role from Django groups (superusers are Admin)."""
    if not user.is_authenticated:
        return None
    if user.is_superuser:
        return "ADMIN"
    names = set(user.groups.values_list("name", flat=True))
    for code, label in ROLES:
        if label in names:
            return code
    return "AGENT"


class Company(models.Model):
    """Single-row table with the insurer's own details. Printed on every PDF, invoice and receipt."""

    name = models.CharField("Trade name", max_length=120, default="Your Insurance Company")
    legal_name = models.CharField(max_length=160, blank=True)
    cr_number = models.CharField("Commercial registration (CR)", max_length=30, blank=True)
    vat_number = models.CharField("VAT registration no. (TRN)", max_length=30, blank=True)
    fsa_license_no = models.CharField("FSA licence no.", max_length=40, blank=True)
    address = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=60, blank=True, default="Muscat")
    country = models.CharField(max_length=60, default="Sultanate of Oman")
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    website = models.CharField(max_length=120, blank=True)
    whatsapp_number = models.CharField(max_length=30, blank=True, help_text="Business WhatsApp shown on documents")
    default_phone_code = models.CharField(max_length=5, default="968", help_text="Prefix added to local mobile numbers (968 = Oman)")
    logo = models.FileField(upload_to="company/", blank=True, help_text="PNG or JPG, shown on documents and the sidebar")
    bank_name = models.CharField(max_length=80, blank=True)
    bank_account = models.CharField(max_length=40, blank=True)
    iban = models.CharField("IBAN", max_length=40, blank=True)
    swift = models.CharField("SWIFT / BIC", max_length=20, blank=True)
    invoice_footer = models.CharField(max_length=240, blank=True, default="Thank you for choosing us.")
    public_base_url = models.CharField(
        max_length=160, blank=True, help_text="e.g. https://motor.example.com: used in payment and document links sent to customers"
    )
    ollama_host = models.CharField("Ollama host", max_length=160, blank=True, help_text="Blank = OLLAMA_HOST setting (http://127.0.0.1:11434)")
    ollama_model = models.CharField("Default AI model", max_length=80, blank=True)

    class Meta:
        verbose_name_plural = "company"

    def __str__(self):
        return self.name

    @classmethod
    def get(cls):
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create(name=getattr(settings, "APP_NAME", "Insurance Company"))
        return obj

    @property
    def display_legal_name(self):
        return self.legal_name or self.name

    @property
    def address_line(self):
        return ", ".join(x for x in (self.address, self.city, self.country) if x)

    def save(self, *a, **kw):
        self.pk = self.pk or (Company.objects.values_list("pk", flat=True).first() or None)
        super().save(*a, **kw)

    def logo_path(self):
        try:
            return self.logo.path if self.logo else None
        except Exception:  # pragma: no cover
            return None


class FlexField(models.Model):
    """Admin-defined extra field. Values live in a JSON column (``flex``) on the owning record."""

    CUSTOMER, VEHICLE, POLICY, CLAIM = "CUSTOMER", "VEHICLE", "POLICY", "CLAIM"
    ENTITIES = [(CUSTOMER, "Customer"), (VEHICLE, "Vehicle"), (POLICY, "Quote / policy"), (CLAIM, "Claim")]
    TEXT, NUMBER, DATE, CHOICE, BOOL, LONGTEXT = "TEXT", "NUMBER", "DATE", "CHOICE", "BOOL", "LONGTEXT"
    TYPES = [(TEXT, "Text"), (LONGTEXT, "Long text"), (NUMBER, "Number"), (DATE, "Date"), (CHOICE, "Drop-down"), (BOOL, "Yes / No")]

    entity = models.CharField(max_length=10, choices=ENTITIES)
    key = models.SlugField(max_length=40, help_text="Internal name, letters/digits/underscore. Cannot be changed later.")
    label = models.CharField(max_length=60)
    field_type = models.CharField(max_length=9, choices=TYPES, default=TEXT)
    choices = models.TextField(blank=True, help_text="Drop-down options, one per line")
    required = models.BooleanField(default=False)
    help_text = models.CharField(max_length=120, blank=True)
    order = models.PositiveSmallIntegerField(default=10)
    active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("entity", "key")
        ordering = ["entity", "order", "label"]

    def __str__(self):
        return f"{self.get_entity_display()}: {self.label}"

    @property
    def choice_list(self):
        return [c.strip() for c in self.choices.splitlines() if c.strip()]


class DemoRun(models.Model):
    """Progress of a demo-data load / unload (the UI polls this while a background thread works)."""

    LOAD, UNLOAD = "LOAD", "UNLOAD"
    RUNNING, DONE, FAILED = "RUNNING", "DONE", "FAILED"
    action = models.CharField(max_length=6)
    status = models.CharField(max_length=7, default=RUNNING)
    stage = models.CharField(max_length=80, blank=True)
    done = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True)
    params = models.JSONField(default=dict, blank=True)
    started_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    @property
    def percent(self):
        return int(self.done * 100 / self.total) if self.total else 0

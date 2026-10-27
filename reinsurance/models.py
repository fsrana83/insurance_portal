from decimal import Decimal

from django.db import models


class Reinsurer(models.Model):
    name = models.CharField(max_length=80, unique=True)
    country = models.CharField(max_length=40, blank=True)
    rating = models.CharField(max_length=8, blank=True)

    def __str__(self):
        return self.name


class Treaty(models.Model):
    """A treaty *version*. Active treaties are immutable: 'Modify' creates a new version (addendum 2.2)."""

    QUOTA, SURPLUS, XOL = "QUOTA", "SURPLUS", "XOL"
    TYPES = [
        (QUOTA, "Quota Share"),
        (SURPLUS, "Surplus"),
        (XOL, "Excess of Loss (non-proportional)"),
    ]
    MOTOR, PNC = "MOTOR", "PNC"
    business = models.CharField(max_length=6, choices=[(MOTOR, "Motor"), (PNC, "Property & Casualty")], default=MOTOR, db_index=True)
    pnc_groups = models.CharField("P&C groups covered", max_length=80, blank=True, help_text="Comma separated: FIRE, MARINE, ENGINEERING, LIABILITY, MISC. Blank = all")
    reference = models.CharField(max_length=30, db_index=True, help_text="Stays the same across versions")
    version = models.PositiveSmallIntegerField(default=1)
    name = models.CharField(max_length=80)
    treaty_type = models.CharField(max_length=8, choices=TYPES)
    rank = models.PositiveSmallIntegerField(default=1, help_text="Order in the reinsurance program (1 = first)")
    covers = models.ManyToManyField("motor.Cover", blank=True, help_text="Empty = all covers")
    reinsurer = models.ForeignKey(Reinsurer, on_delete=models.PROTECT)
    effective_from = models.DateField()
    effective_to = models.DateField()
    # Quota share
    cession_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"), help_text="Quota share %")
    qs_limit = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"),
                                   help_text="Quota share limit: most the treaty pays on one claim (base currency). 0 = no limit")
    # Surplus
    retention = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Base currency")
    capacity = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0"), help_text="Max cession (base currency): lines x retention"
    )
    # XoL / MDP
    RISK, CAT, STOP_LOSS, AGGREGATE = "RISK", "CAT", "STOP_LOSS", "AGGREGATE"
    XOL_KINDS = [(RISK, "Per risk / per occurrence XoL"), (CAT, "Catastrophe XoL (per event)"), (STOP_LOSS, "Stop loss (loss ratio)"),
                 (AGGREGATE, "Aggregate XoL (annual deductible / limit)")]
    xol_kind = models.CharField(max_length=10, choices=XOL_KINDS, default=RISK)
    INURING = [("NET", "Inures: applies to the loss after proportional recoveries"), ("GROSS", "Does not inure: applies to the loss before proportional recoveries")]
    inuring = models.CharField("Inuring basis (P&C per-risk XoL)", max_length=5, choices=INURING, default="NET",
                               help_text="Contractual wording decides whether quota share / surplus reduce the loss presented to this XoL")
    aad = models.DecimalField("Annual aggregate deductible", max_digits=14, decimal_places=3, default=Decimal("0"))
    aal = models.DecimalField("Annual aggregate limit", max_digits=14, decimal_places=3, default=Decimal("0"))
    sl_attach_pct = models.DecimalField("Stop-loss attachment (loss ratio %)", max_digits=6, decimal_places=2, default=Decimal("0"))
    sl_limit_pct = models.DecimalField("Stop-loss limit (% of subject premium)", max_digits=6, decimal_places=2, default=Decimal("0"))
    reinstatements = models.PositiveSmallIntegerField(default=1, help_text="Number of paid reinstatements of the limit")
    reinstatement_pct = models.DecimalField("Reinstatement premium %", max_digits=6, decimal_places=2, default=Decimal("100"), help_text="% of the XoL premium, pro rata to the limit used")
    BASIS_RATE, BASIS_BC = "RATE", "BURNING_COST"
    premium_basis = models.CharField(max_length=12, choices=[(BASIS_RATE, "Fixed rate"), (BASIS_BC, "Burning cost")], default=BASIS_RATE)
    bc_years = models.PositiveSmallIntegerField("Burning cost years", default=5)
    bc_loading = models.DecimalField("Burning cost loading", max_digits=5, decimal_places=3, default=Decimal("1.250"), help_text="1.25 = loss cost / 0.80 (expenses, profit)")
    bc_min_rate = models.DecimalField("Minimum rate %", max_digits=6, decimal_places=3, default=Decimal("0"))
    bc_max_rate = models.DecimalField("Maximum rate %", max_digits=6, decimal_places=3, default=Decimal("0"))
    xol_retention = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    xol_limit = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"))
    est_gnrpi = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"), help_text="Estimated GNRPI")
    rate_pct = models.DecimalField(max_digits=6, decimal_places=3, default=Decimal("0"))
    deposit_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("90"))
    installments = models.PositiveSmallIntegerField(default=4, help_text="4 = quarterly, 2 = semi-annual")
    active = models.BooleanField(default=True)
    superseded_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="amends")
    amendment_note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rank", "reference", "-version"]
        unique_together = ("reference", "version")
        verbose_name_plural = "treaties"

    def __str__(self):
        return f"{self.name} v{self.version}"

    @property
    def mdp(self):
        return (self.est_gnrpi * self.rate_pct / 100 * self.deposit_pct / 100).quantize(Decimal("0.001"))

    @property
    def is_locked(self):
        return self.pk is not None and self.superseded_by_id is None and self.active


class MDPInstallment(models.Model):
    treaty = models.ForeignKey(Treaty, on_delete=models.CASCADE, related_name="mdp_installments")
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    paid = models.BooleanField(default=False)
    paid_on = models.DateField(null=True, blank=True)
    reference = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["due_date"]


class Cession(models.Model):
    """The reinsurance share calculated and stored for every policy issued (FRD 3.4)."""

    policy = models.ForeignKey("motor.Policy", on_delete=models.CASCADE, related_name="cessions")
    risk = models.ForeignKey("motor.PolicyRisk", null=True, blank=True, on_delete=models.CASCADE)
    treaty = models.ForeignKey(Treaty, null=True, blank=True, on_delete=models.PROTECT)
    treaty_type = models.CharField(max_length=8)
    rank = models.PositiveSmallIntegerField(default=0)
    sum_insured_ceded = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"), help_text="Base currency")
    premium_ceded = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"), help_text="Base currency")
    cession_pct = models.DecimalField(max_digits=7, decimal_places=3, default=Decimal("0"))
    is_facultative = models.BooleanField(default=False)
    fac_reinsurer = models.ForeignKey(Reinsurer, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    fac_confirmed = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.policy.policy_no} {self.treaty_type} {self.cession_pct}%"


class AggRecovery(models.Model):
    """A recovery that is worked out on a total, not claim by claim: cat XoL (per event), aggregate XoL and stop loss (per period)."""
    treaty = models.ForeignKey(Treaty, on_delete=models.PROTECT, related_name="agg_recoveries")
    event = models.ForeignKey("claims.CatEvent", null=True, blank=True, on_delete=models.CASCADE, related_name="agg_recoveries")
    kind = models.CharField(max_length=10)
    label = models.CharField(max_length=160)
    retained_loss = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    subject_premium = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    recovery = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    posted = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    received = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    reinstatement_premium = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"))
    computed_on = models.DateField(auto_now=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        unique_together = ("treaty", "event")
        ordering = ["-computed_on", "id"]

    @property
    def due(self):
        return self.posted - self.received

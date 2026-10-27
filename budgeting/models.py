from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models

from motor.models import Product

# Budgeted metrics. "good" says which direction is favourable (+1: higher is better, -1: lower is better).
METRICS = [
    ("GWP", "Gross written premium (net of VAT and fees)", +1),
    ("EARNED", "Insurance revenue (earned premium)", +1),
    ("CLAIMS", "Incurred claims", -1),
    ("COMM", "Acquisition cost (commission)", -1),
    ("POLICIES", "New policies and renewals issued (count)", +1),
]
METRIC_LABEL = {k: v for k, v, _ in METRICS}
METRIC_SHORT = {"GWP": "Written premium", "EARNED": "Earned premium", "CLAIMS": "Incurred claims", "COMM": "Commission", "POLICIES": "Policies issued",
                "LOSS": "Loss ratio %", "EXPENSE": "Expense ratio %", "COMBINED": "Combined ratio %"}
METRIC_GOOD = {k: g for k, _, g in METRICS} | {"LOSS": -1, "EXPENSE": -1, "COMBINED": -1}
RATIO_METRICS = ("LOSS", "EXPENSE", "COMBINED")
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class BudgetVersion(models.Model):
    BUDGET, FORECAST = "BUDGET", "FORECAST"
    KINDS = [(BUDGET, "Budget"), (FORECAST, "Forecast")]
    DRAFT, APPROVED, LOCKED = "DRAFT", "APPROVED", "LOCKED"
    STATUSES = [(DRAFT, "Draft"), (APPROVED, "Approved"), (LOCKED, "Locked")]

    name = models.CharField(max_length=80)
    year = models.PositiveSmallIntegerField(db_index=True)
    kind = models.CharField(max_length=8, choices=KINDS, default=BUDGET)
    status = models.CharField(max_length=8, choices=STATUSES, default=DRAFT)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children",
                               help_text="The version this one was rolled over / copied from")
    method = models.CharField(max_length=60, blank=True, help_text="How the figures were generated")
    cutoff = models.DateField(null=True, blank=True, help_text="Forecasts: actuals are used up to this date")
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_demo = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-year", "kind", "-id"]

    def __str__(self):
        return f"{self.name} ({self.year})"

    @property
    def editable(self):
        return self.status == self.DRAFT


class BudgetLine(models.Model):
    version = models.ForeignKey(BudgetVersion, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    metric = models.CharField(max_length=10)
    month = models.PositiveSmallIntegerField()
    amount = models.DecimalField(max_digits=16, decimal_places=3, default=Decimal("0"), help_text="Base currency (count for POLICIES)")

    class Meta:
        unique_together = [("version", "product", "metric", "month")]
        indexes = [models.Index(fields=["version", "metric"])]

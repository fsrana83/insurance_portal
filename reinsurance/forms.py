from datetime import date

from django import forms

from core.widgets import DateInput, MoneyInput
from motor.forms import StyledMixin
from motor.models import Cover

from .models import Reinsurer, Treaty


class TreatyForm(StyledMixin, forms.ModelForm):
    covers = forms.ModelMultipleChoiceField(Cover.objects.filter(kind__in=["OD", "TP"]), required=False,
                                            widget=forms.CheckboxSelectMultiple, help_text="Leave empty to apply to all covers")

    class Meta:
        model = Treaty
        fields = ["reference", "name", "business", "pnc_groups", "treaty_type", "rank", "reinsurer", "covers", "effective_from", "effective_to",
                  "cession_pct", "qs_limit", "retention", "capacity", "xol_kind", "xol_retention", "xol_limit", "aad", "aal", "sl_attach_pct", "sl_limit_pct",
                  "reinstatements", "reinstatement_pct", "est_gnrpi", "premium_basis", "bc_years", "bc_loading", "bc_min_rate", "bc_max_rate", "rate_pct",
                  "deposit_pct", "installments"]
        widgets = {"effective_from": DateInput(), "effective_to": DateInput()}

    def __init__(self, *a, modify=False, **kw):
        super().__init__(*a, **kw)
        self.fields["reference"].required = False
        if modify:
            self.fields["reference"].disabled = True
            self.fields["treaty_type"].disabled = True
            self.fields["business"].disabled = True
        self._style()

    def clean(self):
        d = super().clean()
        if d.get("effective_from") and d.get("effective_to") and d["effective_to"] <= d["effective_from"]:
            self.add_error("effective_to", "Must be after the effective-from date.")
        t = d.get("treaty_type")
        if t == Treaty.QUOTA and not d.get("cession_pct"):
            self.add_error("cession_pct", "Quota share % is required.")
        if t == Treaty.SURPLUS and not d.get("retention"):
            self.add_error("retention", "Retention is required for a surplus treaty.")
        if t == Treaty.XOL and not d.get("est_gnrpi"):
            self.add_error("est_gnrpi", "Estimated GNRPI is required for XoL.")
        if t == Treaty.XOL and d.get("premium_basis") != Treaty.BASIS_BC and not d.get("rate_pct"):
            self.add_error("rate_pct", "Enter the rate, or choose burning cost pricing.")
        if t == Treaty.XOL:
            kind = d.get("xol_kind")
            if kind in (Treaty.RISK, Treaty.CAT) and not d.get("xol_retention"):
                self.add_error("xol_retention", "Retention is required.")
            if kind == Treaty.AGGREGATE and not (d.get("aad") is not None and d.get("aal")):
                self.add_error("aal", "Annual aggregate deductible and limit are required.")
            if kind == Treaty.STOP_LOSS and not (d.get("sl_attach_pct") and d.get("sl_limit_pct")):
                self.add_error("sl_attach_pct", "Attachment and limit (% of subject premium) are required for stop loss.")
        return d


class AmendmentForm(StyledMixin, forms.Form):
    note = forms.CharField(max_length=200, label="Reason for amendment")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._style()


class MDPForm(StyledMixin, forms.Form):
    actual_gnrpi = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Actual GNRPI (OMR)")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._style()


class FacForm(StyledMixin, forms.Form):
    reinsurer = forms.ModelChoiceField(Reinsurer.objects.all())

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._style()

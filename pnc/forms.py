from datetime import timedelta
from decimal import Decimal

from django import forms

from core import clock
from core.models import Currency
from motor.models import Customer

from .models import PncClass, PncPolicy, PncRisk

ZERO = Decimal("0")
METHODS = [("", "-- how received --"), ("CASH", "Cash"), ("CHEQUE", "Cheque"), ("TRANSFER", "Bank transfer"), ("POS", "Card (POS)")]


class PolicyForm(forms.ModelForm):
    class Meta:
        model = PncPolicy
        fields = ["pnc_class", "customer", "currency", "start_date", "end_date", "interest", "conditions"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), "end_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "conditions": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["pnc_class"].queryset = PncClass.objects.filter(active=True)
        self.fields["currency"].queryset = Currency.objects.filter(active=True)
        self.fields["customer"].queryset = Customer.objects.order_by("name")
        today = clock.today()
        self.fields["start_date"].initial = today
        self.fields["end_date"].initial = today + timedelta(days=364)
        for f in self.fields.values():
            f.widget.attrs.setdefault("class", "input")

    def clean(self):
        d = super().clean()
        if d.get("start_date") and d.get("end_date") and d["end_date"] <= d["start_date"]:
            self.add_error("end_date", "The expiry date must be after the start date.")
        return d


def _extra(cls):
    out = {}
    for f in cls.fields or []:
        t = f.get("type", "text")
        kw = dict(label=f["label"], required=False)
        if t == "choice":
            out[f["key"]] = forms.ChoiceField(choices=[("", "--")] + [(c, c) for c in f.get("choices", [])], **kw)
        elif t == "number":
            out[f["key"]] = forms.DecimalField(**kw)
        elif t == "bool":
            out[f["key"]] = forms.BooleanField(**kw)
        else:
            out[f["key"]] = forms.CharField(**kw)
    return out


class RiskForm(forms.Form):
    description = forms.CharField(max_length=200)
    location = forms.CharField(max_length=200, required=False)
    sum_insured = forms.DecimalField(min_value=Decimal("0.001"), max_digits=16, decimal_places=3)
    value_at_risk = forms.DecimalField(required=False, min_value=ZERO, max_digits=16, decimal_places=3, help_text="Full value (for the average clause)")
    loading_pct = forms.DecimalField(required=False, max_digits=6, decimal_places=2, initial=0, label="Loading / (discount) %")
    loading_reason = forms.CharField(required=False, max_length=120)
    deductible = forms.DecimalField(required=False, min_value=ZERO, max_digits=14, decimal_places=3, help_text="0 = class default")

    def __init__(self, pnc_class, *a, **kw):
        super().__init__(*a, **kw)
        self.pnc_class = pnc_class
        self.fields["sum_insured"].label = pnc_class.si_label
        self.detail_keys = []
        for k, f in _extra(pnc_class).items():
            self.fields["d_" + k] = f
            self.detail_keys.append(k)
        for f in self.fields.values():
            if not isinstance(f.widget, forms.CheckboxInput):
                f.widget.attrs.setdefault("class", "input")

    def details(self):
        return {k: (str(v) if not isinstance(v, bool) else v) for k in self.detail_keys if (v := self.cleaned_data.get("d_" + k)) not in (None, "")}

    def save(self, policy):
        d = self.cleaned_data
        n = (policy.risks.order_by("-number").first().number if policy.risks.exists() else 0) + 1
        return PncRisk.objects.create(policy=policy, number=n, description=d["description"], location=d["location"], sum_insured=d["sum_insured"],
                                      value_at_risk=d["value_at_risk"] or ZERO, loading_pct=d["loading_pct"] or ZERO, loading_reason=d["loading_reason"],
                                      deductible=d["deductible"] or ZERO, details=self.details(), rate_pct=policy.pnc_class.rate_pct)

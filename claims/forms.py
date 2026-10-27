from datetime import date
from decimal import Decimal

from django import forms

import json

from core import flex
from core.models import FlexField
from core.widgets import DateInput, MoneyInput
from motor.forms import StyledMixin
from motor.models import Driver, Policy, PolicyRisk

from . import ai

from .models import CatEvent, Claim, ClaimPayment, Injury, LegalAction, Provider, Recovery, RepairEstimate, Subrogation

GOVERNORATES = ["Muscat", "Dhofar", "Musandam", "Al Buraimi", "Ad Dakhiliyah", "Al Batinah North", "Al Batinah South",
                "Ash Sharqiyah North", "Ash Sharqiyah South", "Ad Dhahirah", "Al Wusta"]


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *a, **kw):
        kw.setdefault("widget", MultipleFileInput(attrs={"multiple": True}))
        super().__init__(*a, **kw)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data if d]
        return [single(data, initial)] if data else []


class ClaimForm(StyledMixin, forms.Form):
    policy = forms.ModelChoiceField(Policy.objects.none(), label="Policy", widget=forms.HiddenInput)
    risk = forms.ModelChoiceField(PolicyRisk.objects.none(), label="Vehicle")
    date_of_loss = forms.DateField(widget=DateInput(), initial=date.today)
    loss_type = forms.ChoiceField(choices=Claim.LOSS_TYPES)
    cause_of_loss = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="What happened")
    location = forms.CharField(max_length=120, required=False)
    governorate = forms.ChoiceField(choices=[("", "—")] + [(g, g) for g in GOVERNORATES], required=False)
    police_report_no = forms.CharField(max_length=40, required=False, label="Royal Oman Police report no.")
    estimated_damage = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Estimated total cost (override)", required=False,
                                          help_text="Leave blank to use the automatic estimate calculated from the damage map, total loss value and injuries")
    is_total_loss = forms.BooleanField(required=False, label="Total loss (value is calculated at the depreciated market value)")
    damage_zones = forms.CharField(required=False, widget=forms.HiddenInput)
    damage_notes = forms.CharField(max_length=200, required=False, label="Damage notes for the estimator")
    ai_estimate_json = forms.CharField(required=False, widget=forms.HiddenInput)
    ai_model = forms.ChoiceField(required=False, label="AI model (local Ollama)", choices=[("", "No AI: statistical estimate only")])
    driver = forms.ModelChoiceField(Driver.objects.none(), required=False, label="Driver at the time of loss",
                                    empty_label="Not on the policy / undisclosed driver")
    driver_age = forms.IntegerField(min_value=16, max_value=100, required=False, label="Driver's age")
    young_driver = forms.BooleanField(required=False, label="Driver is a young / inexperienced driver")
    undisclosed_driver = forms.BooleanField(required=False, label="Driver was not disclosed on the policy")
    third_party_liable = forms.BooleanField(required=False, label="Another party may be liable (subrogation)")
    liable_party = forms.CharField(max_length=120, required=False, label="At-fault party")
    liable_insurer = forms.CharField(max_length=120, required=False, label="At-fault party's insurer")
    liable_insurer_email = forms.EmailField(required=False, label="Insurer e-mail (notice is sent on opening)")
    their_policy_no = forms.CharField(max_length=60, required=False, label="Their policy no.")
    their_vehicle = forms.CharField(max_length=80, required=False, label="Their vehicle / plate")
    their_claim_ref = forms.CharField(max_length=60, required=False, label="Their claim ref.")
    photos = MultipleFileField(required=False, label="Photos")
    police_report = MultipleFileField(required=False, label="Police report")

    def __init__(self, *a, policies=None, ollama_info=None, **kw):
        super().__init__(*a, **kw)
        pols = policies if policies is not None else Policy.objects.none()
        self.fields["policy"].queryset = pols.select_related("customer").order_by("-issued_at")
        self.fields["policy"].label_from_instance = lambda p: f"{p.policy_no} — {p.customer.name}"
        self.fields["risk"].queryset = PolicyRisk.objects.filter(policy__in=pols, active=True).select_related("policy")
        self.fields["risk"].label_from_instance = lambda r: f"{r.make} {r.model} · {r.plate or r.chassis_no} ({r.policy.policy_no})"
        self.fields["driver"].queryset = Driver.objects.filter(policy_risk__policy__in=pols)
        self.fields["driver"].label_from_instance = lambda d: f"{d.name} ({d.licence_no}), age {d.age}"
        info = ai.list_models() if ollama_info is None else ollama_info
        self.ollama = info
        from core.models import Company
        default = Company.get().ollama_model
        self.fields["ai_model"].choices = [("", "No AI: statistical estimate only")] + [(m["name"], f'{m["name"]}  ·  {m["parameters"] or m["size_gb"]}') for m in info["models"]]
        if default and not self.is_bound:
            self.fields["ai_model"].initial = default
        # render only the vehicles / drivers of the chosen policy; the page fills them in when a policy is picked
        chosen = self.data.get("policy") if self.is_bound else (self.initial.get("policy").pk if self.initial.get("policy") else None)
        if chosen:
            self.fields["risk"].widget.choices = [(r.pk, self.fields["risk"].label_from_instance(r)) for r in self.fields["risk"].queryset.filter(policy_id=chosen)]
            self.fields["driver"].widget.choices = [("", self.fields["driver"].empty_label)] + [
                (d.pk, self.fields["driver"].label_from_instance(d)) for d in self.fields["driver"].queryset.filter(policy_risk__policy_id=chosen)]
        else:
            self.fields["risk"].widget.choices = [("", "Select a policy first")]
            self.fields["driver"].widget.choices = [("", self.fields["driver"].empty_label)]
        flex.attach(self, FlexField.CLAIM, prefix="cflex_")
        self._style()

    def clean(self):
        d = super().clean()
        if d.get("policy") and d.get("risk") and d["risk"].policy_id != d["policy"].id:
            self.add_error("risk", "This vehicle is not on the selected policy.")
        if d.get("date_of_loss") and d["date_of_loss"] > date.today():
            self.add_error("date_of_loss", "Date of loss cannot be in the future.")
        if d.get("driver") and d.get("risk") and d["driver"].policy_risk_id != d["risk"].id:
            self.add_error("driver", "This driver is not registered on the selected vehicle.")
        try:
            has_zones = bool(ai.clean_zones(json.loads(d.get("damage_zones") or "{}")))
        except ValueError:
            has_zones = False
        if not d.get("is_total_loss") and d.get("estimated_damage") in (None, "") and not has_zones and d.get("loss_type") != "TP_INJURY":
            self.add_error("estimated_damage", "Mark the damage on the vehicle drawing, or enter an estimated cost, or mark a total loss.")
        try:
            d["zones"] = ai.clean_zones(json.loads(d.get("damage_zones") or "{}"))
        except ValueError:
            d["zones"] = {}
        try:
            d["ai"] = json.loads(d.get("ai_estimate_json") or "{}")
        except ValueError:
            d["ai"] = {}
        return d


class _Base(StyledMixin, forms.Form):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._style()


class EstimateForm(_Base):
    garage = forms.ModelChoiceField(Provider.objects.filter(provider_type="GARAGE", active=True))
    pricing_basis = forms.ChoiceField(choices=RepairEstimate.BASES, initial=RepairEstimate.LABOUR_PARTS, label="Quoted as")
    labour_amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Labour amount", required=False)
    parts_cost = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Parts amount", required=False)
    fixed_amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Fixed amount as quoted", required=False)
    estimated_days = forms.IntegerField(min_value=1, max_value=120, initial=7)
    supplementary = forms.BooleanField(required=False, label="Supplementary estimate (adds to this garage's earlier estimate)")

    def clean(self):
        d = super().clean()
        need = {"LABOUR": ["labour_amount"], "PARTS": ["parts_cost"], "BOTH": ["labour_amount", "parts_cost"], "FIXED": ["fixed_amount"]}.get(d.get("pricing_basis"), [])
        for f_ in need:
            if d.get(f_) in (None, ""):
                self.add_error(f_, "Required for this pricing basis.")
        for f_ in ("labour_amount", "parts_cost", "fixed_amount"):
            if f_ not in need:
                d[f_] = Decimal("0")
        return d


class ReserveForm(_Base):
    reserve = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Reserve")


class ApproveForm(_Base):
    approved_cost = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Approved repair cost", required=False,
                                       help_text="Leave blank on a total loss to use the depreciated market value")
    total_loss = forms.BooleanField(required=False, label="Total loss (salvage applies)")

    def clean(self):
        d = super().clean()
        if not d.get("total_loss") and not d.get("approved_cost"):
            self.add_error("approved_cost", "Enter the approved repair cost.")
        return d


class PayForm(_Base):
    stage = forms.ChoiceField(choices=ClaimPayment.STAGES)
    payee = forms.CharField(max_length=100)
    reference = forms.CharField(max_length=40, required=False)


class RepairForm(_Base):
    completed_on = forms.DateField(widget=DateInput(), initial=date.today, label="Repair completed on")
    final_bill = forms.DecimalField(min_value=0, decimal_places=3, required=False, widget=MoneyInput, label="Final garage bill")
    rework = forms.BooleanField(required=False, label="Faulty repair / re-work needed")
    customer_score = forms.IntegerField(min_value=1, max_value=5, required=False, label="Customer rating (1–5)")


class ReopenForm(_Base):
    reason = forms.CharField(max_length=200, label="Reason for reopening")


class CatFlagForm(_Base):
    cat_event = forms.ModelChoiceField(CatEvent.objects.all(), required=False, label="Catastrophe event", empty_label="Not a catastrophe claim")
    new_name = forms.CharField(max_length=120, required=False, label="Or start a new event: name")
    new_type = forms.ChoiceField(choices=CatEvent.TYPES, required=False, label="Event type")
    new_date = forms.DateField(required=False, widget=DateInput(), label="Event date")

    def clean(self):
        d = super().clean()
        if d.get("new_name") and not d.get("new_date"):
            self.add_error("new_date", "Enter the date of the event.")
        return d


class CloseForm(_Base):
    confirm_reversal = forms.BooleanField(required=False, label="I confirm the estimate is reversed to the invoiced amount")


class InvoiceForm(_Base):
    estimate = forms.ModelChoiceField(RepairEstimate.objects.none(), label="Estimate")
    invoice_no = forms.CharField(max_length=40, label="Garage invoice no.")
    invoice_amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Invoice amount")
    invoice_date = forms.DateField(widget=DateInput(), initial=date.today)

    def __init__(self, claim=None, *a, **kw):
        super().__init__(*a, **kw)
        if claim is not None:
            self.fields["estimate"].queryset = claim.estimates.filter(superseded=False)
            self.fields["estimate"].label_from_instance = lambda e: f"{e.garage.name} · {e.get_pricing_basis_display()} · {e.total}"


class SettleEstimateForm(_Base):
    estimate = forms.ModelChoiceField(RepairEstimate.objects.none(), label="Estimate / invoice")

    def __init__(self, claim=None, *a, **kw):
        super().__init__(*a, **kw)
        if claim is not None:
            self.fields["estimate"].queryset = claim.estimates.filter(superseded=False, settled=False)
            self.fields["estimate"].label_from_instance = lambda e: f"{e.garage.name} · invoice {e.invoice_no or 'missing'} · {e.invoice_amount or e.total}"


class ExcessForm(_Base):
    amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Excess collected", required=False)
    collected_on = forms.DateField(widget=DateInput(), initial=date.today, required=False)
    waiver_reason = forms.CharField(max_length=200, required=False, label="Or: reason the excess was waived")


class LegalFlagForm(_Base):
    legal = forms.BooleanField(required=False, label="This claim is a legal matter")


class LegalActionForm(_Base):
    action_type = forms.ChoiceField(choices=LegalAction.TYPES)
    action_date = forms.DateField(widget=DateInput(), initial=date.today)
    court = forms.CharField(max_length=100, required=False)
    case_no = forms.CharField(max_length=50, required=False, label="Case no.")
    counsel = forms.ModelChoiceField(Provider.objects.filter(provider_type="LEGAL", active=True), required=False)
    amount_claimed = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, required=False)
    legal_cost = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, required=False, label="Legal cost")
    outcome = forms.CharField(max_length=200, required=False)
    next_date = forms.DateField(widget=DateInput(), required=False, label="Next date / hearing")
    note = forms.CharField(max_length=300, required=False)


class InjuryForm(_Base):
    person_name = forms.CharField(max_length=100)
    role = forms.ChoiceField(choices=Injury.ROLES)
    age = forms.IntegerField(min_value=0, max_value=120, required=False)
    severity = forms.ChoiceField(choices=Injury.SEVERITIES)
    injury_description = forms.CharField(max_length=200, required=False, label="Injury / cause of death")
    hospital = forms.ModelChoiceField(Provider.objects.filter(provider_type="HOSPITAL", active=True), required=False)
    date_of_death = forms.DateField(widget=DateInput(), required=False)
    compensation_reserve = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, required=False, label="Compensation / blood money reserve",
                                              help_text="Leave blank to use the indicative tariff")
    medical_cost = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, required=False)
    note = forms.CharField(max_length=200, required=False)

    def clean(self):
        d = super().clean()
        if d.get("severity") == "DEATH" and not d.get("date_of_death"):
            d["date_of_death"] = None
        return d


class RecoveryForm(_Base):
    recovery_type = forms.ChoiceField(choices=[(Recovery.SALVAGE, "Salvage"), (Recovery.REINSURANCE, "Reinsurance"), (Recovery.OVERPAYMENT, "Overpayment refund")])
    amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput)
    note = forms.CharField(max_length=150, required=False)


class SubrogationForm(_Base):
    liable_party = forms.CharField(max_length=120)
    liable_insurer = forms.CharField(max_length=120, required=False)
    recovery_status = forms.ChoiceField(choices=Subrogation.STATUSES)
    legal_counsel = forms.ModelChoiceField(Provider.objects.filter(provider_type="LEGAL"), required=False)
    note = forms.CharField(max_length=200, required=False)


class SubroRecoverForm(_Base):
    amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Amount recovered")


class SurveyorForm(_Base):
    surveyor = forms.ModelChoiceField(Provider.objects.filter(provider_type="SURVEYOR", active=True))
    report_received = forms.BooleanField(required=False, label="Report received today")


class GuaranteeForm(_Base):
    hospital = forms.ModelChoiceField(Provider.objects.filter(provider_type="HOSPITAL", active=True))
    patient_name = forms.CharField(max_length=100)
    guaranteed_amount = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput)


class MedicalBillForm(_Base):
    guarantee = forms.ModelChoiceField(None)
    description = forms.CharField(max_length=120, label="Item (e.g. Private room)")
    days = forms.IntegerField(min_value=1, initial=1)
    rate_charged = forms.DecimalField(min_value=0, decimal_places=3, widget=MoneyInput, label="Rate charged / day")

    def __init__(self, claim, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["guarantee"].queryset = claim.guarantees.all()


class ProviderForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Provider
        fields = ["provider_type", "name", "governorate", "phone", "preferred",
                  "specialities", "room_cap_per_day", "active"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._style()

from django import forms


class IconRadioSelect(forms.RadioSelect):
    """Interactive icon cards (vehicle type, usage). Renders real radio inputs, so it works without JS."""

    template_name = "widgets/icon_radio.html"
    option_template_name = "widgets/icon_radio_option.html"

    def __init__(self, *args, kind="vehicle", **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind

    def get_context(self, name, value, attrs):
        ctx = super().get_context(name, value, attrs)
        ctx["widget"]["kind"] = self.kind
        return ctx


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kw):
        kw.setdefault("format", "%Y-%m-%d")
        super().__init__(**kw)


class MoneyInput(forms.NumberInput):
    """Number input with a live currency-symbol prefix (updated by app.js when the currency changes)."""

    template_name = "widgets/money_input.html"

    def __init__(self, attrs=None):
        super().__init__(attrs={"step": "0.001", "min": "0", **(attrs or {})})


class ProductCardSelect(forms.RadioSelect):
    template_name = "widgets/choice_cards.html"
    option_template_name = "widgets/choice_card_option.html"
    descriptions = {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opt = super().create_option(name, value, label, selected, index, subindex, attrs)
        key = str(getattr(value, "value", value))
        opt["description"] = self.descriptions.get(key, "")
        return opt


class RiderCheckboxes(forms.CheckboxSelectMultiple):
    """Rider chips. ``meta`` maps cover id -> {mandatory_for: "1,2", regulatory: bool, hint: str}."""

    template_name = "widgets/rider_checks.html"
    option_template_name = "widgets/rider_check_option.html"
    meta = {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        opt = super().create_option(name, value, label, selected, index, subindex, attrs)
        m = self.meta.get(str(getattr(value, "value", value)), {})
        opt["mandatory_for"] = m.get("mandatory_for", "")
        opt["regulatory"] = "1" if m.get("regulatory") else ""
        opt["hint"] = m.get("hint", "")
        return opt

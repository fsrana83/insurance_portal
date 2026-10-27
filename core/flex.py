"""Flex fields: admin-defined extra fields that appear on forms and are stored in a JSON column.

Usage in a form's ``__init__`` (before ``self._style()``)::

    flex.attach(self, FlexField.CUSTOMER, initial=instance.flex)

and when saving::

    instance.flex = flex.extract(form, FlexField.CUSTOMER)

``form.flex_bound`` is the list of bound fields for the template (``includes/flex.html``).
"""
from django import forms

from core.widgets import DateInput

from .models import FlexField

PREFIX = "flex_"


def definitions(entity):
    return list(FlexField.objects.filter(entity=entity, active=True))


def _field(d):
    kw = dict(label=d.label, required=d.required, help_text=d.help_text)
    t = d.field_type
    if t == FlexField.NUMBER:
        return forms.DecimalField(widget=forms.NumberInput(attrs={"step": "any"}), **kw)
    if t == FlexField.DATE:
        return forms.DateField(widget=DateInput(), **kw)
    if t == FlexField.BOOL:
        kw["required"] = False
        return forms.BooleanField(**kw)
    if t == FlexField.CHOICE:
        return forms.ChoiceField(choices=[("", "—")] + [(c, c) for c in d.choice_list], **kw)
    if t == FlexField.LONGTEXT:
        return forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), max_length=500, **kw)
    return forms.CharField(max_length=200, **kw)


def attach(form, entity, initial=None, prefix=PREFIX):
    """Add the active flex fields for ``entity`` to ``form``."""
    initial = initial or {}
    names = []
    for d in definitions(entity):
        name = prefix + d.key
        f = _field(d)
        val = initial.get(d.key)
        if val not in (None, ""):
            f.initial = val
        form.fields[name] = f
        names.append((d.key, name))
    if not hasattr(form, "_flex_names"):
        form._flex_names = []
    form._flex_names += [(entity, key, name) for key, name in names]
    bound = [form[n] for _, n in names]
    setattr(form, f"flex_{entity.lower()}", bound)
    form.flex_bound = bound
    return bound


def extract(form, entity=None):
    """JSON-safe dict of the flex values from a validated form."""
    out = {}
    for ent, key, name in getattr(form, "_flex_names", []):
        if entity is not None and ent != entity:
            continue
        v = form.cleaned_data.get(name)
        if v in (None, ""):
            continue
        out[key] = v.isoformat() if hasattr(v, "isoformat") else (str(v) if not isinstance(v, (bool, str)) else v)
    return out


def display(entity, data):
    """[(label, value)] for showing stored flex values (includes inactive definitions that still hold data)."""
    data = data or {}
    rows = []
    for d in FlexField.objects.filter(entity=entity):
        v = data.get(d.key)
        if v in (None, ""):
            continue
        rows.append((d.label, "Yes" if v is True else "No" if v is False else v))
    return rows

"""Applying a policy endorsement from stored form data (used after a second person confirms it)."""
from django.http import QueryDict

from . import services
from .forms import EndorsementForm
from .models import Policy


def to_post(payload):
    q = QueryDict(mutable=True)
    for k, v in payload.items():
        q.setlist(k, v if isinstance(v, list) else [v])
    return q


def apply(pending):
    p = Policy.objects.get(pk=pending.object_id)
    form = EndorsementForm(p, to_post(pending.payload))
    if not form.is_valid():
        raise ValueError("; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))
    d = form.cleaned_data
    try:
        endo = services.apply_endorsement(p, d["endorsement_type"], d["effective_date"], pending.requested_by, d.get("risk"), **form.service_kwargs())
    except services.ValidationFailed as e:
        raise ValueError("; ".join(e.args[0]))
    return [("success", f"Endorsement {endo.number} applied (version {endo.version_after}).")]

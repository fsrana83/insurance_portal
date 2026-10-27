"""Customer lookup: partial name, civil ID / CR, mobile - one result per real customer."""
import re

from django.db.models import Count, Q, Value
from django.db.models.functions import Replace

from .models import Customer


def _digits(s):
    return re.sub(r"\D", "", s or "")


def identity_key(c):
    """Two records with the same ID / CR (or, failing that, the same name and mobile) are the same customer."""
    if c.civil_id:
        return ("id", c.civil_id.strip())
    if c.cr_number:
        return ("cr", c.cr_number.strip().upper())
    return ("nm", " ".join(c.name.lower().split()), _digits(c.phone))


def search(q, limit=15, unique=True):
    """Customers matching every word of ``q`` in the name, or the ID / CR number, or the mobile number."""
    q = " ".join((q or "").split())
    qs = Customer.objects.annotate(phone_digits=Replace(Replace(Replace(Replace("phone", Value(" "), Value("")), Value("-"), Value("")), Value("+"), Value("")), Value("("), Value("")),
                                   n_policies=Count("policies", distinct=True), n_vehicles=Count("vehicles", distinct=True))
    if q:
        for tok in q.split():
            cond = Q(name__icontains=tok) | Q(civil_id__icontains=tok) | Q(cr_number__icontains=tok) | Q(phone__icontains=tok)
            d = _digits(tok)
            if len(d) >= 3:
                cond |= Q(phone_digits__icontains=d)
            qs = qs.filter(cond)
    # best matches first: exact ID / mobile, then names starting with the text
    ranked = sorted(qs[: max(limit * 6, 120)], key=lambda c: (
        0 if q and (c.civil_id == q or c.cr_number.upper() == q.upper() or (len(_digits(q)) >= 6 and c.phone_digits.endswith(_digits(q)))) else
        1 if q and c.name.lower().startswith(q.lower()) else 2, c.name.lower(), -c.n_policies))
    if not unique:
        return ranked[:limit]
    seen, out = {}, []
    for c in ranked:
        k = identity_key(c)
        if k in seen:
            if c.n_policies > seen[k].n_policies:        # keep the record that carries the history
                out[out.index(seen[k])] = c
                seen[k] = c
            continue
        seen[k] = c
        out.append(c)
    return out[:limit]


def find_existing(civil_id="", cr_number="", exclude=None):
    """An existing customer with this civil ID or CR number (used to stop duplicates being created)."""
    cond = Q(pk__in=[])
    if civil_id:
        cond |= Q(civil_id=civil_id.strip())
    if cr_number:
        cond |= Q(cr_number__iexact=cr_number.strip())
    qs = Customer.objects.filter(cond)
    if exclude is not None and exclude.pk:
        qs = qs.exclude(pk=exclude.pk)
    return qs.first()

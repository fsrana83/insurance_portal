"""Vehicle profile: one record per vehicle for life, ownership history, search and transfer."""
from datetime import date

from django.db import transaction
from django.db.models import Q

from core import clock

from .models import Policy, Vehicle, VehicleOwnership


class TransferError(Exception):
    pass


def open_ownership(vehicle, customer, from_date=None, source="First insured", user=None, note=""):
    return VehicleOwnership.objects.create(vehicle=vehicle, customer=customer, from_date=from_date or clock.today(), source=source, note=note, created_by=user)


def in_force_policies(vehicle, exclude_policy=None):
    qs = Policy.objects.filter(risks__vehicle=vehicle, risks__active=True, status__in=["ACTIVE", "PENDING_FAC"], end_date__gte=clock.today())
    if exclude_policy:
        qs = qs.exclude(pk=exclude_policy.pk)
    return qs.distinct()


@transaction.atomic
def transfer_vehicle(vehicle, new_customer, effective=None, user=None, source="Transfer of ownership", note=""):
    """Move the vehicle to a new owner. The vehicle record, chassis identity and all history stay the same."""
    effective = effective or clock.today()
    if vehicle.customer_id == new_customer.pk:
        return None
    cur = vehicle.ownerships.filter(to_date__isnull=True).first()
    if cur:
        cur.to_date = max(effective, cur.from_date)
        cur.save(update_fields=["to_date"])
    own = open_ownership(vehicle, new_customer, effective, source, user, note)
    vehicle.customer = new_customer
    vehicle.save(update_fields=["customer"])
    return own


def transfer_directly(vehicle, new_customer, effective, user, note=""):
    """Record a sale / transfer made outside a policy. Not allowed while a policy is in force: use the endorsement."""
    live = in_force_policies(vehicle).first()
    if live:
        raise TransferError(f"The vehicle is insured under policy {live.policy_no}. Use the 'Transfer of ownership' endorsement on that policy "
                            "so the cover moves to the new owner.")
    if vehicle.customer_id == new_customer.pk:
        raise TransferError("The vehicle already belongs to this customer.")
    return transfer_vehicle(vehicle, new_customer, effective, user, "Direct transfer (no policy in force)", note)


def search(q, limit=60):
    """Vehicle-level search: chassis, plate, engine, make / model (words in any order), owner name / ID / mobile, policy no."""
    q = (q or "").strip()
    qs = Vehicle.objects.select_related("customer")
    if not q:
        return qs.order_by("-id")[:limit]
    for tok in q.split():
        t = tok.upper()
        qs = qs.filter(Q(chassis_no__icontains=t) | Q(plate_number__icontains=t) | Q(engine_no__icontains=t) | Q(make__icontains=tok) | Q(model__icontains=tok)
                       | Q(color__icontains=tok) | Q(year__iexact=tok if tok.isdigit() else -1) | Q(customer__name__icontains=tok)
                       | Q(customer__civil_id__icontains=tok) | Q(customer__cr_number__icontains=tok) | Q(customer__phone__icontains=tok)
                       | Q(ownerships__customer__name__icontains=tok) | Q(risks_on_policies__policy__policy_no__icontains=tok)
                       | Q(plate_code__iexact=tok))
    return qs.distinct()[:limit]

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import user_role

PRODUCERS = {"AGENT", "BROKER"}
STAFF = {"ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE", "CREDIT_CONTROL"}
SEES_ALL_BUSINESS = {"ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_HANDLER", "CLAIMS_MANAGER"}
CAN_QUOTE = {"ADMIN", "AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER"}
CAN_CLAIM = {"ADMIN", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "CUSTOMER"}


def role_required(*roles):
    """Admin is always allowed. Usage: @role_required("AGENT", "BROKER")."""

    def deco(view):
        @wraps(view)
        @login_required
        def wrapped(request, *a, **kw):
            r = user_role(request.user)
            if r != "ADMIN" and r not in roles:
                raise PermissionDenied
            return view(request, *a, **kw)

        return wrapped

    return deco


def scope(user, qs, producer_field="producer"):
    """Producers see their own business; customers see their own policies; staff see everything."""
    r = user_role(user)
    if r in SEES_ALL_BUSINESS:
        return qs
    if r in PRODUCERS:
        return qs.filter(**{producer_field: user})
    if r == "CUSTOMER":
        cust = getattr(user.profile, "customer", None)
        return qs.filter(customer=cust) if cust else qs.none()
    return qs.none()

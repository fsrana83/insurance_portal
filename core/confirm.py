"""Maker-checker (dual confirmation) for changes to policies and claims.

The maker's change is stored as a pending action. A different person with authority over the same area confirms it; only then is it
applied, as the maker, with the checker recorded on every field change in the change log."""
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import audit
from .models import DocumentSequence, PendingAction, user_role

CHECKERS = {"claim": {"ADMIN", "CLAIMS_HANDLER", "CLAIMS_MANAGER"}, "policy": {"ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER"},
            "pnc": {"ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_MANAGER"}}


def enabled():
    return getattr(settings, "DUAL_CONFIRMATION", True)


def submit(user, kind, obj_id, ref, action, summary, payload, reason=""):
    payload = {k: v for k, v in payload.items() if k not in ("csrfmiddlewaretoken",)}
    return PendingAction.objects.create(number=f"PA-{DocumentSequence.next('PENDING'):06d}", kind=kind, object_id=obj_id, ref=ref, action=action,
                                        summary=summary[:250], payload=payload, reason=reason[:200], requested_by=user)


def can_review(user, pending):
    return pending.status == PendingAction.PENDING and user.pk != pending.requested_by_id and user_role(user) in CHECKERS.get(pending.kind, set())


def _execute(pending):
    if pending.kind == "claim":
        from claims import actions
        from claims.models import Claim
        claim = Claim.objects.get(pk=pending.object_id)
        return actions.apply(claim, pending.action, actions.to_post(pending.payload), pending.requested_by)
    if pending.kind == "policy":
        from motor import endorse_exec
        return endorse_exec.apply(pending)
    if pending.kind == "pnc":
        from pnc import actions
        return actions.apply(pending)
    raise ValueError(pending.kind)


def confirm(pending, checker):
    """Apply the pending change. Returns (ok, [(level, text)])."""
    if not can_review(checker, pending):
        raise PermissionError("You cannot confirm this change: it needs a different person with authority.")
    try:
        with transaction.atomic(), audit.acting(pending.requested_by, confirmed_by=checker, reason=pending.reason or pending.summary):
            msgs = _execute(pending)
    except Exception as e:                   # any failure leaves the data untouched and the request marked failed
        pending.status, pending.result = PendingAction.FAILED, str(e.args[0] if getattr(e, "args", None) and not isinstance(e.args[0], (list, tuple)) else e)[:300]
        pending.reviewed_by, pending.reviewed_at = checker, timezone.now()
        pending.save()
        return False, [("error", pending.result)]
    pending.status, pending.result = PendingAction.DONE, "; ".join(t for _, t in msgs)[:300]
    pending.reviewed_by, pending.reviewed_at = checker, timezone.now()
    pending.save()
    return True, msgs


def reject(pending, checker, note=""):
    if not can_review(checker, pending):
        raise PermissionError("You cannot review this change.")
    pending.status, pending.result = PendingAction.REJECTED, note[:300]
    pending.reviewed_by, pending.reviewed_at = checker, timezone.now()
    pending.save()

"""Changes to issued P&C policies and to claims that need a second person (maker-checker)."""
from datetime import date

from core import confirm

from . import services as sv
from .models import PncClaim, PncPolicy, PncRisk

CLAIM_ACTIONS = {"reject": "Reject claim", "close": "Close claim", "reopen": "Reopen claim"}


def request_or_apply(user, kind, obj, action, summary, payload, reason=""):
    """Returns (applied, message). With dual confirmation on, the change is queued for a second person."""
    ref = obj.claim_no if kind == "claim" else obj.number
    if confirm.enabled():
        confirm.submit(user, "pnc", obj.pk, ref, f"{kind}:{action}", summary, payload, reason)
        return False, f"Sent for confirmation by a second person: {summary}."
    run(kind, obj, action, payload, user)
    return True, f"{summary}: done."


def run(kind, obj, action, p, user):
    if kind == "claim":
        if action == "reject":
            sv.reject_claim(obj, user, p.get("reason", ""))
        elif action == "close":
            sv.close(obj, user)
        elif action == "reopen":
            sv.reopen(obj, user, p.get("reason", ""))
        else:
            raise sv.PncError("Unknown claim action.")
    else:
        risk = PncRisk.objects.filter(pk=p.get("risk")).first() if p.get("risk") else None
        eff = date.fromisoformat(p["effective"])
        new_end = date.fromisoformat(p["new_end"]) if p.get("new_end") else None
        rd = None
        if action == "ADD_RISK":
            rd = dict(description=p["description"], location=p.get("location", ""), sum_insured=p["sum_insured"], value_at_risk=p.get("value_at_risk") or 0,
                      loading_pct=p.get("loading_pct") or 0)
        sv.endorse(obj, user, action, eff, p.get("description", ""), risk=risk, new_si=p.get("new_si") or None, new_end=new_end, risk_data=rd,
                   conditions=p.get("conditions"))
    return [("success", "Applied")]


def apply(pending):
    kind, action = pending.action.split(":", 1)
    obj = PncClaim.objects.get(pk=pending.object_id) if kind == "claim" else PncPolicy.objects.get(pk=pending.object_id)
    return run(kind, obj, action, pending.payload, pending.requested_by)

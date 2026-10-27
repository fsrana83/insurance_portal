"""Audit trail: logs every create/update/delete with user id and timestamp (FRD 9.4)."""
import json
import threading
from contextlib import contextmanager

from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models.signals import post_delete, post_save, pre_save
from django.forms.models import model_to_dict

_state = threading.local()


@contextmanager
def suspended():
    """Skip audit logging (bulk demo load / unload write one summary entry instead)."""
    old = getattr(_state, "off", False)
    _state.off = True
    try:
        yield
    finally:
        _state.off = old


def current_user():
    return getattr(_state, "acting", None) or getattr(_state, "user", None)


@contextmanager
def acting(user, confirmed_by=None, reason=""):
    """Run code as ``user`` (the maker) with the second person (checker) recorded on every change it makes."""
    old = (getattr(_state, "acting", None), getattr(_state, "confirmed_by", None), getattr(_state, "reason", ""))
    _state.acting, _state.confirmed_by, _state.reason = user, confirmed_by, reason
    try:
        yield
    finally:
        _state.acting, _state.confirmed_by, _state.reason = old


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _state.user = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
        try:
            return self.get_response(request)
        finally:
            _state.user = None


def _snapshot(instance):
    try:
        data = model_to_dict(instance)
        return json.loads(json.dumps(data, cls=DjangoJSONEncoder, default=str))
    except Exception:  # pragma: no cover - never block a save on audit failure
        return {}


def _log(action, instance):
    from core.models import AuditLog

    if getattr(_state, "off", False):
        return

    AuditLog.objects.create(
        user=current_user(),
        action=action,
        model=instance._meta.label,
        object_id=str(instance.pk),
        object_repr=str(instance)[:200],
        data=_snapshot(instance) if action != "delete" else {},
    )


TRACKED = {"motor.Policy", "motor.PolicyRisk", "motor.Driver", "motor.Endorsement", "claims.Claim", "claims.ClaimPayment", "claims.RepairEstimate",
           "claims.Recovery", "claims.ClaimHead", "claims.PaymentRelease", "claims.Subrogation", "claims.LegalAction", "claims.Injury", "claims.SubroNotice", "claims.LetterOfGuarantee",
           "pnc.PncPolicy", "pnc.PncRisk", "pnc.PncEndorsement", "pnc.PncClaim", "pnc.PncClaimPayment", "pnc.PncRecovery", "pnc.PncMember", "pnc.PncPreauth", "pnc.PncDeclaration", "pnc.PncReserveChange", "pnc.PncRiRecovery"}
_SKIP_FIELDS = {"flex", "ai_estimate", "auto_estimate_detail", "created_at", "excess_breakdown"}


def _ref(instance):
    lab = instance._meta.label
    if lab == "motor.Policy":
        return instance.policy_no
    if lab == "claims.Claim":
        return instance.claim_no
    if lab == "pnc.PncPolicy":
        return instance.policy_no or instance.quote_no or ""
    if lab == "pnc.PncClaim":
        return instance.claim_no
    names = {f.name for f in instance._meta.get_fields()}
    for attr in ("claim", "policy"):
        if attr in names:
            obj = getattr(instance, attr, None)
            if obj is not None:
                return getattr(obj, "claim_no", None) or getattr(obj, "policy_no", None) or getattr(obj, "quote_no", "") or ""
    if "policy_risk" in names:
        try:
            return instance.policy_risk.policy.policy_no
        except Exception:
            return ""
    return ""


def _fields(instance):
    return [f for f in instance._meta.concrete_fields if f.name not in _SKIP_FIELDS and not f.primary_key]


def _norm(f, v):
    from decimal import Decimal, InvalidOperation
    if v is not None and f.get_internal_type() == "DecimalField":
        try:
            return str(Decimal(str(v)).quantize(Decimal(1).scaleb(-f.decimal_places)))
        except (InvalidOperation, ValueError):
            pass
    return "" if v is None else str(v)


def _before_save(sender, instance, **kwargs):
    if getattr(_state, "off", False) or kwargs.get("raw") or sender._meta.label not in TRACKED:
        return
    old = None
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values().first()
    instance._cl_old = old


def _changelog(action, instance, old=None):
    from core.models import ChangeLog

    rows = []
    if action == "update" and old:
        for f in _fields(instance):
            a, b = _norm(f, old.get(f.attname)), _norm(f, getattr(instance, f.attname))
            if a != b:
                rows.append((f.name, a, b))
        if not rows:
            return
    elif action == "create":
        rows = [(f.name, "", _norm(f, getattr(instance, f.attname))) for f in _fields(instance) if getattr(instance, f.attname) not in (None, "", 0, False)
                and str(getattr(instance, f.attname)) not in ("0", "0.000")]
    else:
        rows = [("", "", "")]
    ref = _ref(instance)
    for fld, a, b in rows:
        ChangeLog.objects.create(user=current_user(), confirmed_by=getattr(_state, "confirmed_by", None), action=action, model=instance._meta.label,
                                 object_id=str(instance.pk), object_repr=str(instance)[:200], ref=ref, field=fld, old_value=a[:2000], new_value=b[:2000],
                                 reason=getattr(_state, "reason", "")[:200])


def _on_save(sender, instance, created, **kwargs):
    if kwargs.get("raw"):
        return
    _log("create" if created else "update", instance)
    if sender._meta.label in TRACKED and not getattr(_state, "off", False):
        _changelog("create" if created else "update", instance, getattr(instance, "_cl_old", None))


def _on_delete(sender, instance, **kwargs):
    _log("delete", instance)
    if sender._meta.label in TRACKED and not getattr(_state, "off", False):
        _changelog("delete", instance)


def connect_audit():
    audited_apps = {"motor", "claims", "reinsurance", "payments", "accounting", "pnc"}
    for model in apps.get_models():
        if model._meta.label in {"accounting.JournalLine", "accounting.JournalEntry"}:
            continue
        if model._meta.app_label in audited_apps or model._meta.label in {"core.Currency", "core.Company", "core.FlexField"}:
            pre_save.connect(_before_save, sender=model, dispatch_uid=f"audit-pre-{model._meta.label}")
            post_save.connect(_on_save, sender=model, dispatch_uid=f"audit-save-{model._meta.label}")
            post_delete.connect(_on_delete, sender=model, dispatch_uid=f"audit-del-{model._meta.label}")

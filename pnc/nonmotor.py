"""Non-motor extras: medical census and pre-authorisation, cargo declarations, reserve authority, reinsurer recoveries and bordereaux."""
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from core import clock
from core.models import Currency, convert, to_base, user_role

from . import services as sv
from .models import (PncClaim, PncCession, PncDeclaration, PncMember, PncPolicy, PncPreauth, PncReserveChange, PncRiRecovery)
from .services import PncError, q

D = Decimal
ZERO = D("0")
MEDICAL_GROUPS = ("MEDICAL",)


# ---- medical census -------------------------------------------------------------------------------------------------
def _date(s):
    s = (s or "").strip()
    if not s:
        return None
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    raise ValueError(f"'{s}' is not a date (use YYYY-MM-DD)")


def _check_medical(policy):
    if policy.pnc_class.group not in MEDICAL_GROUPS:
        raise PncError("Members apply to medical and group medical policies only.")


def next_member_no(policy):
    return f"M{policy.members.count() + 1:05d}"


def add_member(policy, name, relation="SELF", category="", dob=None, eligible_from=None):
    _check_medical(policy)
    if policy.status not in ("ACTIVE", "APPROVED", "QUOTE", "PENDING"):
        raise PncError("This policy is not open for enrolment.")
    name = (name or "").strip()
    if not name:
        raise PncError("Enter the member's name.")
    eligible_from = eligible_from or max(policy.start_date, clock.today())
    if eligible_from < policy.start_date or eligible_from > policy.end_date:
        raise PncError("The effective date is outside the policy period.")
    dup = policy.members.filter(name__iexact=name, dob=dob).exclude(eligible_to__lt=eligible_from)
    if dup.exists():
        raise PncError(f"{name} is already enrolled on this policy (duplicate or overlapping enrolment).")
    n = policy.members.count() + 1
    while policy.members.filter(member_no=f"M{n:05d}").exists():
        n += 1
    return PncMember.objects.create(policy=policy, member_no=f"M{n:05d}", name=name, relation=relation, category=category[:40], dob=dob, eligible_from=eligible_from)


def remove_member(member, effective):
    if member.eligible_to and member.eligible_to < effective:
        raise PncError("This member is already removed.")
    if effective < member.eligible_from:
        raise PncError("The removal date is before the member's start date.")
    member.eligible_to = effective
    member.save(update_fields=["eligible_to"])
    return member


def upload_census(policy, text):
    """CSV columns: name, relation, category, dob, effective_from. All valid rows are added; each bad row is reported with its line number."""
    _check_medical(policy)
    rows = list(csv.DictReader(io.StringIO(text)))
    added, errors = 0, []
    seen = set()
    rel = {k for k, _ in PncMember.RELATIONS}
    with transaction.atomic():
        for i, r in enumerate(rows, start=2):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            try:
                relation = (r.get("relation") or "SELF").upper()
                if relation not in rel:
                    raise PncError(f"relation '{relation}' is not one of {', '.join(sorted(rel))}")
                dob, eff = _date(r.get("dob")), _date(r.get("effective_from"))
                key = (r.get("name", "").lower(), dob)
                if key in seen:
                    raise PncError("duplicate row in the file")
                seen.add(key)
                sid = transaction.savepoint()
                try:
                    add_member(policy, r.get("name"), relation, r.get("category", ""), dob, eff)
                except Exception:
                    transaction.savepoint_rollback(sid)
                    raise
                added += 1
            except (PncError, ValueError) as e:
                errors.append(f"Line {i}: {e.args[0] if e.args else e}")
    return added, errors


# ---- pre-authorisation ----------------------------------------------------------------------------------------------
def request_preauth(member, provider, service_date, amount, diagnosis_code="", note=""):
    amount = D(amount or 0)
    if amount <= 0:
        raise PncError("Enter the requested amount.")
    pol = member.policy
    if pol.status != "ACTIVE":
        raise PncError("The policy is not in force.")
    n = PncPreauth.objects.count() + 1
    while PncPreauth.objects.filter(number=f"PA-{n:06d}").exists():
        n += 1
    return PncPreauth.objects.create(member=member, number=f"PA-{n:06d}", provider=provider, service_date=service_date, requested_amount=amount,
                                     diagnosis_code=diagnosis_code, note=note[:200])


def decide_preauth(pa, user, approve, amount=None, note=""):
    if user_role(user) not in ("ADMIN", "CLAIMS_HANDLER", "CLAIMS_MANAGER"):
        raise PncError("Only claims staff can decide a pre-authorisation.")
    if pa.status != PncPreauth.REQUESTED:
        raise PncError("This pre-authorisation was already decided.")
    m = pa.member
    if approve:
        if not m.eligible_on(pa.service_date):
            raise PncError(f"{m.name} is not eligible on {pa.service_date}.")
        if not (m.policy.start_date <= pa.service_date <= m.policy.end_date):
            raise PncError("The service date is outside the policy period.")
        waiting = int(m.policy.risks.filter(active=True).first().details.get("waiting") or 0) if m.policy.risks.filter(active=True).exists() else 0
        if waiting and (pa.service_date - m.eligible_from).days < waiting:
            raise PncError(f"The {waiting}-day waiting period has not passed.")
        amt = D(amount) if amount not in (None, "") else pa.requested_amount
        if amt <= 0:
            raise PncError("Enter the approved amount.")
        if amt > m.remaining:
            raise PncError(f"Only {m.remaining} of the benefit limit is left for this member.")
        pa.approved_amount, pa.status = q(m.policy.currency, amt), PncPreauth.APPROVED
    else:
        if not (note or "").strip():
            raise PncError("Give the reason for declining.")
        pa.status = PncPreauth.DECLINED
    pa.decided_by, pa.note = user, (note or pa.note)[:200]
    pa.save()
    return pa


def settle_preauth(pa, actual, invoice_no):
    """Final settlement against actual charges; duplicate invoices are refused. Pre-authorisation alone is not settlement."""
    if pa.status != PncPreauth.APPROVED:
        raise PncError("Only an approved pre-authorisation can be settled.")
    if not invoice_no:
        raise PncError("Enter the provider invoice number.")
    if PncPreauth.objects.filter(invoice_no=invoice_no, member__policy=pa.member.policy, status=PncPreauth.SETTLED).exists():
        raise PncError("This invoice number was already settled: duplicate invoice.")
    actual = D(actual or 0)
    if actual <= 0:
        raise PncError("Enter the actual charges.")
    pa.actual_amount = q(pa.member.policy.currency, min(actual, pa.approved_amount))
    pa.approved_amount = pa.actual_amount                      # unused authorisation is released back to the member's limit
    pa.invoice_no, pa.status = invoice_no, PncPreauth.SETTLED
    pa.save()
    return pa


# ---- cargo open-cover declarations ----------------------------------------------------------------------------------
def declared_total(risk):
    from django.db.models import Sum
    return risk.declarations.filter(cancelled=False).aggregate(s=Sum("value"))["s"] or ZERO


def add_declaration(policy, risk, user, voyage, value, conveyance="", invoice_no="", declared_on=None):
    if policy.pnc_class.code != "CARGO":
        raise PncError("Declarations apply to marine cargo policies.")
    if policy.status != "ACTIVE":
        raise PncError("The policy is not in force.")
    declared_on = declared_on or clock.today()
    if not (policy.start_date <= declared_on <= policy.end_date):
        raise PncError("The declaration date is outside the policy period.")
    value = D(value or 0)
    if value <= 0:
        raise PncError("Enter the shipment value.")
    if not voyage:
        raise PncError("Enter the voyage / route.")
    limit = D(str(risk.details.get("per_conveyance") or 0))
    if limit and value > limit:
        raise PncError(f"The shipment value exceeds the per-conveyance limit of {limit}.")
    if declared_total(risk) + value > risk.sum_insured:
        raise PncError(f"The declaration would exceed the open cover limit of {risk.sum_insured} (declared so far {declared_total(risk)}).")
    if invoice_no and PncDeclaration.objects.filter(policy=policy, invoice_no=invoice_no, cancelled=False).exists():
        raise PncError("A declaration with this invoice number already exists.")
    n = PncDeclaration.objects.count() + 1
    while PncDeclaration.objects.filter(number=f"DEC-{n:06d}").exists():
        n += 1
    premium = q(policy.currency, value * risk.rate_pct * (1 + risk.loading_pct / 100) / 100)
    return PncDeclaration.objects.create(policy=policy, risk=risk, number=f"DEC-{n:06d}", declared_on=declared_on, voyage=voyage[:120], conveyance=conveyance[:80],
                                         invoice_no=invoice_no, value=value, premium=premium, created_by=user)


def cancel_declaration(dec):
    dec.cancelled = True
    dec.save(update_fields=["cancelled"])
    return dec


# ---- reserve authority ----------------------------------------------------------------------------------------------
def reserve_authority(user):
    """Largest reserve movement (base currency) the user can make alone; None = unlimited."""
    role = user_role(user)
    if role in ("ADMIN", "CLAIMS_MANAGER"):
        return None
    return settings.PNC_RESERVE_AUTHORITY.get(role, ZERO)


@transaction.atomic
def revise_reserve(claim, user, reserve, reason):
    """Record the revision. Within the user's authority it is applied now; otherwise it waits for a claims manager."""
    if not (reason or "").strip():
        raise PncError("Give the reason for the reserve change.")
    sv.assert_open(claim)
    reserve = q(claim.policy.currency, reserve)
    delta = to_base(abs(reserve - claim.reserve), claim.policy.currency)
    auth = reserve_authority(user)
    ch = PncReserveChange.objects.create(claim=claim, old_reserve=claim.reserve, new_reserve=reserve, reason=reason.strip()[:200], requested_by=user)
    if auth is None or delta <= auth:
        sv.set_reserve(claim, user, reserve)
        ch.status, ch.decided_by = PncReserveChange.APPLIED, user
        ch.save(update_fields=["status", "decided_by"])
    return ch


@transaction.atomic
def decide_reserve(ch, user, approve):
    if user_role(user) not in ("ADMIN", "CLAIMS_MANAGER"):
        raise PncError("Only a claims manager can approve a reserve change above the handler's authority.")
    if ch.status != PncReserveChange.PENDING:
        raise PncError("This change was already decided.")
    if ch.requested_by_id == user.id and not user.is_superuser:
        raise PncError("A reserve change must be approved by someone other than the person who asked for it.")
    if approve:
        sv.set_reserve(ch.claim, user, ch.new_reserve)
    ch.status = PncReserveChange.APPLIED if approve else PncReserveChange.REJECTED
    ch.decided_by = user
    ch.save(update_fields=["status", "decided_by"])
    return ch


# ---- reinsurer recoveries --------------------------------------------------------------------------------------------
def book_recoveries(claim, payment):
    """Record each treaty's share of a payment as a recoverable (estimated), so it can be agreed, billed, disputed and collected separately."""
    inc = sv.incurred_base(claim)
    if inc <= 0:
        return []
    ratio = min(to_base(payment.amount, claim.policy.currency) / inc, D(1))
    rows = []
    for t, kind, amt in sv.claim_layers(claim, inc):
        share = amt * ratio
        if share > 0:
            rows.append(PncRiRecovery.objects.create(claim=claim, payment=payment, treaty=t, kind=kind, amount=q(Currency.base(), share)))
    return rows


RI_ROLES = ("ADMIN", "FINANCE", "CLAIMS_MANAGER")


@transaction.atomic
def ri_action(rec, user, action, amount=None, note="", on=None):
    if user_role(user) not in RI_ROLES:
        raise PncError("Only Finance or the Claims Manager can update reinsurance recoveries.")
    today = on or clock.today()
    st = PncRiRecovery
    if action == "agree" and rec.status == st.ESTIMATED:
        rec.status = st.AGREED
    elif action == "bill" and rec.status in (st.ESTIMATED, st.AGREED, st.DISPUTED):
        rec.status, rec.billed_on, rec.due_on = st.BILLED, today, today + timedelta(days=30)
    elif action == "dispute" and rec.status in (st.BILLED, st.AGREED):
        if not (note or "").strip():
            raise PncError("Give the reason for the dispute.")
        rec.status = st.DISPUTED
    elif action == "collect" and rec.status in (st.BILLED, st.AGREED, st.DISPUTED):
        amt = D(amount) if amount not in (None, "") else rec.outstanding
        if amt <= 0 or amt > rec.outstanding + D("0.0005"):
            raise PncError(f"Enter an amount up to the outstanding {rec.outstanding}.")
        sv.add_recovery(rec.claim, user, "REINSURANCE", convert(amt, Currency.base(), rec.claim.policy.currency), today, f"{rec.treaty.reference} {rec.kind}")
        rec.collected += amt
        rec.collected_on = today
        if rec.outstanding <= 0:
            rec.status = st.COLLECTED
    else:
        raise PncError(f"That step is not available while the recovery is {rec.get_status_display().lower()}.")
    if note:
        rec.note = note[:200]
    rec.save()
    return rec


# ---- bordereaux -------------------------------------------------------------------------------------------------------
def premium_bordereau(treaty=None, start=None, end=None):
    qs = PncCession.objects.filter(is_facultative=False, treaty__isnull=False).select_related("policy__customer", "policy__pnc_class", "treaty", "risk")
    if treaty:
        qs = qs.filter(treaty=treaty)
    if start:
        qs = qs.filter(policy__start_date__gte=start)
    if end:
        qs = qs.filter(policy__start_date__lte=end)
    return [[c.treaty.reference, c.policy.policy_no or c.policy.quote_no, c.policy.customer.name, c.policy.pnc_class.name, c.risk.description if c.risk else "",
             c.policy.start_date, c.policy.end_date, c.sum_insured_ceded, c.premium_ceded, c.treaty_type] for c in qs]


PREMIUM_HEAD = ["Treaty", "Policy", "Insured", "Class", "Risk", "From", "To", "Sum insured ceded (base)", "Premium ceded (base)", "Type"]


def claims_bordereau(treaty=None, start=None, end=None):
    qs = PncRiRecovery.objects.select_related("claim__policy__customer", "treaty", "payment")
    if treaty:
        qs = qs.filter(treaty=treaty)
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    return [[r.treaty.reference, r.claim.claim_no, r.claim.policy.policy_no, r.claim.policy.customer.name, r.claim.date_of_loss, r.kind,
             r.payment.paid_on if r.payment else "", r.amount, r.collected, r.outstanding, r.get_status_display()] for r in qs]


CLAIMS_HEAD = ["Treaty", "Claim", "Policy", "Insured", "Date of loss", "Layer", "Paid on", "Recoverable (base)", "Collected", "Outstanding", "Status"]

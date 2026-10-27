"""P&C postings on the shared ledger. Entries carry no motor policy / claim link; they are found by ``source`` (policy / claim number)."""
from decimal import Decimal

from django.db.models import Sum

from accounting import services as acc
from accounting.models import JournalEntry, JournalLine
from core import clock
from core.models import to_base, user_role

D = Decimal
ZERO = D("0")
q3 = acc.q3


def _b(policy, amount):
    return q3(D(amount) * D(policy.fx_rate))


def _post(policy, etype, narration, lines, source, user=None, on=None):
    e = acc.post(etype, narration, lines, source=source, user=user, on=on)
    if e:
        e.currency, e.fx_rate, e.is_demo, e.book = policy.currency, policy.fx_rate, policy.is_demo, "PNC"
        e.save(update_fields=["currency", "fx_rate", "is_demo", "book"])
    return e


def posted(source, code, side, types=None, prefix=False):
    qs = JournalLine.objects.filter(account_id=code)
    qs = qs.filter(entry__source__startswith=source) if prefix else qs.filter(entry__source=source)
    if types:
        qs = qs.filter(entry__entry_type__in=types)
    a = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    return (a["d"] or ZERO) if side == "D" else (a["c"] or ZERO)


def net_cr(source, code, types=None):
    return posted(source, code, "C", types) - posted(source, code, "D", types)


def sync_policy(policy, user, etype, narration):
    """Post the difference between the policy's current premium / VAT / reinsurance / commission and what is booked."""
    src = policy.policy_no
    kinds = ["ISSUE", "ENDORSE"]
    net = q3(_b(policy, policy.net_premium) - net_cr(src, "2110", kinds))
    fees = q3(_b(policy, policy.fees) - net_cr(src, "4300", kinds))
    tax = q3(_b(policy, policy.tax) - net_cr(src, "2210", kinds))
    lines = [("2110", "C", net), ("4300", "C", fees), ("2210", "C", tax)]
    cr = sum((a for _, _, a in lines), ZERO)
    lines.append(("1120", "D", cr))
    ceded = sum((D(c.premium_ceded) for c in policy.cessions.all()), ZERO)
    d_ri = q3(ceded - net_cr(src, "2230", kinds))
    comm = q3(_b(policy, policy.net_premium) * policy.pnc_class.commission_pct / D(100)) if user_role(policy.producer) in ("AGENT", "BROKER") else ZERO
    d_cm = q3(comm - net_cr(src, "2220", kinds))
    if d_ri:
        lines += [("1210", "D", d_ri), ("2230", "C", d_ri)]
    if d_cm:
        lines += [("5120", "D", d_cm), ("2220", "C", d_cm)]
    return _post(policy, JournalEntry.ENDORSE if etype == "ENDORSE" else JournalEntry.ISSUE, narration, lines, src, user)


def on_receipt(policy, receipt, user=None):
    amt = _b(policy, receipt.amount)
    return _post(policy, JournalEntry.RECEIPT, f"Receipt {receipt.number} from {policy.customer.name}", [("1110", "D", amt), ("1120", "C", amt)], policy.policy_no, user)


def earned_fraction(policy, through):
    total = max((policy.end_date - policy.start_date).days + 1, 1)
    if policy.status == "CANCELLED" and policy.cancelled_on and through >= policy.cancelled_on:
        return D(1)
    return D(min(max((through - policy.start_date).days + 1, 0), total)) / D(total)


def recognise_policy(policy, through, user=None):
    src = policy.policy_no
    frac = earned_fraction(policy, through)
    delta = q3(_b(policy, policy.net_premium) * frac - net_cr(src, "4110", ["EARN"]))
    ceded = sum((D(c.premium_ceded) for c in policy.cessions.all()), ZERO)
    d_ri = q3(ceded * frac - (posted(src, "5130", "D", ["EARN"]) - posted(src, "5130", "C", ["EARN"])))
    return _post(policy, JournalEntry.EARN, f"Revenue recognition to {through:%d %b %Y}: {src}",
                 [("2110", "D", delta), ("4110", "C", delta), ("5130", "D", d_ri), ("1210", "C", d_ri)], src, user, on=through)


def recognise_all(through, user=None):
    from .models import PncPolicy
    n = 0
    for p in PncPolicy.objects.exclude(policy_no=None).filter(start_date__lte=through).select_related("currency", "pnc_class"):
        if recognise_policy(p, through, user):
            n += 1
    return n


# ---- claims -----------------------------------------------------------------------------------------------------------
def sync_claim(claim, user=None, memo=""):
    from . import services as sv
    from .models import PncClaim
    p, src = claim.policy, claim.claim_no
    if claim.status == PncClaim.REJECTED:
        incurred = ZERO
    elif claim.status in (PncClaim.APPROVED, PncClaim.SETTLED, PncClaim.CLOSED):
        incurred = claim.net_payable
    else:
        incurred = max(claim.reserve - sv.deductible_for(claim, claim.reserve), ZERO)
    target = _b(p, incurred)
    delta = q3(target - (posted(src, "5110", "D", ["CLAIM"]) - posted(src, "5110", "C", ["CLAIM"])))
    ri_t = q3(sv.ri_total(claim, target)) if claim.status != PncClaim.REJECTED else ZERO
    d_ri = q3(ri_t - (posted(src, "1230", "D", ["CLAIM"]) - posted(src, "1230", "C", ["CLAIM"])))
    e = _post(p, JournalEntry.CLAIM, f"Claim {src}: incurred position ({claim.get_status_display()})" + (f" - {memo}" if memo else ""),
              [("5110", "D", delta), ("2120", "C", delta), ("1230", "D", d_ri), ("4210", "C", d_ri)], src, user)
    if claim.status == PncClaim.CLOSED:
        types = ["CLAIM", "CLAIM_PAY", "RECOVERY"]
        lic = net_cr(src, "2120", types)
        if lic > 0:
            _post(p, JournalEntry.CLAIM, f"Claim {src}: release of remaining LIC on closure", [("2120", "D", lic), ("5110", "C", lic)], src, user)
    return e


def on_payment(claim, pay, user=None):
    amt = _b(claim.policy, pay.amount)
    return _post(claim.policy, JournalEntry.CLAIM_PAY, f"Claim {claim.claim_no}: {pay.get_stage_display()} to {pay.payee}", [("2120", "D", amt), ("1110", "C", amt)],
                 claim.claim_no, user)


def on_recovery(claim, rec, user=None):
    amt = _b(claim.policy, rec.amount)
    if rec.recovery_type == "REINSURANCE":
        lines, label = [("1110", "D", amt), ("1230", "C", amt)], "reinsurance recovery received"
    else:
        lines, label = [("1110", "D", amt), ("5110", "C", amt)], rec.get_recovery_type_display().lower() + " recovered"
    return _post(claim.policy, JournalEntry.RECOVERY, f"Claim {claim.claim_no}: {label}", lines, claim.claim_no, user, on=rec.received_on)

"""Commission earned / due / settled for agents and brokers.

* Earned  = commission booked on the policy (base + overrider + profit, less clawbacks).
* Due     = the part of it that is payable now: commission is payable on premium that has been *collected*, so a line is
            payable pro-rata to (premium collected / premium invoiced), capped at 100 %.
* Settled = what has already been paid out (``Commission.settled_amount``).
* Balance = due - settled.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Sum

from core import clock

D = Decimal
ZERO = D("0")


def q3(x):
    return D(x).quantize(D("0.001"), rounding=ROUND_HALF_UP)


def collected_ratio(policy):
    """Share of the invoiced premium already received (base currency), 0-1."""
    total = D(policy.total_payable) * D(policy.fx_rate)
    if total <= 0:
        return D(1) if D(policy.total_payable) < 0 else ZERO
    got = sum((D(p.amount) * D(p.fx_rate) for p in policy.payments.all() if p.status == "PAID"), ZERO)
    return min(max(got / total, ZERO), D(1))


def eligible(line, ratio):
    """Commission payable now on one line. Clawbacks (negative) are always deducted in full."""
    if line.amount < 0:
        return D(line.amount)
    return q3(D(line.amount) * ratio)


def balance(line, ratio):
    return q3(eligible(line, ratio) - D(line.settled_amount))


def due_lines(producer, upto=None):
    """[(commission line, amount to pay now)] for a producer."""
    from .models import Commission

    qs = Commission.objects.filter(producer=producer).select_related("policy", "policy__currency").prefetch_related("policy__payments")
    if upto:
        qs = qs.filter(policy__issued_at__date__lte=upto)
    out = []
    for c in qs:
        amt = balance(c, collected_ratio(c.policy))
        if amt != 0:
            out.append((c, amt))
    return out


def _next_number():
    from .models import CommissionSettlement

    n = CommissionSettlement.objects.count() + 1
    while True:
        num = f"CS-{clock.today():%Y}-{n:05d}"
        if not CommissionSettlement.objects.filter(number=num).exists():
            return num
        n += 1


@transaction.atomic
def settle(producer, user=None, reference="", note="", upto=None, is_demo=False):
    """Pay every commission line that is payable (collected premium) and not yet settled. Returns the settlement or None."""
    from accounting import services as acc

    from .models import CommissionSettlement

    lines = due_lines(producer, upto)
    total = q3(sum((a for _, a in lines), ZERO))
    if not lines or total <= 0:
        return None
    st = CommissionSettlement.objects.create(number=_next_number(), producer=producer, paid_on=clock.today(), amount=total,
                                             reference=reference, note=note, created_by=user, is_demo=is_demo)
    for c, amt in lines:
        c.settled_amount = q3(D(c.settled_amount) + amt)
        c.settled_on = st.paid_on
        c.settlement = st
        c.save(update_fields=["settled_amount", "settled_on", "settlement"])
    acc.on_commission_paid(st, user)
    return st

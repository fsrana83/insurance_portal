"""Reinsurance allocation by treaty, used by the registers.

Proportional treaties (quota share / surplus) follow the premium ceded on the policy: a claim's reinsurers' share under a
treaty = premium ceded to that treaty / net premium of the risk (the same rule the ledger uses for the reinsurance asset).
Facultative placements are shown on their own line ("FAC") but are not part of the automatic recovery posted to the ledger.
"""
from collections import OrderedDict
from decimal import Decimal

from django.db.models import Sum

from core.models import to_base

D = Decimal
ZERO = D("0")


def key_of(cession):
    if cession.is_facultative or not cession.treaty_id:
        return "FAC"
    return cession.treaty.reference


def label_of(cession):
    if cession.is_facultative or not cession.treaty_id:
        fr = cession.fac_reinsurer.name if cession.fac_reinsurer_id else "unplaced"
        return f"Facultative ({fr})"
    return f"{cession.treaty.reference} {cession.treaty.name}"


def policy_weights(policy):
    """{key: (label, ceded premium)} for the policy's current cessions."""
    out = OrderedDict()
    for c in sorted(policy.cessions.all(), key=lambda c: (c.rank, c.id)):
        k = key_of(c)
        lab = label_of(c)
        out[k] = (out[k][0] if k in out else lab, (out[k][1] if k in out else ZERO) + D(c.premium_ceded))
    return out


def split(amount, weights):
    """Split ``amount`` across treaties pro-rata to their ceded premium. Returns {key: amount}."""
    tot = sum((w for _, w in weights.values()), ZERO)
    if not tot or not amount:
        return {}
    return {k: D(amount) * w / tot for k, (_, w) in weights.items()}


def claim_shares(claim):
    """{key: (label, share 0-1)} for the claim's risk (proportional treaties only; FAC has share 0 for the ledger)."""
    pol = claim.policy
    net_base = to_base(claim.risk.net_premium, pol.currency)
    out = OrderedDict()
    for c in sorted(pol.cessions.all(), key=lambda c: (c.rank, c.id)):
        if c.risk_id and c.risk_id != claim.risk_id:
            continue
        if c.treaty_type == "XOL":
            continue
        k = key_of(c)
        share = D(c.premium_ceded) / net_base if net_base else ZERO
        if k == "FAC":
            share = ZERO
        prev = out.get(k)
        out[k] = (prev[0] if prev else label_of(c), (prev[1] if prev else ZERO) + share)
    total = sum((s for _, s in out.values()), ZERO)
    if total > 1:                                   # never recover more than the claim
        out = OrderedDict((k, (l, s / total)) for k, (l, s) in out.items())
    return out


def total_share(shares):
    return min(sum((s for _, s in shares.values()), ZERO), D(1))


def xol_weights(claim, loss_base):
    """{reference: (label, recovery)} excess-of-loss recoveries the claim triggers on a loss (base currency)."""
    from claims.services import xol_recoveries

    return OrderedDict((t.reference, (f"{t.reference} {t.name}", a)) for t, a in xol_recoveries(claim, loss_base))


def allocate(claim, basis, xol_amount, loss_base):
    """Split a reinsurance amount across treaties: proportional treaties take ``basis`` x their share, the XoL part
    (``xol_amount``) goes to the XoL treaties that respond to a loss of ``loss_base``. Returns {key: (label, amount)}."""
    out = OrderedDict((k, (lab, D(basis) * s)) for k, (lab, s) in claim_shares(claim).items())
    xol_amount = D(xol_amount)
    if xol_amount:
        ws = xol_weights(claim, loss_base)
        tot = sum((a for _, a in ws.values()), ZERO)
        if ws and tot > 0:
            for k, (lab, a) in ws.items():
                out[k] = (lab, xol_amount * a / tot)
        else:
            out["XOL"] = ("Excess of loss", xol_amount)
    return out

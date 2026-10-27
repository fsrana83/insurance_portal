"""Actuals taken from the IFRS 17 ledger, so budget-versus-actual always reconciles to the accounts.

    GWP      credits to 2110 (LRC) on ISSUE / ENDORSE entries  = premium written, net of VAT and fees, net of refunds
    EARNED   credits to 4110 on EARN entries                   = insurance revenue
    CLAIMS   debits less credits on 5110                       = incurred claims (reserve movements, payments' effect, recoveries)
    COMM     debits less credits on 5120                       = acquisition cash flows
    POLICIES count of ISSUE entries                            = policies issued (new business and renewals)
Everything is in the base currency (OMR) and grouped by the product of the policy (or the claim's policy) and by entry month."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce, ExtractMonth, ExtractYear

from accounting.models import JournalEntry, JournalLine

ZERO = Decimal("0")


def _product(prefix="entry__"):
    return Coalesce(F(prefix + "policy__product_id"), F(prefix + "claim__policy__product_id"))


def actuals(year_from, year_to=None, product=None):
    """{(year, month): {product_id: {metric: Decimal}}} for the calendar years year_from..year_to."""
    year_to = year_to or year_from
    out = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: ZERO)))
    lines = JournalLine.objects.filter(entry__date__year__gte=year_from, entry__date__year__lte=year_to).filter(entry__book="MOTOR")   # motor book only
    base = lines.annotate(pid=_product(), y=ExtractYear("entry__date"), m=ExtractMonth("entry__date"))
    if product:
        base = base.filter(pid=product)
    for r in base.filter(account_id="2110", entry__entry_type__in=["ISSUE", "ENDORSE"]).values("pid", "y", "m").annotate(c=Sum("credit"), d=Sum("debit")):
        out[(r["y"], r["m"])][r["pid"]]["GWP"] += (r["c"] or ZERO) - (r["d"] or ZERO)
    for r in base.filter(account_id="4110", entry__entry_type="EARN").values("pid", "y", "m").annotate(c=Sum("credit"), d=Sum("debit")):
        out[(r["y"], r["m"])][r["pid"]]["EARNED"] += (r["c"] or ZERO) - (r["d"] or ZERO)
    for r in base.filter(account_id="5110").values("pid", "y", "m").annotate(c=Sum("credit"), d=Sum("debit")):
        out[(r["y"], r["m"])][r["pid"]]["CLAIMS"] += (r["d"] or ZERO) - (r["c"] or ZERO)
    for r in base.filter(account_id="5120").values("pid", "y", "m").annotate(c=Sum("credit"), d=Sum("debit")):
        out[(r["y"], r["m"])][r["pid"]]["COMM"] += (r["d"] or ZERO) - (r["c"] or ZERO)
    ents = JournalEntry.objects.filter(entry_type="ISSUE", book="MOTOR", date__year__gte=year_from, date__year__lte=year_to).annotate(pid=Coalesce(F("policy__product_id"), F("claim__policy__product_id")))
    if product:
        ents = ents.filter(pid=product)
    for r in ents.annotate(y=ExtractYear("date"), m=ExtractMonth("date")).values("pid", "y", "m").annotate(n=Count("id")):
        out[(r["y"], r["m"])][r["pid"]]["POLICIES"] += Decimal(r["n"])
    return out


def flat(year_from, year_to=None, product=None):
    """{(product_id, metric, year, month): value}"""
    res = {}
    for (y, m), prods in actuals(year_from, year_to, product).items():
        for pid, mets in prods.items():
            for met, v in mets.items():
                res[(pid, met, y, m)] = v
    return res


def last_closed(today=None):
    """(year, month) of the last fully closed month."""
    t = today or date.today()
    return (t.year, t.month - 1) if t.month > 1 else (t.year - 1, 12)

"""Role-specific dashboard data (FRD 8.1 and addendum section 5)."""
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Sum

from core.models import Currency, to_base, user_role
from core.permissions import PRODUCERS, scope

D = Decimal
ZERO = D("0")


def gwp(policies):
    return sum((to_base(p.net_premium, p.currency) for p in policies.select_related("currency")), ZERO)


def dashboard_context(user):
    from claims.models import Claim, Provider, Subrogation
    from motor.models import Commission, Policy, Proposal
    from reinsurance.engine import actual_gnrpi, capacity_utilisation
    from reinsurance.models import Cession, Treaty

    role = user_role(user)
    today = date.today()
    month_start, year_start = today.replace(day=1), today.replace(month=1, day=1)
    ctx = {"role": role, "base": Currency.base()}
    mine = scope(user, Policy.objects.all())
    show_all = role in ("ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_MANAGER", "CLAIMS_HANDLER")

    if role in PRODUCERS | {"ADMIN", "PRODUCTION_MANAGER"}:
        ctx["producer"] = {
            "mtd": gwp(mine.filter(issued_at__date__gte=month_start)),
            "ytd": gwp(mine.filter(issued_at__date__gte=year_start)),
            "mtd_count": mine.filter(issued_at__date__gte=month_start).count(),
            "ytd_count": mine.filter(issued_at__date__gte=year_start).count(),
            "pending_quotes": scope(user, Proposal.objects.filter(status__in=["QUOTE", "PROPOSAL", "PENDING", "APPROVED"]))
            .select_related("customer", "currency")[:8],
        }
        comm = Commission.objects.filter(producer=user) if role != "ADMIN" else Commission.objects.all()
        ctx["producer"]["commission_ytd"] = comm.filter(created_at__date__gte=year_start).aggregate(s=Sum("amount"))["s"] or ZERO
        ctx["producer"]["commission_mtd"] = comm.filter(created_at__date__gte=month_start).aggregate(s=Sum("amount"))["s"] or ZERO

    if role in PRODUCERS | {"ADMIN", "PRODUCTION_MANAGER", "CUSTOMER", "UNDERWRITER"}:
        ctx["expiring"] = mine.filter(status="ACTIVE", end_date__gte=today, end_date__lte=today + timedelta(days=45)) \
            .select_related("customer", "currency").order_by("end_date")[:8]

    if role in ("ADMIN", "PRODUCTION_MANAGER"):
        rows = []
        for u in {p.producer for p in Policy.objects.filter(issued_at__date__gte=year_start).select_related("producer")}:
            pol = Policy.objects.filter(producer=u, issued_at__date__gte=year_start)
            rows.append({"user": u, "count": pol.count(), "gwp": gwp(pol)})
        ctx["team"] = sorted(rows, key=lambda r: r["gwp"], reverse=True)
        ytd = Policy.objects.filter(issued_at__date__gte=year_start)
        renewals = ytd.filter(proposal__renewal_of__isnull=False)
        ctx["nb_vs_renewal"] = {"new": gwp(ytd.filter(proposal__renewal_of__isnull=True)), "renewal": gwp(renewals),
                                "new_count": ytd.filter(proposal__renewal_of__isnull=True).count(), "renewal_count": renewals.count()}
        ctx["approval_count"] = Proposal.objects.filter(status="PENDING").count()
        total = ctx["nb_vs_renewal"]["new"] + ctx["nb_vs_renewal"]["renewal"]
        ctx["nb_vs_renewal"]["new_pct"] = (ctx["nb_vs_renewal"]["new"] / total * 100) if total else ZERO

    if role in ("ADMIN", "UNDERWRITER", "FINANCE"):
        util = []
        for t in Treaty.objects.filter(active=True, superseded_by__isnull=True, treaty_type="SURPLUS"):
            used, cap, pct = capacity_utilisation(t)
            util.append({"treaty": t, "used": used, "cap": cap, "pct": min(pct, D(100))})
        ctx["capacity"] = util
        active = Policy.objects.filter(status__in=["ACTIVE", "PENDING_FAC"])
        exposures = sorted(((p, sum((to_base(r.sum_insured, p.currency) for r in p.risks.all()), ZERO)) for p in active.select_related("currency", "customer")),
                           key=lambda t: t[1], reverse=True)
        ctx["exposure_total"] = sum((e for _, e in exposures), ZERO)
        ctx["top_risks"] = exposures[:5]
        ctx["fac_pending"] = Cession.objects.filter(is_facultative=True, fac_confirmed=False).count()
        loaded = sum((to_base(p.expense_load, p.currency) for p in Policy.objects.filter(issued_at__date__gte=year_start).select_related("currency")), ZERO)
        actual = Commission.objects.filter(created_at__date__gte=year_start).aggregate(s=Sum("amount"))["s"] or ZERO
        ctx["expense_ratio"] = {"loaded": loaded, "actual": actual, "variance": loaded - actual,
                                "util_pct": (actual / loaded * 100) if loaded else ZERO}

    if role in ("ADMIN", "CLAIMS_MANAGER", "CLAIMS_HANDLER"):
        by_status = {s: Claim.objects.filter(status=s).count() for s, _ in Claim.STATUSES}
        ctx["claims_status"] = by_status
        subs = Subrogation.objects.select_related("claim__policy__currency")
        paid = sum((to_base(s.reserve_amount, s.claim.policy.currency) for s in subs), ZERO)
        rec = sum((to_base(s.amount_recovered, s.claim.policy.currency) for s in subs), ZERO)
        ctx["subrogation"] = {"paid": paid, "recovered": rec, "ratio": (rec / paid * 100) if paid else ZERO, "open": subs.exclude(recovery_status__in=["RECOVERED", "WRITTEN_OFF"]).count()}
        garages = Provider.objects.filter(provider_type="GARAGE", ratings__isnull=False).distinct().order_by("-average_score")
        ctx["top_garages"] = list(garages[:3])
        ctx["bottom_garages"] = list(garages.order_by("average_score")[:3])
        open_surveys = Claim.objects.filter(surveyor__isnull=False, surveyor_report_on__isnull=True).exclude(status__in=["CLOSED", "REJECTED"]).select_related("surveyor")
        from django.conf import settings
        ctx["open_surveys"] = [(c, c.surveyor_days, (c.surveyor_days or 0) > settings.SURVEYOR_SLA_DAYS) for c in open_surveys[:8]]
        ctx["repair_delays"] = Claim.objects.filter(repair_delay_days__gt=0).count()

    if role in ("ADMIN", "FINANCE"):
        mdp = []
        for t in Treaty.objects.filter(active=True, superseded_by__isnull=True, treaty_type="XOL"):
            actual = actual_gnrpi(t)
            add, earned = (max(actual * t.rate_pct / 100 - t.mdp, ZERO), actual * t.rate_pct / 100)
            mdp.append({"treaty": t, "estimated": t.est_gnrpi, "actual": actual, "mdp": t.mdp, "earned": earned, "additional": add,
                        "pct": min((actual / t.est_gnrpi * 100) if t.est_gnrpi else ZERO, D(200))})
        ctx["mdp"] = mdp
        pos = Commission.objects.exclude(kind="CLAWBACK").aggregate(s=Sum("amount"))["s"] or ZERO
        neg = Commission.objects.filter(kind="CLAWBACK").aggregate(s=Sum("amount"))["s"] or ZERO
        ctx["commission_split"] = {"payable": pos, "clawback": -neg, "net": pos + neg}
        ctx["fac_pending"] = Cession.objects.filter(is_facultative=True, fac_confirmed=False).count()
        if "capacity" not in ctx:
            ctx["capacity"] = [{"treaty": t, "used": u, "cap": c, "pct": min(p, D(100))} for t in Treaty.objects.filter(active=True, superseded_by__isnull=True, treaty_type="SURPLUS")
                               for (u, c, p) in [capacity_utilisation(t)]]

    if role == "CUSTOMER":
        ctx["my_policies"] = mine.select_related("currency")
        ctx["my_claims"] = Claim.objects.filter(policy__in=mine)[:6]
    return ctx

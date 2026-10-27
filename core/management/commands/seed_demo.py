"""Seed reference data (currencies, products, tariffs, treaties...) and optional demo business.

NOTE: every rate, tariff, excess and limit below is an ILLUSTRATIVE PLACEHOLDER.
Replace with your approved FSA motor tariff and your board-approved pricing before go-live.
"""
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from core.models import Currency, ROLES, UserProfile
from motor import services
from motor.models import (
    ApprovalLimit, CommissionRule, Cover, Customer, Discount, Fee, Policy, Product, ProductCover, Proposal,
    ProposalRisk, RateTable, Vehicle,
)
from reinsurance import engine
from reinsurance.models import Reinsurer, Treaty
from claims.models import Provider
from claims import services as cs


class Command(BaseCommand):
    help = "Load reference data and (unless --no-demo) demo users, policies and claims."

    def add_arguments(self, p):
        p.add_argument("--no-demo", action="store_true", help="Reference data only (no demo logins)")
        p.add_argument("--demo", action="store_true", help="Also load the full demo book (1000 policies / 300 claims)")

    def handle(self, *a, **o):
        self.reference()
        from accounting.services import ensure_accounts
        ensure_accounts()
        if not o["no_demo"]:
            self.logins()
        if o["demo"]:
            from core.demo import load_demo
            self.stdout.write(str(load_demo()))
        self.stdout.write(self.style.SUCCESS("Seed complete. Demo logins: admin / agent1 / broker1 / pm1 / uw1 / claims1 / claimsmgr1 / finance1 / credit1 / customer1 — password demo1234"))

    # ------------------------------------------------------------------ reference
    def reference(self):
        # new Omani rial sign = U+20C4, UAE dirham = U+20C3, Saudi riyal = U+20C1 (all need a recent font; app.js falls back to the Arabic abbreviation)
        cur = [
            ("OMR", "Omani Rial", "⃄", "ر.ع.", 3, "1", True),
            ("USD", "US Dollar", "$", "$", 2, "0.38450000", False),
            ("AED", "UAE Dirham", "⃃", "د.إ", 2, "0.10470000", False),
            ("SAR", "Saudi Riyal", "⃁", "ر.س", 2, "0.10250000", False),
            ("GBP", "Pound Sterling", "£", "£", 2, "0.50500000", False),
        ]
        for code, name, sym, fb, dec, rate, base in cur:
            Currency.objects.update_or_create(code=code, defaults=dict(name=name, symbol=sym, symbol_fallback=fb, decimals=dec,
                                                                       rate_to_base=D(rate), is_base=base, active=True))
        for _, label in ROLES:
            Group.objects.get_or_create(name=label)

        comp, _ = Product.objects.update_or_create(code="COMP", defaults=dict(
            name="Motor Comprehensive", includes_od=True, is_tariff_regulated=False, expense_ratio=D("20"),
            description="Own damage plus compulsory third-party liability, with natural-disaster cover.",
            standard_excess=D("50"), young_driver_excess=D("100"), undisclosed_driver_excess=D("150")))
        tpl, _ = Product.objects.update_or_create(code="TPL", defaults=dict(
            name="Motor Third Party", includes_od=False, is_tariff_regulated=True, expense_ratio=D("20"),
            description="Compulsory third-party liability at the regulated tariff, with natural-disaster cover.",
            standard_excess=D("0"), young_driver_excess=D("0"), undisclosed_driver_excess=D("0")))

        od, _ = Cover.objects.update_or_create(code="OD", defaults=dict(name="Own Damage", kind="OD"))
        tp, _ = Cover.objects.update_or_create(code="TP", defaults=dict(name="Third Party Liability", kind="TP"))
        riders = {
            "NATCAT": dict(name="Natural disasters and weather", description="Automatic under the unified policy", flat_amount=D("0")),
            "PV": dict(name="Political violence", description="Riot, strike, civil commotion", rate_pct=D("0.050"), min_premium=D("4")),
            "EXCESS_PROT": dict(name="Excess protector", description="Insurer pays the excess", flat_amount=D("12")),
            "GCC": dict(name="GCC extension", description="Cover in other GCC states", flat_amount=D("20")),
            "RSA": dict(name="Roadside assistance", description="Towing and recovery", flat_amount=D("10")),
            "AGENCY": dict(name="Agency repair", description="Repairs at the dealer", rate_pct=D("0.350"), min_premium=D("20")),
            "REPL_CAR": dict(name="Replacement car", description="Courtesy car during repair", flat_amount=D("18")),
            "PAB": dict(name="Personal accident benefit", description="Driver and passengers (TPL policies only)", flat_amount=D("6"), only_products="TPL"),
        }
        rc = {}
        for code, d in riders.items():
            rc[code], _ = Cover.objects.update_or_create(code=code, defaults=dict(kind="RIDER", **d))
        for product in (comp, tpl):
            ProductCover.objects.update_or_create(product=product, cover=rc["NATCAT"], defaults=dict(mandatory=True, waivable=False, regulatory=True))
        for code in ("PV", "EXCESS_PROT"):
            ProductCover.objects.update_or_create(product=comp, cover=rc[code], defaults=dict(mandatory=True, waivable=True, regulatory=False))

        RateTable.objects.all().delete()
        od_rows = [  # vehicle_type, usage, age_min, rate, min
            ("", "PRIVATE", 0, "2.400", "90"), ("SUV", "PRIVATE", 0, "2.800", "110"), ("PICKUP", "PRIVATE", 0, "2.600", "100"),
            ("", "PRIVATE", 8, "3.200", "110"), ("SUV", "PRIVATE", 8, "3.600", "130"),
            ("", "COMMERCIAL", 0, "3.400", "150"), ("", "TAXI", 0, "4.500", "180"), ("", "RENTAL", 0, "4.000", "160"),
            ("", "DRIVING_SCHOOL", 0, "3.800", "140"), ("", "AGRICULTURAL", 0, "3.000", "120"), ("", "GOVERNMENT", 0, "3.000", "130"),
            ("MOTORCYCLE", "", 0, "3.200", "60"), ("TRAILER", "", 0, "2.500", "80"),
        ]
        for vt, us, age, rate, mn in od_rows:
            RateTable.objects.create(cover=od, vehicle_type=vt, usage=us, age_min=age, rate_pct=D(rate), min_premium=D(mn))
        tp_rows = [  # vehicle_type, usage, cc_min, cc_max, flat
            ("", "PRIVATE", 0, 2000, "32"), ("", "PRIVATE", 2001, 99999, "42"), ("", "COMMERCIAL", 0, 99999, "48"), ("", "TAXI", 0, 99999, "55"),
            ("", "RENTAL", 0, 99999, "52"), ("", "DRIVING_SCHOOL", 0, 99999, "50"), ("", "AGRICULTURAL", 0, 99999, "40"), ("", "GOVERNMENT", 0, 99999, "46"),
            ("MOTORCYCLE", "", 0, 99999, "18"), ("BUS", "", 0, 99999, "85"), ("TRUCK", "", 0, 99999, "90"), ("TRAILER", "", 0, 99999, "30"),
        ]
        for vt, us, lo, hi, flat in tp_rows:
            RateTable.objects.create(cover=tp, vehicle_type=vt, usage=us, cc_min=lo, cc_max=hi, flat_premium=D(flat))

        Discount.objects.all().delete()
        for k, th, pct in [("NCD", 1, 10), ("NCD", 2, 15), ("NCD", 3, 20), ("NCD", 5, 25), ("FLEET", 5, 5), ("FLEET", 20, 10), ("FLEET", 50, 15)]:
            Discount.objects.create(kind=k, threshold=D(th), pct=D(pct))
        for code, name, amt in [("ISSUANCE", "Policy issuance fee", "5"), ("ENDORSEMENT", "Endorsement fee", "3"), ("TRANSFER", "Transfer of ownership fee", "4")]:
            Fee.objects.update_or_create(code=code, defaults=dict(name=name, amount=D(amt)))
        for role, si, disc, fleet, ok in [("AGENT", 25000, 10, 10, False), ("BROKER", 40000, 15, 25, False), ("PRODUCTION_MANAGER", 100000, 25, 100, True),
                                           ("UNDERWRITER", None, None, None, True), ("ADMIN", None, None, None, True)]:
            ApprovalLimit.objects.update_or_create(role=role, defaults=dict(max_sum_insured=si, max_discount_pct=disc, max_fleet_size=fleet, can_approve=ok))
        CommissionRule.objects.all().delete()
        CommissionRule.objects.create(producer_type="AGENT", base_pct=D("10"), overrider_pct=D("1"), profit_share_pct=D("10"), profit_loss_ratio_threshold=D("55"))
        CommissionRule.objects.create(producer_type="BROKER", base_pct=D("12.5"), overrider_pct=D("1"), profit_share_pct=D("10"), profit_loss_ratio_threshold=D("55"))
        CommissionRule.objects.create(producer_type="DIRECT", base_pct=D("0"))

        # reinsurance
        r1, _ = Reinsurer.objects.get_or_create(name="Demo Reinsurer Muscat", defaults=dict(country="Oman", rating="A-"))
        r2, _ = Reinsurer.objects.get_or_create(name="Demo Reinsurer Zurich", defaults=dict(country="Switzerland", rating="AA-"))
        y0 = date.today().year
        for k, y in enumerate((y0 - 2, y0 - 1, y0)):
            if Treaty.objects.filter(reference=f"TR-QS-{y}").exists():
                continue
            f, t = date(y, 1, 1), date(y, 12, 31)
            qs = Treaty.objects.create(reference=f"TR-QS-{y}", name=f"TPL Quota Share {y}", treaty_type="QUOTA", rank=1, reinsurer=r1, effective_from=f, effective_to=t, cession_pct=D("30"))
            qs.covers.set([tp])
            Treaty.objects.create(reference=f"TR-SUR-{y}", name=f"Motor Surplus {y}", treaty_type="SURPLUS", rank=2, reinsurer=r2, effective_from=f, effective_to=t,
                                  retention=D("20000"), capacity=D("100000"))
            x = Treaty.objects.create(reference=f"TR-XOL-{y}", name=f"Motor Excess of Loss {y}", treaty_type="XOL", rank=3, reinsurer=r2, effective_from=f, effective_to=t,
                                      xol_retention=D("2500"), xol_limit=D("50000"), est_gnrpi=D("200000"), rate_pct=D("2.5"), deposit_pct=D("90"), installments=4)
            engine.schedule_mdp(x)
        from core.models import FlexField
        for ent, key, label, typ, ch in [("POLICY", "sales_channel", "Sales channel", "CHOICE", "Walk-in\nOnline\nReferral\nBroker desk"),
                                         ("CLAIM", "police_station", "Police station", "TEXT", "")]:
            FlexField.objects.get_or_create(entity=ent, key=key, defaults=dict(label=label, field_type=typ, choices=ch))

        if not Provider.objects.exists():
            P = Provider.objects.create
            P(provider_type="SURVEYOR", name="Al Batinah Loss Adjusters", governorate="Al Batinah North", specialities="COLLISION,THEFT", preferred=True, phone="2440 1100")
            P(provider_type="SURVEYOR", name="Muscat Surveyors", governorate="Muscat", specialities="COLLISION,FIRE", preferred=True, phone="2420 2200")
            P(provider_type="SURVEYOR", name="Salalah Assessors", governorate="Dhofar", specialities="NATURAL,THEFT", phone="2329 3300")
            for n, g, pref, rate, mkt in [("Gulf Auto Body", "Muscat", True, "6", "8"), ("Sohar Motors Repair", "Al Batinah North", True, "5.5", "8"),
                                          ("Quick Fix Garage", "Muscat", False, "9.5", "8"), ("Nizwa Auto Works", "Ad Dakhiliyah", False, "7", "8"),
                                          ("Dhofar Collision Centre", "Dhofar", True, "6.5", "8")]:
                P(provider_type="GARAGE", name=n, governorate=g, preferred=pref, phone="9100 0000")
            P(provider_type="HOSPITAL", name="Muscat General Hospital", governorate="Muscat", preferred=True, room_cap_per_day=D("45"))
            P(provider_type="HOSPITAL", name="Sohar Medical Centre", governorate="Al Batinah North", preferred=True, room_cap_per_day=D("40"))
            P(provider_type="LEGAL", name="Al Harthy Legal Advisors", governorate="Muscat", preferred=True)

    # ------------------------------------------------------------------ demo business
    def user(self, name, role, ptype="DIRECT", manager=None, first=""):
        u, created = User.objects.get_or_create(username=name, defaults=dict(email=f"{name}@example.com", first_name=first or name.title(), is_staff=role == "ADMIN"))
        u.set_password("demo1234")
        if role == "ADMIN":
            u.is_staff = u.is_superuser = True
        u.save()
        u.groups.set(Group.objects.filter(name=dict(ROLES)[role]))
        pr = u.profile
        pr.producer_type, pr.manager = ptype, manager
        pr.save()
        return u

    def logins(self):
        mk = self.user
        mk("admin", "ADMIN", first="Admin")
        pm = mk("pm1", "PRODUCTION_MANAGER", first="Maryam")
        mk("uw1", "UNDERWRITER", first="Aisha")
        mk("agent1", "AGENT", "AGENT", pm, first="Salim")
        mk("broker1", "BROKER", "BROKER", pm, first="Khalid")
        mk("claims1", "CLAIMS_HANDLER", first="Nasser")
        mk("claimsmgr1", "CLAIMS_MANAGER", first="Fatma")
        mk("finance1", "FINANCE", first="Hamad")
        mk("credit1", "CREDIT_CONTROL", first="Rashid")
        mk("customer1", "CUSTOMER", first="Yousuf")

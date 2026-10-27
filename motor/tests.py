from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from claims import services as cs
from claims.models import Claim, Provider
from core.models import Currency, ROLES, convert
from core.templatetags.money import money
from motor import services
from motor.forms import parse_fleet_file
from motor.models import Cover, Customer, Endorsement, Policy, Product, Proposal
from reinsurance.models import Cession


def make_user(name, role, ptype="DIRECT", manager=None):
    u = User.objects.create_user(name, f"{name}@x.com", "pw")
    u.groups.add(Group.objects.get(name=dict(ROLES)[role]))
    u.profile.producer_type, u.profile.manager = ptype, manager
    u.profile.save()
    return u


def risk_post(i=0, **over):
    d = {"vehicle_id": "", "make": "Toyota", "model": "Corolla", "year": "2022", "plate_code": "AB", "plate_number": "1111",
         "chassis_no": "JTDKB20U093123456", "engine_no": "E123", "color": "White", "vehicle_type": "SALOON", "usage": "PRIVATE",
         "cc": "1600", "seats": "5", "sum_insured": "8000"}
    d.update(over)
    return {f"risks-{i}-{k}": v for k, v in d.items()}


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", "--no-demo", verbosity=0)
        cls.pm = make_user("pm", "PRODUCTION_MANAGER")
        cls.agent = make_user("agent", "AGENT", "AGENT", cls.pm)
        cls.uw = make_user("uw", "UNDERWRITER")
        cls.handler = make_user("handler", "CLAIMS_HANDLER")
        cls.cmgr = make_user("cmgr", "CLAIMS_MANAGER")
        cls.cust_user = make_user("cust", "CUSTOMER")
        cls.omr, cls.usd = Currency.objects.get(code="OMR"), Currency.objects.get(code="USD")
        cls.comp, cls.tpl = Product.objects.get(code="COMP"), Product.objects.get(code="TPL")
        cls.customer = Customer.objects.create(name="Test Customer", civil_id="12345678", phone="9900", date_of_birth=date(1990, 1, 1), email="t@x.com")

    def quote(self, product=None, currency=None, user=None, **vehicle):
        from motor.models import Vehicle, ProposalRisk
        product, currency, user = product or self.comp, currency or self.omr, user or self.agent
        p = Proposal.objects.create(number=services.next_quote_number(), product=product, customer=self.customer, producer=user, currency=currency,
                                    fx_rate=currency.rate_to_base, start_date=date.today(), end_date=date.today() + timedelta(days=364),
                                    valid_until=date.today() + timedelta(days=30))
        v = Vehicle.objects.create(customer=self.customer, make="Toyota", model="Corolla", year=2022, plate_code="AB", plate_number="1",
                                   chassis_no=vehicle.pop("chassis", "JTDKB20U093000001"), engine_no="E1", color="White",
                                   vehicle_type=vehicle.get("vehicle_type", "SALOON"), usage=vehicle.get("usage", "PRIVATE"), cc=1600, seats=5)
        ProposalRisk.objects.create(proposal=p, vehicle=v, sum_insured=D(vehicle.get("si", "8000")))
        services.reprice_proposal(p)
        return p

    def issue(self, p, by=None):
        services.convert_to_proposal(p)
        if p.status == Proposal.PENDING:
            p.status = Proposal.APPROVED
            p.save()
        return services.issue_policy(p, by or p.producer)


class PricingTests(Base):
    def test_tpl_is_flat_tariff_without_discounts(self):
        p = self.quote(product=self.tpl)
        p.ncd_years, p.discount_pct = 5, D("20")
        p.save()
        services.reprice_proposal(p)
        r = p.risks.first()
        self.assertEqual(r.discount_amount, 0)
        self.assertEqual(r.net_premium, D("32.000"))  # private <= 2000cc tariff, no discount, no OD

    def test_comprehensive_applies_ncd_and_mandatory_riders(self):
        p = self.quote()
        p.ncd_years = 3
        p.save()
        services.reprice_proposal(p)
        r = p.risks.first()
        self.assertGreater(r.discount_amount, 0)
        names = {l["code"] for l in r.breakdown["rider_lines"]}
        self.assertTrue({"NATCAT", "PV", "EXCESS_PROT"} <= names)

    def test_multi_currency_quote_converts_back_to_same_base_premium(self):
        omr, usd = self.quote(), self.quote(currency=self.usd, chassis="JTDKB20U093000002")
        # same risk: USD sum insured is entered in USD so scale it to the same OMR value
        from motor.models import ProposalRisk
        r = usd.risks.first()
        r.sum_insured = convert(D("8000"), self.omr, self.usd)
        r.save()
        services.reprice_proposal(usd)
        base_omr = omr.net_premium
        base_usd = convert(usd.net_premium, self.usd, self.omr)
        self.assertAlmostEqual(float(base_omr), float(base_usd), delta=0.05)

    def test_money_filter_uses_symbol_and_decimals(self):
        html = str(money(D("1234.5"), "OMR"))
        self.assertIn("⃄", html)
        self.assertIn("1,234.500", html)
        self.assertIn("1,234.50", str(money(D("1234.5"), "USD")))
        self.assertIn("$", str(money(D("1"), "USD")))


class WorkflowTests(Base):
    def test_agent_within_limit_auto_approved_then_issued(self):
        p = self.quote()
        services.convert_to_proposal(p)
        self.assertEqual(p.status, Proposal.APPROVED)
        pol = services.issue_policy(p, self.agent)
        self.assertTrue(pol.policy_no.startswith("MOT/"))
        self.assertEqual(pol.risks.count(), 1)
        self.assertTrue(pol.commissions.filter(kind="BASE").exists())
        self.assertTrue(pol.cessions.exists())

    def test_over_limit_needs_approval_and_agent_cannot_decide(self):
        p = self.quote(si="60000")
        services.convert_to_proposal(p)
        self.assertEqual(p.status, Proposal.PENDING)
        self.assertFalse(services.can_decide(self.agent, p))
        self.assertTrue(services.can_decide(self.uw, p))
        with self.assertRaises(services.ValidationFailed):
            services.issue_policy(p, self.agent)

    def test_duplicate_chassis_blocked(self):
        from django.db import IntegrityError, transaction
        self.issue(self.quote())
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.quote()          # same chassis again: rejected by the database

    def test_cancellation_refunds_pro_rata_and_claws_back(self):
        pol = self.issue(self.quote())
        eff = date.today() + timedelta(days=182)
        endo = services.apply_endorsement(pol, Endorsement.CANCELLATION, eff, self.agent)
        self.assertLess(endo.premium_adjustment, 0)
        pol.refresh_from_db()
        self.assertEqual(pol.status, Policy.CANCELLED)
        self.assertTrue(pol.commissions.filter(kind="CLAWBACK", amount__lt=0).exists())

    def test_change_of_usage_re_rates_and_additions_increase_premium(self):
        pol = self.issue(self.quote())
        risk = pol.risks.first()
        q = services.quote_endorsement(pol, Endorsement.USAGE, date.today(), risk, new_usage="COMMERCIAL")
        self.assertGreater(q["adjustment"], 0)
        q2 = services.quote_endorsement(pol, Endorsement.ADDITIONS, date.today(), risk, accessories_value=D("1000"))
        self.assertGreater(q2["adjustment"], 0)
        q3 = services.quote_endorsement(pol, Endorsement.COLOR, date.today(), risk, new_color="Black")
        self.assertEqual(q3["adjustment"], 0)

    def test_renewal_prefills_quote(self):
        pol = self.issue(self.quote())
        q = services.renew_policy_as_quote(pol, self.agent)
        self.assertEqual(q.renewal_of, pol)
        self.assertEqual(q.risks.count(), 1)
        self.assertEqual(q.start_date, pol.end_date + timedelta(days=1))


class ReinsuranceTests(Base):
    def test_surplus_ceded_and_fac_blocks_binding(self):
        pol = self.issue(self.quote(si="130000", user=self.uw, chassis="WDCYC7DF4NX999999"))
        self.assertEqual(pol.status, Policy.PENDING_FAC)
        fac = pol.cessions.get(is_facultative=True)
        self.assertEqual(fac.sum_insured_ceded, D("10000.000"))
        sur = pol.cessions.get(treaty_type="SURPLUS")
        self.assertEqual(sur.sum_insured_ceded, D("100000.000"))
        from reinsurance.engine import confirm_facultative
        from reinsurance.models import Reinsurer
        confirm_facultative(fac, Reinsurer.objects.first())
        pol.refresh_from_db()
        self.assertEqual(pol.status, Policy.ACTIVE)

    def test_quota_share_on_tp_only(self):
        pol = self.issue(self.quote(product=self.tpl, chassis="JTDKB20U093000003"))
        qs = pol.cessions.get(treaty_type="QUOTA")
        self.assertEqual(qs.cession_pct, D("30.000"))
        self.assertGreater(qs.premium_ceded, 0)

    def test_treaty_modification_creates_version_and_keeps_history(self):
        from reinsurance.engine import modify_treaty
        from reinsurance.models import Treaty
        t = Treaty.objects.get(reference=f"TR-SUR-{date.today().year}")
        new, warnings = modify_treaty(t, "Raise retention", retention=D("25000"))
        t.refresh_from_db()
        self.assertEqual(new.version, 2)
        self.assertEqual(t.superseded_by, new)
        self.assertEqual(t.retention, D("20000.000"))

    def test_mdp_no_refund(self):
        from reinsurance.engine import mdp_adjustment
        from reinsurance.models import Treaty
        x = Treaty.objects.get(treaty_type="XOL", effective_from__year=date.today().year)
        self.assertEqual(x.mdp, D("4500.000"))
        add, _ = mdp_adjustment(x, D("100000"))
        self.assertEqual(add, 0)
        add, earned = mdp_adjustment(x, D("240000"))
        self.assertEqual(add, D("1500.000"))


class ClaimTests(Base):
    def setUp(self):
        self.pol = self.issue(self.quote())
        self.risk = self.pol.risks.first()

    def register(self, **kw):
        data = dict(date_of_loss=date.today(), loss_type="COLLISION", cause_of_loss="x", estimated_damage=D("1000"), driver_age=40)
        data.update(kw)
        return cs.register_claim(self.pol, self.risk, self.handler, **data)

    def ready_to_close(self, c):
        """Garage invoice recorded and settled, excess collected: what closure now requires."""
        g = Provider.objects.filter(provider_type="GARAGE").first() or Provider.objects.create(provider_type="GARAGE", name="G1", preferred=True)
        e = cs.check_estimate(c, g, "FIXED", D("0"), D("0"), c.approved_repair_cost or D("1"), 5)
        cs.record_invoice(e, "INV-1", e.total, date.today())
        cs.settle_estimate(e)
        cs.record_excess(c, c.excess_applied, date.today())

    def test_excess_components(self):
        c = self.register(driver_age=22, undisclosed_driver=True)
        self.assertEqual(c.excess_applied, D("300.000"))  # 50 standard + 100 young + 150 undisclosed
        c2 = self.register()
        self.assertEqual(c2.excess_applied, D("50.000"))

    def test_loss_outside_period_rejected(self):
        with self.assertRaises(cs.ClaimError):
            self.register(date_of_loss=date.today() - timedelta(days=5))

    def test_net_payable_and_70_30_payment_rule(self):
        c = self.register()
        cs.approve_claim(c, D("1000"), self.handler)
        self.assertEqual(c.net_payable, D("950.000"))
        with self.assertRaises(cs.ClaimError):
            cs.pay_claim(c, "FINAL", "Garage")
        cs.pay_claim(c, "INTERIM", "Garage")
        self.assertEqual(c.total_paid, D("665.000"))
        with self.assertRaises(cs.ClaimError):  # repair not completed
            cs.pay_claim(c, "FINAL", "Garage")
        cs.complete_repair(c, c.file_finalised_on + timedelta(days=10))
        cs.pay_claim(c, "FINAL", "Garage")
        c.refresh_from_db()
        self.assertEqual(c.total_paid, D("950.000"))
        self.assertEqual(c.status, Claim.SETTLED)
        with self.assertRaises(cs.ClaimError):          # no invoice / settlement / excess recorded yet
            cs.close_claim(c)
        self.ready_to_close(c)
        cs.close_claim(c)
        self.assertEqual(c.status, Claim.CLOSED)

    def test_repair_delay_beyond_30_days_is_flagged(self):
        c = self.register()
        cs.approve_claim(c, D("1000"), self.handler)
        cs.complete_repair(c, c.file_finalised_on + timedelta(days=36))
        self.assertEqual(c.repair_delay_days, 6)

    def test_large_settlement_needs_claims_manager(self):
        c = self.register(estimated_damage=D("9000"))
        with self.assertRaises(cs.ClaimError):
            cs.approve_claim(c, D("9000"), self.handler)
        cs.approve_claim(c, D("9000"), self.cmgr)

    def test_subrogation_refunds_excess_and_blocks_close_until_resolved(self):
        c = self.register(third_party_liable=True)
        cs.approve_claim(c, D("1000"), self.handler)
        cs.pay_claim(c, "FULL", "Customer")
        with self.assertRaises(cs.ClaimError):
            cs.close_claim(c)
        sub = cs.record_subrogation_recovery(c, c.subrogation.reserve_amount)
        self.assertEqual(sub.recovery_status, "RECOVERED")
        self.assertGreater(sub.excess_refunded, 0)

    def test_garage_rating_updates_average(self):
        g = Provider.objects.create(provider_type="GARAGE", name="G", preferred=True)
        c = self.register()
        cs.approve_claim(c, D("1000"), self.handler)
        c.garage = g
        c.save()
        cs.complete_repair(c, c.file_finalised_on + timedelta(days=5), rework=False, customer_score=5)
        g.refresh_from_db()
        self.assertEqual(g.average_score, D("5.00"))

    def test_non_preferred_garage_above_market_flagged(self):
        g = Provider.objects.create(provider_type="GARAGE", name="G2", preferred=False)
        est = cs.check_estimate(self.register(), g, "FIXED", D("0"), D("0"), D("5000"), 5)
        self.assertTrue(est.exceeds_market)


class FleetUploadTests(Base):
    def test_row_wise_errors(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        csv = ("make,model,year,plate_code,plate_number,chassis_no,engine_no,color,vehicle_type,usage,cc,seats,sum_insured\n"
               "Toyota,Hilux,2022,AB,1,JTFDE626000123456,E1,White,PICKUP,Commercial,2700,5,9000\n"
               "Toyota,Hilux,2022,AB,2,JTFDE626000123456,E2,White,Pickup,Commercial,2700,5,9000\n"
               "Ford,Ranger,1900,AB,3,BAD,E3,Blue,Pickup,Commercial,2200,5,8000\n")
        rows, errors = parse_fleet_file(SimpleUploadedFile("f.csv", csv.encode()))
        self.assertEqual(len(rows), 1)
        self.assertEqual({e[0] for e in errors}, {3, 4})
        self.assertEqual(rows[0]["vehicle_type"], "PICKUP")


class ViewTests(Base):
    def test_quote_form_renders_icon_pickers(self):
        self.client.force_login(self.agent)
        r = self.client.get(reverse("quote_create"))
        self.assertContains(r, 'class="icon-card"', count=None)
        for t in ("SALOON", "SUV", "PICKUP", "VAN", "BUS", "TRUCK", "MOTORCYCLE", "TRAILER", "PRIVATE", "TAXI", "RENTAL"):
            self.assertContains(r, f'value="{t}"')

    def test_full_quote_post_preview_and_submit(self):
        self.client.force_login(self.agent)
        data = {
            "customer": "", "new_customer_type": "INDIVIDUAL", "new_name": "Ali Test", "new_civil_id": "55555555", "new_phone": "9955 5555",
            "new_dob": "1985-05-05", "new_email": "ali@x.com", "product": self.comp.pk, "currency": "AED", "start_date": date.today().isoformat(),
            "end_date": (date.today() + timedelta(days=364)).isoformat(), "ncd_years": "1", "discount_pct": "0",
            "risks-TOTAL_FORMS": "1", "risks-INITIAL_FORMS": "0", "risks-MIN_NUM_FORMS": "1", "risks-MAX_NUM_FORMS": "1000", **risk_post(0, sum_insured="30000"),
        }
        prev = self.client.post(reverse("quote_preview"), data).json()
        self.assertTrue(prev["ok"], prev)
        self.assertEqual(prev["currency"], "AED")
        r = self.client.post(reverse("quote_create"), data, follow=True)
        p = Proposal.objects.get(customer__name="Ali Test")
        self.assertEqual(p.currency.code, "AED")
        self.assertEqual(p.risks.first().vehicle.vehicle_type, "SALOON")
        self.assertEqual(f"{p.total_payable:,.2f}", prev["total"])
        # 30,000 AED is ~3,141 OMR: within agent authority, so converting approves it
        self.client.post(reverse("quote_submit", args=[p.pk]))
        p.refresh_from_db()
        self.assertEqual(p.status, Proposal.APPROVED)
        self.client.post(reverse("issue", args=[p.pk]), {"basis": "CASH", "method": "CASH"})
        self.assertTrue(Policy.objects.filter(proposal=p).exists())

    def test_invalid_chassis_shows_error(self):
        self.client.force_login(self.agent)
        data = {"customer": self.customer.pk, "product": self.comp.pk, "currency": "OMR", "start_date": date.today().isoformat(),
                "end_date": (date.today() + timedelta(days=364)).isoformat(), "ncd_years": "0",
                "risks-TOTAL_FORMS": "1", "risks-INITIAL_FORMS": "0", "risks-MIN_NUM_FORMS": "1", "risks-MAX_NUM_FORMS": "1000", **risk_post(0, chassis_no="NOTAVIN")}
        r = self.client.post(reverse("quote_create"), data)
        self.assertContains(r, "17 characters")

    def test_customer_sees_only_own_policies(self):
        pol = self.issue(self.quote())
        self.cust_user.profile.customer = self.customer
        self.cust_user.profile.save()
        other = Customer.objects.create(name="Other", civil_id="99999999", phone="1")
        self.client.force_login(self.cust_user)
        self.assertEqual(self.client.get(reverse("policy_detail", args=[pol.pk])).status_code, 200)
        pol.customer = other
        pol.save()
        self.assertEqual(self.client.get(reverse("policy_detail", args=[pol.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("treaty_list")).status_code, 403)

    def test_policy_pdf_and_dashboard_render(self):
        pol = self.issue(self.quote())
        self.client.force_login(self.agent)
        r = self.client.get(reverse("policy_pdf", args=[pol.pk]))
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_reminders_are_idempotent(self):
        pol = self.issue(self.quote())
        Policy.objects.filter(pk=pol.pk).update(end_date=date.today() + timedelta(days=7))
        call_command("send_renewal_reminders", verbosity=0)
        call_command("send_renewal_reminders", verbosity=0)
        self.assertEqual(pol.reminders.count(), 1)

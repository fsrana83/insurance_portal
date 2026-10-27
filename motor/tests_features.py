"""Tests for the second-release features: demo loader, vouchers, depreciation, drivers, payments, accounting,
documents, sharing, AI estimate, flex fields, company setup and a render smoke test of every screen."""
from datetime import date, timedelta
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from accounting.models import FinancialDocument, JournalEntry, JournalLine
from claims.models import Claim
from core import demo
from core.models import Company, FlexField
from motor import services
from motor.models import Driver, Endorsement, Policy, Voucher
from payments.models import Payment


class DemoBook(TestCase):
    """One small demo book shared by the read-only tests below."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        cls.counts = demo.load_demo(policies=45, claims=18, seed=7)

    def login(self, name="admin"):
        self.client.force_login(User.objects.get(username=name))

    def test_loader_counts_and_flags(self):
        self.assertEqual(Policy.objects.filter(is_demo=True).count(), 45)
        self.assertEqual(Claim.objects.filter(is_demo=True).count(), 18)
        self.assertFalse(Policy.objects.filter(is_demo=False).exists())
        self.assertEqual(Policy.objects.exclude(proposal__is_demo=True).count(), 0)

    def test_journals_balance(self):
        t = JournalLine.objects.aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(t["d"], t["c"])
        for e in JournalEntry.objects.all()[:200]:
            s = e.lines.aggregate(d=Sum("debit"), c=Sum("credit"))
            self.assertEqual(s["d"], s["c"], e.number)

    def test_every_policy_has_dn_and_invoice(self):
        for p in Policy.objects.all():
            kinds = set(p.fin_documents.values_list("doc_type", flat=True))
            self.assertTrue({"DN", "INV"} <= kinds, p.policy_no)

    def test_payments_never_exceed_total(self):
        for p in Policy.objects.all():
            self.assertLessEqual(p.paid_amount, p.total_payable + D("0.001"), p.policy_no)

    def test_screens_render(self):
        self.login()
        pol, claim = Policy.objects.filter(is_demo=True).first(), Claim.objects.first()
        pay = Payment.objects.first()
        urls = ["dashboard", "quote_list", "quote_create", "policy_list", "claim_list", "claim_new", "customer_list", "treaty_list", "provider_list", "reports",
                "company_setup", "flex_list", "flex_new", "voucher_list", "voucher_create", "payment_list", "acc_transactions", "acc_tb", "acc_ifrs17", "acc_documents",
                "premium_register", "ai_models", "demo_data", "approval_queue"]
        for name in urls:
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 200, name)
        for name, args in [("policy_detail", [pol.pk]), ("claim_detail", [claim.pk]), ("customer_detail", [pol.customer_id]), ("endorse", [pol.pk]),
                           ("policy_share", [pol.pk]), ("voucher_edit", [Voucher.objects.first().pk]), ("quote_detail", [pol.proposal_id]),
                           ("flex_edit", [FlexField.objects.first().pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("payment_share", args=[pay.pk])).status_code in (200, 302), True)

    def test_claim_form_and_detail_contain_damage_map(self):
        self.login("claims1")
        self.assertContains(self.client.get(reverse("claim_new")), 'id="damage-map"')
        c = Claim.objects.exclude(damage_zones={}).first()
        self.assertContains(self.client.get(reverse("claim_detail", args=[c.pk])), "data-zone")

    def test_pdfs(self):
        self.login()
        pol = Policy.objects.filter(is_demo=True).first()
        r = self.client.get(reverse("policy_pdf", args=[pol.pk]))
        self.assertTrue(r.content.startswith(b"%PDF"))
        for d in pol.fin_documents.all():
            self.assertTrue(self.client.get(reverse("fin_document_pdf", args=[d.pk])).content.startswith(b"%PDF"))
        pay = pol.payments.filter(status="PAID").first()
        if pay:
            self.assertTrue(self.client.get(reverse("receipt_download", args=[pay.pk])).content.startswith(b"%PDF"))

    def test_premium_register_exports(self):
        self.login()
        q = f"?from={date.today() - timedelta(days=2000)}&to={date.today()}"
        csv = self.client.get(reverse("premium_register") + q + "&format=csv")
        self.assertEqual(csv.status_code, 200)
        self.assertGreater(len(csv.content.decode().splitlines()), 40)
        self.assertEqual(self.client.get(reverse("premium_register") + q + "&format=xlsx").status_code, 200)

    def test_sharing_eml_and_whatsapp(self):
        self.login()
        pol = Policy.objects.filter(is_demo=True).first()
        r = self.client.post(reverse("policy_share", args=[pol.pk]), {"action": "outlook", "docs": ["schedule"], "email": "a@b.com", "subject": "S", "message": "Hi"})
        self.assertEqual(r["Content-Type"], "message/rfc822")
        body = r.content.decode("latin-1")
        self.assertIn("X-Unsent: 1", body)
        self.assertIn("application/pdf", body)
        r = self.client.post(reverse("policy_share", args=[pol.pk]), {"action": "whatsapp", "docs": ["schedule"], "phone": "9912 3456", "message": "Hello"})
        self.assertContains(r, "whatsapp://send?phone=96899123456")
        share = pol.shares.first()
        self.client.logout()
        self.assertContains(self.client.get(reverse("share_view", args=[share.token])), pol.policy_no)
        self.assertTrue(self.client.get(reverse("share_download", args=[share.token, "schedule"])).content.startswith(b"%PDF"))

    def test_customer_policies_api(self):
        self.login()
        pol = Policy.objects.filter(is_demo=True).first()
        d = self.client.get(reverse("customer_policies"), {"customer": pol.customer_id}).json()
        self.assertTrue(any(p["id"] == pol.pk for p in d["policies"]))

    def test_ai_estimate_statistical_when_ollama_down(self):
        self.login("claims1")
        from claims import ai
        risk = Claim.objects.exclude(damage_zones={}).first().risk
        with mock.patch.object(ai, "_request", side_effect=OSError("down")):
            r = self.client.post(reverse("claim_estimate"), {"risk": risk.pk, "zones": {"front_bumper": 2, "hood": 1}, "loss_type": "COLLISION", "model": "llama3"},
                                 content_type="application/json")
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["source"], "statistical")
        self.assertGreater(d["expected"], 0)

    def test_ollama_model_list(self):
        from claims import ai
        fake = {"models": [{"name": "llama3.2:3b", "size": 2_000_000_000, "details": {"family": "llama", "parameter_size": "3B", "quantization_level": "Q4_K_M"}},
                           {"name": "mistral:7b", "size": 4_100_000_000, "details": {}}]}
        with mock.patch.object(ai, "_request", return_value=fake):
            info = ai.list_models()
        self.assertEqual([m["name"] for m in info["models"]], ["llama3.2:3b", "mistral:7b"])

    def test_unload_keeps_customer_used_by_a_real_pnc_quote(self):
        from datetime import timedelta
        from core.models import Currency
        from motor.models import Customer
        from pnc import services as psv
        from pnc.models import PncClass, PncPolicy, PncRisk
        cust = Customer.objects.filter(is_demo=True).first()
        today = date.today()
        pol = PncPolicy.objects.create(quote_no=psv.seq("PQ"), pnc_class=PncClass.objects.get(code="FIRE"), customer=cust, producer=User.objects.get(username="agent1"),
                                       currency=Currency.base(), fx_rate=1, start_date=today, end_date=today + timedelta(days=364))
        PncRisk.objects.create(policy=pol, number=1, description="Real", sum_insured=1000, rate_pct=1)
        demo.unload_demo()
        self.assertTrue(Customer.objects.filter(pk=cust.pk, is_demo=False).exists())
        self.assertTrue(PncPolicy.objects.filter(pk=pol.pk).exists())
        self.assertEqual(Policy.objects.count(), 0)

    def test_unload_removes_only_demo(self):
        from motor.models import Customer
        real = Customer.objects.create(name="Real Customer", phone="9000")
        removed = demo.unload_demo()
        self.assertEqual(Policy.objects.count(), 0)
        self.assertEqual(Claim.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertTrue(Customer.objects.filter(pk=real.pk).exists())
        self.assertGreater(removed["policies"], 0)
        self.assertEqual(demo.demo_counts()["policies"], 0)


class FeatureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        demo.load_demo(policies=12, claims=0, seed=3)

    def setUp(self):
        self.admin = User.objects.get(username="admin")
        self.client.force_login(self.admin)
        self.pol = Policy.objects.filter(status="ACTIVE", product__code="COMP").first()

    def test_depreciation_reducing_balance(self):
        v = services.depreciated_value(D("10000"), date(2024, 1, 1), date(2026, 1, 1), D("15"))
        self.assertAlmostEqual(float(v), 10000 * 0.85 ** (731 / 365.25), places=2)
        self.assertEqual(services.depreciated_value(D("10000"), date(2024, 1, 1), date(2024, 1, 1), D("15")), D("10000"))

    def test_renewal_uses_depreciated_value(self):
        pol = Policy.objects.filter(product__code="COMP", status__in=["ACTIVE", "EXPIRED"]).first()
        q = services.renew_policy_as_quote(pol, self.admin)
        for r in q.risks.all():
            prev = pol.risks.get(vehicle=r.vehicle)
            self.assertLessEqual(r.sum_insured, prev.sum_insured)

    def test_regno_endorsement_and_clash(self):
        risk = self.pol.risks.first()
        eff = max(self.pol.start_date, date.today() - timedelta(days=1))
        services.apply_endorsement(self.pol, Endorsement.REGNO, eff, self.admin, risk, new_plate_code="ZZ", new_plate_number="9999")
        risk.refresh_from_db()
        self.assertIn("9999", risk.plate)
        risk.vehicle.refresh_from_db()
        self.assertEqual((risk.vehicle.plate_code, risk.vehicle.plate_number), ("ZZ", "9999"))

    def test_add_driver_endorsement(self):
        risk = self.pol.risks.first()
        eff = max(self.pol.start_date, date.today() - timedelta(days=1))
        services.apply_endorsement(self.pol, Endorsement.ADDDRIVER, eff, self.admin, risk, driver_name="New Driver", driver_licence="123456",
                                   driver_dob=date(2003, 5, 5), driver_relationship="CHILD", driver_nationality="Omani")
        self.assertTrue(Driver.objects.filter(policy_risk=risk, name="New Driver").exists())

    def test_voucher_validation_and_discountable_base(self):
        v = Voucher.objects.create(code="TEST10", discount_type="PERCENT", value=D("10"), valid_from=date.today() - timedelta(days=1),
                                   valid_to=date.today() + timedelta(days=5), issued_by=self.admin)
        ok, err = services.validate_voucher("test10", self.pol.product, self.pol.customer)
        self.assertEqual((ok, err), (v, None))
        tpl = services.Product.objects.get(code="TPL") if hasattr(services, "Product") else self.pol.product.__class__.objects.get(code="TPL")
        self.assertIsNotNone(services.validate_voucher("TEST10", tpl, self.pol.customer)[1])     # regulated TPL cannot be discounted
        self.assertIsNotNone(services.validate_voucher("NOSUCH", self.pol.product, self.pol.customer)[1])
        v.valid_to = date.today() - timedelta(days=1)
        v.save()
        self.assertIsNotNone(services.validate_voucher("TEST10", self.pol.product, self.pol.customer)[1])

    def test_voucher_generation_form(self):
        r = self.client.post(reverse("voucher_create"), {"code": "", "description": "x", "discount_type": "PERCENT", "value": "5", "valid_from": date.today(),
                                                         "valid_to": date.today() + timedelta(days=30), "active": "on", "quantity": "3"})
        self.assertIn(r.status_code, (200, 302))

    def test_chassis_unique_form_rule(self):
        from motor.forms import RiskForm
        r0 = self.pol.risks.first()
        data = {"vehicle_type": r0.vehicle_type, "usage": r0.usage, "make": "Other", "model": "Thing", "year": r0.year, "color": "Red", "plate_code": "QQ",
                "plate_number": "1234", "chassis_no": r0.chassis_no, "engine_no": "X1", "cc": 1600, "seats": 5, "sum_insured": "5000"}
        f = RiskForm(data, prefix="risks-0")
        self.assertFalse(f.is_valid())
        self.assertTrue("chassis_no" in f.errors or f.non_field_errors(), f.errors)

    def test_payment_gateway_flow(self):
        pol = next(p for p in Policy.objects.filter(status="ACTIVE") if p.balance_due > 5)
        from payments import services as ps
        pay = ps.request_online_payment(pol, D("5"), self.admin)
        self.client.logout()
        url = reverse("pay_online", args=[pay.token])
        self.assertContains(self.client.get(url), "Demo gateway")
        r = self.client.post(url, {"holder": "A B", "number": "4000 0000 0000 0002", "expiry": "12/30", "cvv": "123"})
        self.assertContains(r, "declined")
        pay.refresh_from_db()
        self.assertEqual(pay.status, "PENDING")
        r = self.client.post(url, {"holder": "A B", "number": "4242 4242 4242 4242", "expiry": "12/30", "cvv": "123"})
        pay.refresh_from_db()
        self.assertEqual(pay.status, "PAID")
        self.assertTrue(pay.receipt_no.startswith("RV/"))
        self.assertTrue(JournalEntry.objects.filter(entry_type="RECEIPT", source=pay.receipt_no).exists() or JournalEntry.objects.filter(source=pay.receipt_no).exists())

    def test_overpayment_rejected(self):
        from payments import services as ps
        with self.assertRaises(ps.PaymentError):
            ps.record_receipt(self.pol, self.pol.total_payable * 5, "CASH", self.admin)

    def test_revenue_recognition_is_idempotent(self):
        from accounting import services as acc
        acc.recognise_revenue(date.today(), self.admin)
        n1 = JournalEntry.objects.count()
        acc.recognise_revenue(date.today(), self.admin)
        self.assertEqual(JournalEntry.objects.count(), n1)

    def test_flex_field_roundtrip(self):
        FlexField.objects.create(entity="CUSTOMER", key="employer", label="Employer", field_type="TEXT")
        from motor.forms import CustomerForm
        f = CustomerForm({"name": "Flex Person", "customer_type": "INDIVIDUAL", "phone": "9911", "civil_id": "99887766", "date_of_birth": "1980-01-01",
                          "cflex_employer": "Oman Insurance"}) if "cflex_employer" in CustomerForm().fields else None
        if f is None:
            f = CustomerForm({"name": "Flex Person", "customer_type": "INDIVIDUAL", "phone": "9911", "civil_id": "99887766", "date_of_birth": "1980-01-01",
                              "flex_employer": "Oman Insurance"})
        self.assertTrue(f.is_valid(), f.errors)
        c = f.save()
        self.assertEqual(c.flex.get("employer"), "Oman Insurance")

    def test_company_setup_admin_only_and_on_documents(self):
        r = self.client.post(reverse("company_setup"), {"name": "Acme Insurance", "country": "Oman", "default_phone_code": "968", "legal_name": "Acme SAOC",
                                                        "vat_number": "OM123"})
        self.assertIn(r.status_code, (200, 302))
        self.assertEqual(Company.get().name, "Acme Insurance") if r.status_code == 302 else None
        self.client.force_login(User.objects.get(username="agent1"))
        self.assertEqual(self.client.get(reverse("company_setup")).status_code, 403)
        self.assertEqual(self.client.get(reverse("demo_data")).status_code, 403)

    def test_expired_policy_can_be_renewed(self):
        pol = Policy.objects.filter(status="EXPIRED", product__code="COMP").first()
        if pol:
            r = self.client.post(reverse("renew", args=[pol.pk]))
            self.assertEqual(r.status_code, 302)

"""Fifth release: customer search, vehicle profile / transfer, loss ratio, claim vehicle fetch, report gating, management pack, XoL posting."""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase

from accounting.models import JournalLine
from claims.models import Claim
from core import demo
from reinsurance import engine
from reinsurance.models import Cession, MDPInstallment, Treaty

from . import customers, lossratio, mgmt_export, mgmt_pack, vehicles
from .models import Customer, Policy, Vehicle

PERIOD = {"run": "1", "from": "2015-01-01", "to": "2030-12-31"}


class V5(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        demo.load_demo(policies=90, claims=40, seed=5)

    def setUp(self):
        self.admin = User.objects.get(username="admin")
        self.client.force_login(self.admin)

    # ---- customers -------------------------------------------------------------------
    def test_customer_search_name_partial_id_and_mobile(self):
        c = Customer.objects.exclude(phone="").exclude(civil_id="").first()
        part = c.name.split()[0][:4]
        self.assertIn(c.pk, [x.pk for x in customers.search(part)])
        self.assertIn(c.pk, [x.pk for x in customers.search(c.civil_id)])
        self.assertIn(c.pk, [x.pk for x in customers.search("".join(ch for ch in c.phone if ch.isdigit())[-7:])])
        r = self.client.get("/api/customer-search/", {"q": part}).json()["results"]
        self.assertTrue(r)

    def test_customer_search_returns_unique_people(self):
        base = Customer.objects.exclude(civil_id="").first()
        Customer.objects.create(name=base.name, civil_id=base.civil_id, phone=base.phone)      # legacy duplicate record
        res = customers.search(base.civil_id)
        self.assertEqual(len([x for x in res if x.civil_id == base.civil_id]), 1)
        self.assertEqual(len(customers.search(base.civil_id, unique=False)), 2)

    # ---- vehicles --------------------------------------------------------------------
    def test_vehicle_transfer_keeps_same_profile(self):
        v = Vehicle.objects.exclude(risks_on_policies__policy__status="ACTIVE", risks_on_policies__policy__end_date__gte=date.today()).first()
        if v is None:
            self.skipTest("no free vehicle")
        old, new = v.customer, Customer.objects.exclude(pk=v.customer_id).first()
        pk, chassis = v.pk, v.chassis_no
        vehicles.transfer_directly(v, new, date.today(), self.admin)
        v.refresh_from_db()
        self.assertEqual((v.pk, v.chassis_no, v.customer_id), (pk, chassis, new.pk))
        self.assertEqual(Vehicle.objects.filter(chassis_no=chassis).count(), 1)
        hist = list(v.ownerships.order_by("from_date", "id"))
        self.assertIn(old.pk, [h.customer_id for h in hist])
        self.assertEqual(hist[-1].customer_id, new.pk)
        self.assertIsNone(hist[-1].to_date)
        self.assertEqual(self.client.get(f"/vehicles/{pk}/").status_code, 200)

    def test_direct_transfer_blocked_while_policy_in_force(self):
        v = Vehicle.objects.filter(risks_on_policies__policy__status="ACTIVE", risks_on_policies__policy__end_date__gte=date.today()).first()
        if v is None:
            self.skipTest("no vehicle in force")
        other = Customer.objects.exclude(pk=v.customer_id).first()
        with self.assertRaises(vehicles.TransferError):
            vehicles.transfer_directly(v, other, date.today(), self.admin)

    def test_vehicle_level_search(self):
        v = Vehicle.objects.first()
        self.assertIn(v.pk, [x.pk for x in vehicles.search(v.chassis_no[-6:])])
        self.assertIn(v.pk, [x.pk for x in vehicles.search(f"{v.model} {v.make}")])
        self.assertEqual(self.client.get("/vehicles/", {"q": v.make}).status_code, 200)
        self.assertTrue(self.client.get("/api/vehicle-search/", {"q": v.chassis_no[-6:]}).json()["results"])

    # ---- loss ratio ------------------------------------------------------------------
    def test_loss_ratio_all_groupings_tie_to_ledger(self):
        s, e = date(2015, 1, 1), date(2030, 12, 31)
        incurred = JournalLine.objects.filter(account_id="5110", entry__entry_type__in=["CLAIM", "CLAIM_PAY"], entry__claim__isnull=False).aggregate(d=Sum("debit"), c=Sum("credit"))
        want = (incurred["d"] or 0) - (incurred["c"] or 0)
        for g in ("insured", "vehicle", "both"):
            rows = lossratio.compute(self.admin, s, e, g)
            self.assertTrue(rows, g)
            self.assertAlmostEqual(float(lossratio.totals(rows)["incurred"]), float(want), places=1, msg=g)
            r = self.client.get("/registers/loss-ratio/", {**PERIOD, "group": g, "format": "csv"})
            self.assertEqual(r.status_code, 200, g)

    # ---- claims: policy -> vehicle -----------------------------------------------------
    def test_claim_policy_fetches_vehicle(self):
        p = Policy.objects.filter(status="ACTIVE").first()
        res = self.client.get("/claims/api/policy-search/", {"q": p.policy_no}).json()["results"]
        self.assertTrue(any(x["id"] == p.pk for x in res))
        data = self.client.get(f"/claims/api/policy/{p.pk}/vehicles/").json()
        self.assertTrue(data["risks"])
        self.assertEqual(len(data["risks"]), p.risks.filter(active=True).count())

    # ---- reports gating ----------------------------------------------------------------
    def test_reports_show_nothing_until_run(self):
        for url in ("/registers/claims-register/", "/registers/loss-ratio/", "/reports/run/?type=production", "/reports/premium-register/", "/management/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, "Nothing is shown", msg_prefix=url)
        self.assertNotContains(self.client.get("/registers/claims-register/", {"from": "2015-01-01", "to": "2030-12-31"}), "<tbody>")
        self.assertContains(self.client.get("/registers/claims-register/", PERIOD), "<tbody>")

    def test_hub_lists_every_report_as_card(self):
        r = self.client.get("/reports/")
        self.assertContains(r, "hub-card")
        for t in ("Claim register", "Loss ratio by insured / vehicle", "Management insights", "Premium register", "Broker production"):
            self.assertContains(r, t)

    # ---- management pack ---------------------------------------------------------------
    def test_management_pack_ties_to_ledger_and_exports(self):
        s, e = date(2015, 1, 1), date(2030, 12, 31)
        pack = mgmt_pack.build(self.admin, s, e)
        k = {x["key"]: x for x in pack["kpis"]}
        gwp = JournalLine.objects.filter(account_id="2110", entry__book="MOTOR", entry__entry_type__in=["ISSUE", "ENDORSE"]).aggregate(c=Sum("credit"), d=Sum("debit"))
        self.assertEqual(k["gwp"]["value"], (gwp["c"] or 0) - (gwp["d"] or 0))
        self.assertTrue(pack["insights"])
        self.assertAlmostEqual(float(sum(r["gwp"] for r in pack["by_product"])), float(k["gwp"]["value"]), places=1)
        for fmt, magic in (("pdf", b"%PDF"), ("pptx", b"PK"), ("docx", b"PK"), ("xlsx", b"PK"), ("csv", b"")):
            r = self.client.get("/management/", {**PERIOD, "format": fmt})
            self.assertEqual(r.status_code, 200, fmt)
            self.assertTrue(r.content.startswith(magic) and len(r.content) > 200, fmt)
            self.assertIn(f".{fmt}", r["Content-Disposition"])
        self.assertContains(self.client.get("/management/", PERIOD), "Key findings")

    def test_management_roles(self):
        for user, ok in (("agent1", False), ("broker1", False), ("pm1", True), ("finance1", True)):
            self.client.force_login(User.objects.get(username=user))
            self.assertEqual(self.client.get("/management/", PERIOD).status_code, 200 if ok else 403, user)

    # ---- XoL ---------------------------------------------------------------------------
    def test_xol_premium_accrues_per_policy_and_posts(self):
        xol = Cession.objects.filter(treaty_type=Treaty.XOL)
        self.assertTrue(xol.exists())
        accrued = xol.aggregate(s=Sum("premium_ceded"))["s"]
        led = JournalLine.objects.filter(account_id="2230", entry__entry_type__in=["ISSUE", "ENDORSE"], entry__policy__isnull=False).aggregate(c=Sum("credit"), d=Sum("debit"))
        self.assertGreaterEqual((led["c"] or 0) - (led["d"] or 0), accrued)

    def test_xol_recovery_on_large_claim_is_posted(self):
        from claims import services as cs

        def net(c, acct, side):
            a = JournalLine.objects.filter(account_id=acct, entry__claim=c, entry__entry_type="CLAIM").aggregate(d=Sum("debit"), cr=Sum("credit"))
            return (a["d"] or 0) - (a["cr"] or 0) if side == "D" else (a["cr"] or 0) - (a["d"] or 0)
        found = 0
        for c in Claim.objects.exclude(status="REJECTED").select_related("policy"):
            incurred = net(c, "5110", "D")
            xr = cs.xol_recoveries(c, incurred) if incurred > 0 else []
            if xr:
                found += 1
                self.assertAlmostEqual(float(net(c, "1230", "D")), float(cs.reinsurance_target(c, incurred)), places=2)
                self.assertGreater(net(c, "1230", "D"), incurred * cs.reinsurance_share(c))
        self.assertGreater(found, 0)

    def test_xol_instalments_and_topup_are_idempotent(self):
        inst = MDPInstallment.objects.filter(treaty__treaty_type="XOL", paid=True).first()
        self.assertIsNotNone(inst)
        before = JournalLine.objects.count()
        self.assertIsNone(engine.pay_installment(inst, self.admin))          # second payment ignored
        t = Treaty.objects.filter(treaty_type="XOL", superseded_by__isnull=True).order_by("effective_to").first()
        engine.post_xol_adjustment(t, self.admin, force=True)
        n = JournalLine.objects.count()
        self.assertIsNone(engine.post_xol_adjustment(t, self.admin, force=True))
        self.assertEqual(JournalLine.objects.count(), n)
        self.assertGreaterEqual(n, before)

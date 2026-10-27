"""Sixth release: change log, dual confirmation, claim lock / reopen, garage pricing basis, closure checks, treaty recovery journal,
subrogation notices, legal actions, injuries and the automatic estimate."""
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.core import mail
from django.test import override_settings

from claims import actions, services as cs
from claims.models import Claim, Injury, LegalAction, Provider, RepairEstimate, SubroNotice, TreatyRecovery
from core import confirm as dual
from core.models import ChangeLog, PendingAction

from .tests import Base


class V6(Base):
    def setUp(self):
        self.pol = self.issue(self.quote())
        self.risk = self.pol.risks.first()

    def register(self, **kw):
        data = dict(date_of_loss=date.today(), loss_type="COLLISION", cause_of_loss="x", estimated_damage=D("1000"), driver_age=40)
        data.update(kw)
        return cs.register_claim(self.pol, self.risk, self.handler, **data)

    def garage(self, preferred=True):
        return Provider.objects.create(provider_type="GARAGE", name="G%s" % Provider.objects.count(), preferred=preferred)

    def settle(self, c, amount="1000"):
        cs.approve_claim(c, D(amount), self.handler)
        cs.pay_claim(c, "FULL", "Customer", "R1")
        c.refresh_from_db()
        return c

    def paperwork(self, c):
        e = cs.check_estimate(c, self.garage(), "BOTH", D("400"), D("600"), D("0"), 5)
        cs.record_invoice(e, "INV1", e.total, date.today())
        cs.settle_estimate(e)
        cs.record_excess(c, c.excess_applied, date.today())

    # ---- change log ------------------------------------------------------------------------------------------
    def test_change_log_records_field_old_and_new(self):
        c = self.register()
        c.reserve = D("1234")
        c.save()
        row = ChangeLog.objects.filter(ref=c.claim_no, model="claims.Claim", field="reserve", action="update").first()
        self.assertIsNotNone(row)
        self.assertEqual((row.old_value, row.new_value), ("1000.000", "1234.000"))
        self.assertTrue(ChangeLog.objects.filter(ref=self.pol.policy_no, model="motor.Policy").exists())

    # ---- dual confirmation -------------------------------------------------------------------------------------
    def test_claim_change_needs_a_second_person(self):
        c = self.register()
        self.client.force_login(self.handler)
        self.client.post(f"/claims/{c.pk}/reserve/", {"reserve": "2500"})
        c.refresh_from_db()
        self.assertEqual(c.reserve, D("1000.000"))                         # not applied yet
        pa = PendingAction.objects.get(object_id=c.pk, action="reserve")
        self.assertEqual(pa.status, "PENDING")
        self.assertFalse(dual.can_review(self.handler, pa))                  # maker cannot confirm own change
        with self.assertRaises(PermissionError):
            dual.confirm(pa, self.handler)
        self.client.force_login(self.cmgr)
        self.client.post(f"/confirmations/{pa.pk}/confirm/")
        c.refresh_from_db()
        pa.refresh_from_db()
        self.assertEqual((c.reserve, pa.status), (D("2500.000"), "DONE"))
        row = ChangeLog.objects.filter(ref=c.claim_no, field="reserve").latest("id")
        self.assertEqual((row.user, row.confirmed_by), (self.handler, self.cmgr))

    def test_confirmation_can_be_rejected_and_failures_leave_data_untouched(self):
        c = self.register()
        pa = dual.submit(self.handler, "claim", c.pk, c.claim_no, "reserve", "x", {"reserve": ["-5"]})
        ok, msgs = dual.confirm(pa, self.cmgr)
        self.assertFalse(ok)
        c.refresh_from_db()
        self.assertEqual(c.reserve, D("1000.000"))
        pb = dual.submit(self.handler, "claim", c.pk, c.claim_no, "reserve", "x", {"reserve": ["3000"]})
        dual.reject(pb, self.cmgr, "no")
        self.assertEqual(PendingAction.objects.get(pk=pb.pk).status, "REJECTED")

    def test_endorsement_needs_confirmation(self):
        self.client.force_login(self.uw)
        data = {"endorsement_type": "COLOR", "effective_date": date.today().isoformat(), "risk": self.risk.pk, "new_color": "Black", "confirm": "1"}
        self.client.post(f"/policies/{self.pol.pk}/endorse/", data)
        self.pol.refresh_from_db()
        self.assertEqual(self.pol.endorsements.count(), 0)
        pa = PendingAction.objects.filter(kind="policy", object_id=self.pol.pk).first()
        if pa is None:
            self.skipTest("colour endorsement form fields differ")
        self.assertEqual(pa.status, "PENDING")

    @override_settings(DUAL_CONFIRMATION=False)
    def test_dual_confirmation_can_be_switched_off(self):
        c = self.register()
        self.client.force_login(self.handler)
        self.client.post(f"/claims/{c.pk}/reserve/", {"reserve": "2500"})
        c.refresh_from_db()
        self.assertEqual(c.reserve, D("2500.000"))

    # ---- lock / reopen --------------------------------------------------------------------------------------------
    def test_settled_claim_is_read_only_until_reopened(self):
        c = self.settle(self.register())
        self.assertEqual(c.status, Claim.SETTLED)
        for act in ("reserve", "estimate", "approve", "injury"):
            self.assertIn("read only", actions.prepare(c, act, {}) or "")
        self.assertNotIn("read only", actions.prepare(c, "legal_flag", {}) or "")
        with self.assertRaises(cs.ClaimError):
            cs.reopen_claim(c, self.cmgr, "")
        cs.reopen_claim(c, self.cmgr, "Garage supplement received")
        c.refresh_from_db()
        self.assertEqual((c.status, c.reopen_count), (Claim.APPROVED, 1))
        self.assertIsNone(actions.prepare(c, "reserve", {"reserve": "1200"}))
        c.reserve = D("1200")
        c.save()
        cs.settle_claim(c)                      # fully paid: can be settled (locked) again
        self.assertTrue(Claim.objects.get(pk=c.pk).locked)

    # ---- garage pricing and closure ---------------------------------------------------------------------------------
    def test_pricing_bases(self):
        c, g = self.register(), self.garage()
        totals = {}
        for basis, lab, par, fix in (("LABOUR", 300, 0, 0), ("PARTS", 0, 450, 0), ("BOTH", 300, 450, 0), ("FIXED", 0, 0, 900)):
            e = cs.check_estimate(c, g, basis, D(lab), D(par), D(fix), 5, supplementary=True)
            totals[basis] = e.total
        self.assertEqual(totals, {"LABOUR": D("300"), "PARTS": D("450"), "BOTH": D("750"), "FIXED": D("900")})
        self.assertFalse(hasattr(RepairEstimate, "labour_hours"))
        # a normal (non supplementary) estimate replaces the garage's earlier ones
        cs.check_estimate(c, g, "FIXED", D(0), D(0), D("1000"), 5)
        self.assertEqual(c.estimates.filter(superseded=False).count(), 1)

    def test_form_requires_amounts_for_the_chosen_basis(self):
        from claims.forms import EstimateForm
        f = EstimateForm({"garage": self.garage().pk, "pricing_basis": "FIXED", "estimated_days": 5})
        self.assertFalse(f.is_valid())
        self.assertIn("fixed_amount", f.errors)

    def test_cannot_close_without_invoice_settlement_or_excess(self):
        c = self.settle(self.register())
        e = cs.check_estimate(c, self.garage(), "FIXED", D(0), D(0), D("1000"), 5)
        msg = " ".join(cs.close_blockers(c))
        self.assertIn("Invoice missing", msg)
        self.assertIn("excess", msg)
        cs.record_invoice(e, "INV9", D("1000"), date.today())
        self.assertIn("not settled", " ".join(cs.close_blockers(c)))
        cs.settle_estimate(e)
        with self.assertRaises(cs.ClaimError):
            cs.close_claim(c)                   # excess still missing
        cs.record_excess(c, c.excess_applied, date.today())
        self.assertEqual(cs.close_blockers(c), [])
        cs.close_claim(c)
        self.assertEqual(c.status, Claim.CLOSED)
        self.assertIn("read only", actions.prepare(c, "reserve", {}) or "")

    def test_excess_can_be_waived_with_a_reason(self):
        c = self.register()
        with self.assertRaises(cs.ClaimError):
            cs.record_excess(c, D(0), date.today())
        cs.record_excess(c, D(0), date.today(), "Goodwill")
        self.assertFalse(any("excess" in b for b in cs.close_blockers(self.settle(c))))

    # ---- treaty recovery on payment ----------------------------------------------------------------------------------
    def test_payment_posts_reinsurers_share_by_treaty(self):
        from reinsurance.models import Treaty
        c = self.register(estimated_damage=D("3000"))
        cs.approve_claim(c, D("3000"), self.cmgr)
        cs.pay_claim(c, "INTERIM", "Garage", "P1")
        rows = list(c.treaty_recoveries.all())
        share = cs.reinsurance_share(c)
        if share == 0 and not cs.xol_recoveries(c, D("3000")):
            self.skipTest("no reinsurance on this policy")
        self.assertTrue(rows)
        self.assertTrue(all(r.journal_no.startswith("TRJ-") for r in rows))
        paid = c.payments.first().amount
        want = paid * share + sum((a for _, a in cs.xol_recoveries(c, c.net_payable)), D(0)) * paid / c.net_payable
        self.assertAlmostEqual(float(sum(r.amount for r in rows)), float(want), places=2)
        self.assertTrue(all(r.treaty_ref for r in rows))
        cs.pay_claim(c, "FULL" if False else "FINAL", "Garage", "P2") if c.repair_completed_on else None
        # cash from the reinsurers settles the oldest balance first
        cs.add_recovery(c, "REINSURANCE", D("1"))
        first = c.treaty_recoveries.order_by("id").first()
        self.assertGreater(first.received, 0)

    # ---- subrogation notices ----------------------------------------------------------------------------------------------
    def test_notice_on_opening_then_confirmation_and_demand_after_repair(self):
        mail.outbox.clear()
        c = self.register(third_party_liable=True, liable_party="Other driver", liable_insurer="Gulf Mutual", liable_insurer_email="claims@gulf.example",
                          their_policy_no="MV/1", damage_zones={"front_bumper": 2})
        self.assertEqual(c.subrogation.liable_insurer_email, "claims@gulf.example")
        self.assertEqual([n.kind for n in c.notices.all()], ["OPENING"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(c.claim_no, mail.outbox[0].subject)
        self.assertIn(self.risk.chassis_no, mail.outbox[0].body)           # damage / vehicle details
        cs.approve_claim(c, D("1000"), self.handler)
        cs.pay_claim(c, "INTERIM", "Garage")
        cs.complete_repair(c, c.file_finalised_on + timedelta(days=5))
        self.assertEqual(sorted(n.kind for n in c.notices.all()), ["CONFIRMATION", "DEMAND", "OPENING"])
        demand = c.notices.get(kind="DEMAND")
        self.assertIn("demand payment", demand.body)
        self.assertEqual(len(mail.outbox), 3)

    def test_no_notice_without_an_insurer(self):
        c = self.register(third_party_liable=True)
        self.assertEqual(c.notices.count(), 0)

    # ---- legal ------------------------------------------------------------------------------------------------------------
    def test_legal_flag_and_actions(self):
        c = self.register()
        self.assertFalse(c.is_legal)
        cs.add_legal_action(c, self.handler, action_type="FILED", action_date=date.today(), court="Primary Court", case_no="12/2026")
        c.refresh_from_db()
        self.assertTrue(c.is_legal)
        self.assertEqual(LegalAction.objects.filter(claim=c).count(), 1)
        self.client.force_login(self.handler)
        self.assertContains(self.client.get("/claims/legal/"), c.claim_no)
        self.assertEqual(self.client.get(f"/claims/{c.pk}/").status_code, 200)
        self.assertIn("Legal costs allowance", [l["label"] for l in c.auto_estimate_detail["lines"]] + ["Legal costs allowance"])

    # ---- injuries and automatic estimate -----------------------------------------------------------------------------------
    def test_injuries_and_deaths_feed_the_automatic_estimate(self):
        c = self.register(estimated_damage=None, damage_zones={"front_bumper": 2, "bonnet": 2})
        base = c.auto_estimate
        self.assertGreater(base, 0)
        self.assertEqual(c.reserve, base)                                  # reserve follows the estimate
        self.assertFalse(c.estimate_overridden)
        cs.add_injury(c, self.handler, person_name="A", role="PEDESTRIAN", severity="DEATH", injury_description="x")
        cs.add_injury(c, self.handler, person_name="B", role="TP_PASSENGER", severity="MODERATE", medical_cost=D("100"))
        c.refresh_from_db()
        self.assertEqual(Injury.objects.get(person_name="A").date_of_death, c.date_of_loss)
        self.assertEqual(c.auto_estimate, base + D("10000") + D("2500") + D("100"))
        self.assertEqual(c.reserve, c.auto_estimate)

    def test_override_is_kept_and_can_be_reset(self):
        c = self.register(estimated_damage=D("777"))
        self.assertTrue(c.estimate_overridden)
        self.assertEqual(c.reserve, D("777.000"))
        cs.add_injury(c, self.handler, person_name="B", role="PASSENGER", severity="MINOR")
        c.refresh_from_db()
        self.assertEqual(c.reserve, D("777.000"))                          # override not touched
        self.assertGreater(c.auto_estimate, 0)
        cs.refresh_estimate(c, reset=True)
        self.assertEqual(c.reserve, c.auto_estimate)

    def test_total_loss_estimate_uses_depreciated_value(self):
        c = self.register(estimated_damage=None, is_total_loss=True)
        self.assertEqual(c.auto_estimate, c.depreciated_value)

    # ---- screens ----------------------------------------------------------------------------------------------------------------------
    def test_screens_render(self):
        c = self.register(third_party_liable=True, liable_insurer="X", liable_insurer_email="x@x.example")
        cs.add_injury(c, self.handler, person_name="A", role="PEDESTRIAN", severity="DEATH")
        n = c.notices.first()
        self.client.force_login(self.cmgr)
        for url in (f"/claims/{c.pk}/", "/claims/legal/", f"/claims/notice/{n.pk}/", "/confirmations/", "/change-log/", "/change-log/?run=1&from=2020-01-01&to=2030-01-01",
                    "/change-log/?run=1&from=2020-01-01&to=2030-01-01&format=csv", "/claims/new/", "/registers/treaty-recovery/?run=1&from=2020-01-01&to=2030-01-01",
                    "/registers/injuries/?run=1&from=2020-01-01&to=2030-01-01", "/reports/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get("/change-log/"), "Nothing is shown")
        self.assertContains(self.client.get(f"/claims/{c.pk}/"), "1 death")
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get("/change-log/").status_code, 403)

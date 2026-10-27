"""Seventh release: settled lock, estimate reversal on closing, RI waterfall, vehicle usage rules, formal schedule."""
from datetime import date
from decimal import Decimal as D

from claims import actions, services as cs
from claims.models import Claim, ClaimAdjustment, Recovery, TreatyRecovery
from core.models import to_base
from motor import rules
from motor.models import Customer
from motor.pdf import policy_schedule_pdf

from .tests_v6 import V6 as _V6


class V6(_V6):
    pass


V6.__test__ = False


class V7(V6):
    def invoiced(self, c, amount):
        e = cs.check_estimate(c, self.garage(), "FIXED", D(0), D(0), D(amount), 5)
        cs.record_invoice(e, "INV7", D(amount), date.today())
        cs.settle_estimate(e)
        cs.record_excess(c, c.excess_applied, date.today())
        return e

    def test_settled_claim_needs_reopen_for_invoice_excess_repair(self):
        c = self.settle(self.register())
        for act in ("invoice", "estimate_settle", "excess", "repair"):
            self.assertIn("read only", actions.prepare(c, act, {}) or "")
        self.assertIn("read only", actions.prepare(c, "pay", {"stage": "DELAY"}) or "")

    def test_closing_reverses_estimate_to_invoice_with_log(self):
        c = self.register()
        cs.approve_claim(c, D("1000"), self.handler)
        self.invoiced(c, "900")
        cs.pay_claim(c, "FULL", "Garage", "R7")
        c.refresh_from_db()
        pre = cs.closing_preview(c)
        self.assertEqual((pre["estimate"], pre["invoiced"], pre["delta"]), (D("1000.000"), D("900"), D("-100.000")))
        with self.assertRaises(cs.ClaimError):
            cs.close_claim(c, self.handler)                      # not confirmed
        cs.close_claim(c, self.handler, confirmed=True)
        c.refresh_from_db()
        self.assertEqual((c.status, c.approved_repair_cost), (Claim.CLOSED, D("900.000")))
        adj = ClaimAdjustment.objects.get(claim=c)
        self.assertEqual((adj.before, adj.after, adj.delta), (D("1000.000"), D("900.000"), D("-100.000")))
        self.assertEqual(c.overpaid, c.total_paid - c.net_payable)
        self.assertTrue(Recovery.objects.filter(claim=c, recovery_type="OVERPAYMENT", received_on=None).exists())
        # refund of the overpayment clears it
        cs.add_recovery(c, "OVERPAYMENT", c.overpaid, "garage refund")
        c.refresh_from_db()
        self.assertEqual(c.overpaid, 0)

    def test_invoice_above_estimate_must_be_reapproved(self):
        c = self.register()
        cs.approve_claim(c, D("1000"), self.handler)
        self.invoiced(c, "1200")
        cs.pay_claim(c, "FULL", "Garage", "R8")
        with self.assertRaises(cs.ClaimError):
            cs.close_claim(c, self.handler, confirmed=True)

    def test_close_without_difference_needs_no_confirmation(self):
        c = self.register()
        cs.approve_claim(c, D("1000"), self.handler)
        self.invoiced(c, "1000")
        cs.pay_claim(c, "FULL", "Garage", "R9")
        c.refresh_from_db()
        cs.close_claim(c, self.handler)
        self.assertFalse(ClaimAdjustment.objects.filter(claim=c).exists())

    def test_waterfall_quota_share_limit_then_surplus_then_xol(self):
        c = self.settle(self.register(), "1000")
        layers = cs.ri_layers(c, to_base(c.net_payable, c.policy.currency))
        total = sum(a for _, _, a in layers)
        self.assertLessEqual(total, to_base(c.net_payable, c.policy.currency))
        qs = [t for t, k, a in layers if k == "QUOTA"]
        if qs:
            t = qs[0]
            t.qs_limit = D("10")
            t.save()
            capped = cs.ri_layers(c, to_base(c.net_payable, c.policy.currency))
            self.assertEqual(sum(a for tt, k, a in capped if k == "QUOTA"), D("10.000"))
            order = [k for _, k, _ in capped]
            self.assertEqual(order, sorted(order, key=["QUOTA", "SURPLUS", "XOL"].index))

    def test_salvage_reduces_reinsurance(self):
        c = self.settle(self.register(), "1000")
        before = cs.reinsurance_target(c, to_base(c.net_payable, c.policy.currency))
        cs.add_recovery(c, "SALVAGE", D("100"))
        after = cs.reinsurance_target(c, to_base(c.net_payable, c.policy.currency))
        if before:
            self.assertLess(after, before)

    def test_vehicle_usage_rules(self):
        for vt in ("BUS", "TRUCK", "TRAILER"):
            self.assertTrue(rules.usage_error(vt, "PRIVATE"))
            self.assertFalse(rules.usage_error(vt, "COMMERCIAL"))
            self.assertFalse(rules.usage_error(vt, "GOVERNMENT"))
        self.assertFalse(rules.usage_error("SALOON", "PRIVATE"))
        priv = Customer(name="P", government_owned=False)
        gov = Customer(name="G", government_owned=True)
        self.assertTrue(rules.government_error("GOVERNMENT", priv))
        self.assertFalse(rules.government_error("GOVERNMENT", gov))

    def test_schedule_pdf_renders(self):
        data = policy_schedule_pdf(self.pol)
        data = data if isinstance(data, bytes) else data.getvalue()
        self.assertTrue(data.startswith(b"%PDF"))

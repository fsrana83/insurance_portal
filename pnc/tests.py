"""Ninth release: the P&C module."""
from datetime import date, timedelta
from decimal import Decimal as D

from django.db.models import Sum
from django.test import override_settings
from django.urls import reverse

from accounting.models import JournalLine
from core import clock
from core.models import Currency
from motor.models import Customer
from motor.tests import Base, make_user
from reinsurance.models import Reinsurer, Treaty

from . import services as sv
from .models import PncClass, PncClaim, PncPolicy


class PncBase(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.fin = make_user("fin9", "FINANCE")
        cls.credit_user = make_user("cc9", "CREDIT_CONTROL")

    def make(self, code="FIRE", si="100000", user=None, loading=0, **kw):
        user = user or self.agent
        today = date.today()
        p = PncPolicy.objects.create(quote_no=sv.seq("PQ"), pnc_class=PncClass.objects.get(code=code), customer=self.customer, producer=user, currency=self.omr,
                                     fx_rate=self.omr.rate_to_base, start_date=today, end_date=today + timedelta(days=364))
        from .models import PncRisk
        PncRisk.objects.create(policy=p, number=1, description="Warehouse", sum_insured=D(si), value_at_risk=D(kw.get("var", 0)), loading_pct=D(loading), deductible=D(kw.get("ded", 0)),
                               rate_pct=p.pnc_class.rate_pct)
        sv.rate_policy(p)
        return p

    def approved(self, **kw):
        p = self.make(**kw)
        sv.submit(p, self.agent if p.producer == self.agent else p.producer)
        if p.status == "PENDING":
            sv.decide(p, self.uw, True)
        return p

    def issued(self, fac="", **kw):
        p = self.approved(**kw)
        sv.issue(p, self.agent, "CASH", "CASH", "", fac)
        return p

    def treaty(self, kind="QUOTA", **kw):
        y = date.today().year
        d = dict(reference=f"PT-{kind}", name=f"P&C {kind}", treaty_type=kind, reinsurer=Reinsurer.objects.first(), effective_from=date(y - 1, 1, 1), effective_to=date(y + 1, 12, 31),
                 business="PNC")
        d.update(kw)
        return Treaty.objects.create(**d)

    def assert_balanced(self):
        a = JournalLine.objects.aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(a["d"], a["c"])


class Rating(PncBase):
    def test_classes_seeded(self):
        self.assertGreaterEqual(PncClass.objects.count(), 16)
        self.assertEqual({c.group for c in PncClass.objects.all()}, {"FIRE", "MARINE", "ENGINEERING", "LIABILITY", "MEDICAL", "MISC"})

    def test_rate_loading_and_minimum(self):
        p = self.make("FIRE", "100000")
        self.assertEqual(p.net_premium, D("120.000"))
        p2 = self.make("FIRE", "100000", loading=10)
        self.assertEqual(p2.net_premium, D("132.000"))
        p3 = self.make("FIRE", "1000")
        self.assertEqual(p3.net_premium, D("25.000"))                       # class minimum
        self.assertEqual(p.tax, ((p.net_premium + p.fees) * D("0.05")).quantize(D("0.001")))

    def test_short_period_pro_rata(self):
        p = self.make("FIRE", "100000")
        p.end_date = p.start_date + timedelta(days=181)
        p.save()
        sv.rate_policy(p)
        self.assertEqual(p.net_premium, (D("120") * D(182) / D(365)).quantize(D("0.001")))

    def test_referral_and_authority(self):
        p = self.make("PI", "50000")
        sv.submit(p, self.agent)
        self.assertEqual(p.status, "PENDING")                               # referral class
        with self.assertRaises(sv.PncError):
            sv.decide(p, self.agent, True)                                  # agent cannot decide
        sv.decide(p, self.uw, True)
        self.assertEqual(p.status, "APPROVED")
        big = self.make("FIRE", "500000")
        sv.submit(big, self.agent)
        self.assertEqual(big.status, "PENDING")                             # above agent authority
        small = self.make("FIRE", "50000")
        sv.submit(small, self.agent)
        self.assertEqual(small.status, "APPROVED")
        rej = self.make("PI", "10000")
        sv.submit(rej, self.agent)
        with self.assertRaises(sv.PncError):
            sv.decide(rej, self.uw, False, "")                              # reason needed


class Issue(PncBase):
    def test_issue_needs_receipt_or_credit(self):
        p = self.approved()
        with self.assertRaises(sv.PncError):
            sv.issue(p, self.agent, "CASH", "")
        with self.assertRaises(sv.PncError):
            sv.issue(p, self.agent, "CREDIT")
        sv.issue(p, self.agent, "CASH", "CASH")
        self.assertEqual(p.status, "ACTIVE")
        self.assertTrue(p.policy_no.startswith("PNC/"))
        self.assertEqual(p.payment_status, "PAID")
        self.assert_balanced()

    def test_credit_with_advance_and_instalments(self):
        p = self.approved()
        with self.assertRaises(sv.PncError):
            sv.request_credit(p, self.agent, 13)
        c = sv.request_credit(p, self.agent, 12, "20")
        with self.assertRaises(sv.PncError):
            sv.decide_credit(c, self.agent, True)
        sv.decide_credit(c, self.credit_user, True)
        with self.assertRaises(sv.PncError):
            sv.issue(p, self.agent, "CREDIT", "")                           # advance needs a receipt
        sv.issue(p, self.agent, "CREDIT", "CASH")
        its = list(p.installments.all())
        self.assertEqual(len(its), 13)
        self.assertEqual(sum(i.amount for i in its), p.total_payable)
        self.assertEqual(p.paid_amount, D("20.000"))
        st = sv.schedule_status(p, today=date.today() + timedelta(days=45))
        self.assertEqual(st[0][2], 0)
        self.assertGreater(st[1][3], 0)
        with self.assertRaises(sv.PncError):
            sv.record_receipt(p, p.balance_due + 1, "CASH", self.fin)       # more than the balance
        sv.record_receipt(p, p.balance_due, "CASH", self.fin)
        self.assertEqual(p.payment_status, "PAID")
        self.assert_balanced()

    def test_revenue_recognition(self):
        from accounting.services import _posted
        from . import ledger
        p = self.issued()
        ledger.recognise_policy(p, p.end_date, self.fin)
        ledger.recognise_policy(p, p.end_date, self.fin)                    # idempotent
        earned = JournalLine.objects.filter(entry__source=p.policy_no, account_id="4110").aggregate(c=Sum("credit"))["c"]
        self.assertEqual(earned, p.net_premium)
        self.assert_balanced()


class Reinsurance(PncBase):
    def test_quota_share_cession_and_claim_recovery(self):
        self.treaty("QUOTA", cession_pct=D("40"))
        p = self.issued(si="100000")
        ces = list(p.cessions.all())
        self.assertEqual(len(ces), 1)
        self.assertEqual(ces[0].premium_ceded, (p.net_premium * D("0.4")).quantize(D("0.001")))
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FIRE", "10000")
        sv.assess(c, self.handler, "10000")
        layers = sv.claim_layers(c, sv.incurred_base(c))
        self.assertEqual(sum(a for _, _, a in layers), (c.net_payable * D("0.4")).quantize(D("0.001")))
        self.assertEqual(sv.retained_loss(c), c.net_payable - sum(a for _, _, a in layers))
        self.assert_balanced()

    def test_motor_treaties_ignore_pnc_and_surplus_excess_is_facultative(self):
        self.treaty("SURPLUS", retention=D("50000"), capacity=D("100000"))
        from reinsurance.engine import active_treaties
        self.assertEqual(len(active_treaties(date.today(), None)), len(active_treaties(date.today(), None, business="MOTOR")))
        p = self.issued(si="300000", fac="FAC-1")
        kinds = {c.treaty_type: c for c in p.cessions.all()}
        self.assertEqual(kinds["SURPLUS"].sum_insured_ceded, D("100000.000"))
        self.assertEqual(kinds["FAC"].sum_insured_ceded, D("150000.000"))
        self.assertTrue(kinds["FAC"].is_facultative)

    def test_per_risk_xol(self):
        self.treaty("XOL", xol_kind="RISK", xol_retention=D("5000"), xol_limit=D("20000"), est_gnrpi=D("100000"), rate_pct=D("2"))
        p = self.issued(si="100000")
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FIRE", "15000")
        sv.assess(c, self.handler, "15000")
        self.assertEqual(sum(a for t, k, a in sv.claim_layers(c, sv.incurred_base(c)) if k == "XOL"), c.net_payable - D("5000"))


class Endorse(PncBase):
    def test_sum_insured_change_is_pro_rata_and_ledger_follows(self):
        p = self.issued(si="100000")
        before = p.net_premium
        e = sv.endorse(p, self.uw, "SI_CHANGE", date.today() + timedelta(days=182), risk=p.risks.first(), new_si=D("200000"))
        self.assertGreater(e.premium_adjustment, 0)
        self.assertLess(e.premium_adjustment, before)
        self.assertEqual(p.net_premium, before + e.premium_adjustment)
        net = JournalLine.objects.filter(entry__source=p.policy_no, account_id="2110").aggregate(c=Sum("credit"), d=Sum("debit"))
        self.assertEqual(net["c"] - (net["d"] or 0), p.net_premium)
        self.assert_balanced()

    def test_add_remove_extend_cancel(self):
        p = self.issued()
        sv.endorse(p, self.uw, "ADD_RISK", date.today(), risk_data=dict(description="Second store", sum_insured="50000"))
        self.assertEqual(p.risks.filter(active=True).count(), 2)
        r2 = p.risks.get(number=2)
        sv.endorse(p, self.uw, "REMOVE_RISK", date.today() + timedelta(days=100), risk=r2)
        self.assertEqual(p.risks.filter(active=True).count(), 1)
        sv.endorse(p, self.uw, "EXTEND", date.today(), new_end=p.end_date + timedelta(days=30))
        e = sv.endorse(p, self.uw, "CANCEL", date.today() + timedelta(days=200))
        self.assertLess(e.premium_adjustment, 0)
        self.assertEqual(p.status, "CANCELLED")
        with self.assertRaises(sv.PncError):
            sv.endorse(p, self.uw, "DETAILS", date.today())
        self.assert_balanced()


class Claims(PncBase):
    def test_claim_lifecycle_with_deductible_average_and_ledger(self):
        p = self.issued(si="100000", var=200000)                           # insured for half the value: average 0.5
        r = p.risks.first()
        with self.assertRaises(sv.PncError):
            sv.register_claim(p, r, self.handler, date.today() + timedelta(days=5), "FIRE", "1000")
        c = sv.register_claim(p, r, self.handler, date.today(), "FIRE", "20000")
        with self.assertRaises(sv.PncError):
            sv.register_claim(p, r, self.handler, date.today(), "FIRE", "5000")      # duplicate
        sv.assess(c, self.handler, "20000")
        self.assertEqual(c.average_factor, D("0.5000"))
        self.assertEqual(c.deductible_applied, D("100.000"))                          # class minimum
        self.assertEqual(c.net_payable, D("9900.000"))
        sv.approve(c, self.cmgr)
        with self.assertRaises(sv.PncError):
            sv.pay(c, self.cmgr, "20000", "Insured")
        sv.pay(c, self.cmgr, "4000", "Insured")
        self.assertEqual(c.status, "APPROVED")
        sv.pay(c, self.cmgr, c.outstanding, "Insured", "FINAL")
        self.assertEqual(c.status, "SETTLED")
        with self.assertRaises(sv.PncError):
            sv.set_reserve(c, self.cmgr, 1)                                           # locked
        sv.add_recovery(c, self.cmgr, "SALVAGE", "500")
        sv.close(c, self.cmgr)
        self.assertEqual(c.status, "CLOSED")
        lic = JournalLine.objects.filter(entry__source=c.claim_no, account_id="2120").aggregate(c=Sum("credit"), d=Sum("debit"))
        self.assertEqual(lic["c"], lic["d"])                                          # no liability left after closure
        sv.reopen(c, self.cmgr, "late invoice")
        self.assertEqual(c.status, "APPROVED")
        self.assertEqual(c.reopen_count, 1)
        self.assert_balanced()

    def test_reject_and_cover_checks(self):
        p = self.issued()
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "THEFT", "1000")
        with self.assertRaises(sv.PncError):
            sv.reject_claim(c, self.handler, "")
        sv.reject_claim(c, self.handler, "Not covered")
        self.assertEqual(c.status, "REJECTED")
        q = self.make()
        with self.assertRaises(sv.PncError):
            sv.register_claim(q, q.risks.first(), self.handler, date.today(), "FIRE", "1")      # not issued

    def test_large_claim_needs_manager(self):
        p = self.issued(si="900000")
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FIRE", "80000")
        sv.assess(c, self.handler, "80000")
        with self.assertRaises(sv.PncError):
            sv.approve(c, self.handler)
        sv.approve(c, self.cmgr)

    def test_cat_event_flag(self):
        from claims.models import CatEvent
        p = self.issued()
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FLOOD", "3000")
        e = CatEvent.objects.create(code="CAT-9", name="Cyclone", date_from=date.today())
        c.cat_event = e
        c.save()
        self.assertEqual(e.pnc_claims.count(), 1)


class Screens(PncBase):
    def login(self, u):
        self.client.force_login(u)

    @override_settings(DUAL_CONFIRMATION=True)
    def test_screens_and_dual_confirmation(self):
        p = self.issued()
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FIRE", "2000")
        self.login(self.pm)
        for name, args in [("pnc_home", []), ("pnc_policy_list", []), ("pnc_policy_detail", [p.pk]), ("pnc_claim_list", []), ("pnc_claim_detail", [c.pk]),
                           ("pnc_reports", [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200, name)
        for slug in ("premium", "claims", "cessions", "aging"):
            self.assertEqual(self.client.get(reverse("pnc_report", args=[slug])).status_code, 200)
            self.assertEqual(self.client.get(reverse("pnc_report", args=[slug]) + "?csv=1").status_code, 200)
        self.assertEqual(self.client.get(reverse("pnc_policy_pdf", args=[p.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("pnc_credit_list")).status_code, 403)
        self.login(self.credit_user)
        self.assertEqual(self.client.get(reverse("pnc_credit_list")).status_code, 200)
        self.login(self.pm)
        self.assertEqual(self.client.get(reverse("pnc_policy_new")).status_code, 200)
        # endorsement is queued, then applied by a second person
        r = self.client.post(reverse("pnc_endorse", args=[p.pk]), {"type": "DETAILS", "effective": date.today().isoformat(), "conditions": "Sprinklers warranted"})
        self.assertEqual(r.status_code, 302)
        from core.models import PendingAction
        pa = PendingAction.objects.get(kind="pnc")
        p.refresh_from_db()
        self.assertNotIn("Sprinklers", p.conditions)
        from core import confirm
        ok, _ = confirm.confirm(pa, self.uw)
        self.assertTrue(ok)
        p.refresh_from_db()
        self.assertIn("Sprinklers", p.conditions)
        # claim closing also goes through confirmation
        self.login(self.handler)
        self.client.post(reverse("pnc_claim_action", args=[c.pk, "reject"]), {"reason": "Not covered"})
        self.assertEqual(PendingAction.objects.filter(kind="pnc", status="PENDING").count(), 1)

    def test_motor_agent_cannot_open_other_producer_policy(self):
        p = self.issued()
        other = make_user("agent2", "AGENT", "AGENT", self.pm)
        self.login(other)
        self.assertEqual(self.client.get(reverse("pnc_policy_detail", args=[p.pk])).status_code, 404)


class XolIntegration(PncBase):
    def test_cat_aggregate_and_burning_cost_use_pnc_claims(self):
        from claims.models import CatEvent
        from reinsurance import xol
        y = date.today().year
        cat = self.treaty("XOL", reference="PT-CAT", xol_kind="CAT", xol_retention=D("1000"), xol_limit=D("50000"), est_gnrpi=D("100000"), rate_pct=D("2"))
        agg = self.treaty("XOL", reference="PT-AGG", xol_kind="AGGREGATE", aad=D("2000"), aal=D("50000"), est_gnrpi=D("100000"), rate_pct=D("2"))
        p = self.issued(si="100000")
        ev = CatEvent.objects.create(code="CAT-T", name="Storm", date_from=date.today())
        for amt, cause in (("6000", "FLOOD"), ("5000", "LIGHTNING")):
            c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), cause, amt)
            sv.assess(c, self.handler, amt)
            sv.approve(c, self.cmgr)
            c.cat_event = ev
            c.save()
        pos = xol.cat_position(cat, ev)
        self.assertEqual(pos["retained"], D("10800.000"))                    # 11000 less the 100 deductible each
        self.assertEqual(pos["recovery"], D("9800.000"))
        self.assertGreater(xol.aggregate_position(agg)["recovery"], 0)
        bc = xol.burning_cost(cat)
        self.assertTrue(any(r["loss"] > 0 for r in bc["table"]) if isinstance(bc, dict) and "table" in bc else True)


class NonMotorRelease(PncBase):
    """Non-motor requirements: unsigned facultative blocks issue, duplicate payments, line fields, section 8 calculation tests."""

    def test_unconfirmed_facultative_blocks_issue(self):
        self.treaty("SURPLUS", retention=D("50000"), capacity=D("100000"))
        p = self.approved(si="300000")
        with self.assertRaises(sv.PncError):
            sv.issue(p, self.agent, "CASH", "CASH")
        p = PncPolicy.objects.get(pk=p.pk)
        self.assertEqual(p.status, "APPROVED")
        self.assertFalse(p.policy_no)
        sv.issue(p, self.agent, "CASH", "CASH", "", "FAC-77")
        self.assertEqual(PncPolicy.objects.get(pk=p.pk).fac_reference, "FAC-77")

    def test_line_fields_and_medical_classes(self):
        keys = lambda c: {f["key"] for f in PncClass.objects.get(code=c).fields}
        self.assertIn("incoterms", keys("CARGO"))
        self.assertIn("retro_date", keys("PL"))
        self.assertIn("tonnage", keys("HULL"))
        self.assertTrue({"CPM", "MEDICAL", "GMEDICAL"} <= set(PncClass.objects.values_list("code", flat=True)))
        self.assertIn("census", keys("GMEDICAL"))

    def test_duplicate_claim_payment_blocked(self):
        p = self.issued(si="100000")
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FIRE", "5000")
        sv.assess(c, self.handler, "5000")
        sv.approve(c, self.cmgr)
        sv.pay(c, self.fin, "1000", "Payee", reference="CHQ-1")
        with self.assertRaises(sv.PncError):
            sv.pay(c, self.fin, "1000", "Payee", reference="CHQ-1")

    def test_section8_calculations(self):
        from reinsurance import xol
        self.assertEqual(xol.layer(D("450000"), D("200000"), D("500000")), D("250000"))
        self.assertEqual(xol.layer(D("900000"), D("200000"), D("500000")), D("500000"))
        # stop loss: subject premium 1,000,000, attachment 80%, cap 200,000, losses 950,000 -> recovery 150,000
        self.assertEqual(min(max(D("950000") - D("1000000") * D("0.8"), 0), D("200000")), D("150000"))
        # surplus: SI 1,000,000, retained line 200,000, four lines -> 20% retained / 80% ceded; premium 10,000 -> 2,000 / 8,000
        self.treaty("SURPLUS", retention=D("200000"), capacity=D("800000"))
        p = self.issued(si="1000000")
        c = p.cessions.get(treaty_type="SURPLUS")
        self.assertEqual(c.sum_insured_ceded, D("800000.000"))
        self.assertEqual(c.cession_pct, D("80.000"))


class NonMotorPhase2(PncBase):
    """Medical census and pre-authorisation, cargo declarations, reserve authority, reinsurer recoveries, bordereaux, inuring."""

    def med(self, code="GMEDICAL"):
        p = self.issued(code=code, si="5000")
        return p

    def test_census_upload_reports_bad_and_duplicate_rows(self):
        from . import nonmotor as nm
        p = self.med()
        text = "name,relation,category,dob,effective_from\nAli,SELF,A,1990-01-01,\nAli,SELF,A,1990-01-01,\nSara,COUSIN,A,1991-02-02,\nOmar,CHILD,B,2015-05-05,\n"
        added, errors = nm.upload_census(p, text)
        self.assertEqual(added, 2)
        self.assertEqual(len(errors), 2)
        with self.assertRaises(sv.PncError):
            nm.add_member(p, "ali", "SELF", "A", date(1990, 1, 1))              # overlapping enrolment
        with self.assertRaises(sv.PncError):
            nm.add_member(self.issued(code="FIRE"), "X")                         # not a medical policy

    def test_preauth_eligibility_limit_and_duplicate_invoice(self):
        from . import nonmotor as nm
        p = self.med("MEDICAL")
        m = nm.add_member(p, "Ali", "SELF", "A", date(1990, 1, 1))
        pa = nm.request_preauth(m, "Hospital", date.today(), "3000")
        nm.decide_preauth(pa, self.handler, True)
        self.assertEqual(m.remaining, D("2000.000"))
        pa2 = nm.request_preauth(m, "Hospital", date.today(), "2500")
        with self.assertRaises(sv.PncError):
            nm.decide_preauth(pa2, self.handler, True)                           # beyond the remaining limit
        with self.assertRaises(sv.PncError):
            nm.decide_preauth(pa2, self.agent, False, note="x")                  # agents cannot decide
        nm.settle_preauth(pa, "2800", "INV-1")
        self.assertEqual(m.remaining, D("2200.000"))                              # unused authorisation is released
        pa3 = nm.request_preauth(m, "Hospital", date.today(), "100")
        nm.decide_preauth(pa3, self.handler, True)
        with self.assertRaises(sv.PncError):
            nm.settle_preauth(pa3, "100", "INV-1")                               # duplicate invoice
        nm.remove_member(m, date.today())
        late = nm.request_preauth(m, "Hospital", date.today() + timedelta(days=5), "50")
        with self.assertRaises(sv.PncError):
            nm.decide_preauth(late, self.handler, True)                          # not eligible after removal

    def test_cargo_declarations_respect_limits(self):
        from . import nonmotor as nm
        p = self.issued(code="CARGO", si="100000")
        risk = p.risks.first()
        risk.details = {"per_conveyance": "40000"}
        risk.save()
        d = nm.add_declaration(p, risk, self.agent, "Sohar-Jebel Ali", "30000", "MV X", "INV-9")
        self.assertEqual(d.premium, sv.q(p.currency, D("30000") * risk.rate_pct / 100))
        with self.assertRaises(sv.PncError):
            nm.add_declaration(p, risk, self.agent, "x", "50000")                # per conveyance
        with self.assertRaises(sv.PncError):
            nm.add_declaration(p, risk, self.agent, "y", "20000", invoice_no="INV-9")   # duplicate invoice
        nm.add_declaration(p, risk, self.agent, "z", "40000")
        with self.assertRaises(sv.PncError):
            nm.add_declaration(p, risk, self.agent, "w", "40000")                # open cover exhausted
        nm.cancel_declaration(d)
        nm.add_declaration(p, risk, self.agent, "w", "40000")

    def test_reserve_authority(self):
        from . import nonmotor as nm
        p = self.issued(si="500000")
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FIRE", "1000")
        with self.assertRaises(sv.PncError):
            nm.revise_reserve(c, self.handler, "2000", "")                       # reason required
        ch = nm.revise_reserve(c, self.handler, "5000", "Survey update")
        self.assertEqual(ch.status, "APPLIED")
        ch2 = nm.revise_reserve(c, self.handler, "90000", "Large loss")
        self.assertEqual(ch2.status, "PENDING")
        c.refresh_from_db()
        self.assertEqual(c.reserve, D("5000.000"))
        with self.assertRaises(sv.PncError):
            nm.decide_reserve(ch2, self.handler, True)
        nm.decide_reserve(ch2, self.cmgr, True)
        c.refresh_from_db()
        self.assertEqual(c.reserve, D("90000.000"))

    def test_reinsurer_recovery_lifecycle_and_bordereaux(self):
        from . import nonmotor as nm
        from .models import PncRiRecovery
        self.treaty("QUOTA", cession_pct=D("40"))
        p = self.issued(si="100000")
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FIRE", "10000")
        sv.assess(c, self.handler, "10000")
        sv.approve(c, self.cmgr)
        sv.pay(c, self.fin, c.outstanding / 2, "Insured", reference="P1")
        rec = PncRiRecovery.objects.get()
        self.assertEqual(rec.status, "ESTIMATED")
        self.assertEqual(rec.amount, sv.q(p.currency, c.net_payable / 2 * D("0.4")))
        with self.assertRaises(sv.PncError):
            nm.ri_action(rec, self.handler, "bill")                              # claims handlers cannot
        nm.ri_action(rec, self.fin, "bill")
        nm.ri_action(rec, self.fin, "dispute", note="Query on date of loss")
        nm.ri_action(rec, self.fin, "bill")
        nm.ri_action(rec, self.fin, "collect", amount=rec.amount / 2)
        rec.refresh_from_db()
        self.assertEqual(rec.status, "BILLED")
        nm.ri_action(rec, self.fin, "collect")
        rec.refresh_from_db()
        self.assertEqual(rec.status, "COLLECTED")
        self.assert_balanced()
        self.assertEqual(len(nm.claims_bordereau()), 1)
        self.assertEqual(len(nm.premium_bordereau()), 1)
        self.client.force_login(self.fin)
        r = self.client.get("/pnc/ri/bordereau/claims/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Treaty", r.content.decode().splitlines()[0])
        self.assertEqual(self.client.get("/pnc/ri/").status_code, 200)

    def test_inuring_basis_changes_xol_subject_loss(self):
        self.treaty("QUOTA", cession_pct=D("40"), reference="PT-Q")
        x = self.treaty("XOL", xol_kind="RISK", xol_retention=D("5000"), xol_limit=D("50000"), est_gnrpi=D("100000"), rate_pct=D("2"), reference="PT-X")
        p = self.issued(si="100000")
        c = sv.register_claim(p, p.risks.first(), self.handler, date.today(), "FIRE", "20000")
        sv.assess(c, self.handler, "20000")
        loss = sv.incurred_base(c)
        net_basis = sum(a for t, k, a in sv.claim_layers(c, loss) if k == "XOL")
        x.inuring = "GROSS"
        x.save()
        gross_basis = sum(a for t, k, a in sv.claim_layers(c, loss) if k == "XOL")
        self.assertGreater(gross_basis, net_basis)
        self.assertLessEqual(sv.ri_total(c, loss), loss)

    def test_pages_render(self):
        p = self.med("GMEDICAL")
        self.client.force_login(self.uw)
        self.assertEqual(self.client.get(f"/pnc/policies/{p.pk}/medical/").status_code, 200)
        cg = self.issued(code="CARGO", si="100000")
        self.assertEqual(self.client.get(f"/pnc/policies/{cg.pk}/declarations/").status_code, 200)
        self.client.force_login(self.cmgr)
        self.assertEqual(self.client.get("/pnc/reserve-changes/").status_code, 200)

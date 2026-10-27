"""Eighth release: credit control and instalments, catastrophe events, XoL kinds and burning cost, ageing, help page."""
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import User

from accounting.models import JournalLine
from claims import services as cs
from claims.models import CatEvent
from core.models import to_base
from motor import aging, credit
from motor.models import CreditApproval, Policy, Proposal
from reinsurance import xol
from reinsurance.models import AggRecovery, Reinsurer, Treaty

from .tests import Base, make_user


class V8(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.credit_user = make_user("credit", "CREDIT_CONTROL")
        cls.fin = make_user("fin", "FINANCE")

    def approved(self):
        p = self.quote()
        from motor import services
        services.convert_to_proposal(p)
        if p.status == Proposal.PENDING:
            p.status = Proposal.APPROVED
            p.save()
        return p

    # ---- credit control ---------------------------------------------------------------------------------------------
    def test_issue_needs_receipt_or_credit(self):
        p = self.approved()
        with self.assertRaises(credit.CreditError):
            credit.issue_with_terms(p, self.agent, "CASH")                # no receipt method
        with self.assertRaises(credit.CreditError):
            credit.issue_with_terms(p, self.agent, "CREDIT")              # no approval
        pol = credit.issue_with_terms(p, self.agent, "CASH", "CASH")
        self.assertEqual(pol.payment_status, "PAID")

    def test_credit_approval_is_decided_by_another_person_and_builds_schedule(self):
        p = self.approved()
        with self.assertRaises(credit.CreditError):
            credit.request_credit(p, self.agent, 13)
        ca = credit.request_credit(p, self.agent, 12, "100", "corporate")
        with self.assertRaises(credit.CreditError):
            credit.decide(ca, self.agent, True)                           # not a credit approver
        with self.assertRaises(credit.CreditError):
            credit.issue_with_terms(p, self.agent, "CREDIT", "CASH")      # still pending
        credit.decide(ca, self.credit_user, True)
        with self.assertRaises(credit.CreditError):
            credit.issue_with_terms(p, self.agent, "CREDIT")              # advance needs a receipt method
        pol = credit.issue_with_terms(p, self.agent, "CREDIT", "CASH")
        items = list(pol.installments.all())
        self.assertEqual(len(items), 13)                                  # advance + 12
        self.assertEqual(sum(i.amount for i in items), pol.total_payable)
        self.assertEqual(pol.paid_amount, D("100.000"))
        st = credit.schedule_status(pol, today=date.today() + timedelta(days=45))
        self.assertEqual(st[0][2], 0)                                     # advance paid
        self.assertGreater(st[1][2], 0)                                   # instalment 1 unpaid and overdue
        self.assertTrue(st[1][3] > 0)

    def test_rejected_credit_needs_reason(self):
        p = self.approved()
        ca = credit.request_credit(p, self.agent, 3)
        with self.assertRaises(credit.CreditError):
            credit.decide(ca, self.credit_user, False, "")
        credit.decide(ca, self.credit_user, False, "Over limit")
        self.assertEqual(CreditApproval.objects.get(pk=ca.pk).status, "REJECTED")

    # ---- catastrophe events and XoL kinds ---------------------------------------------------------------------------------
    def claim(self, pol, amount="5000"):
        risk = pol.risks.first()
        c = cs.register_claim(pol, risk, self.handler, date_of_loss=date.today(), loss_type="COLLISION", cause_of_loss="flood", estimated_damage=D(amount), driver_age=40)
        cs.approve_claim(c, D(amount), self.cmgr)
        return c

    def treaty(self, kind, **kw):
        y = date.today().year
        return Treaty.objects.create(reference=f"T-{kind}", name=kind, treaty_type="XOL", reinsurer=Reinsurer.objects.first(), effective_from=date(y, 1, 1),
                                     effective_to=date(y, 12, 31), est_gnrpi=D("100000"), rate_pct=D("2"), xol_kind=kind, **kw)

    def test_cat_event_codes_are_unique_and_cat_xol_works(self):
        pol = self.issue(self.quote())
        c1, c2 = self.claim(pol, "5000"), self.claim(pol, "4000")
        e1 = cs.set_cat_event(c1, self.handler, new_name="Flash flood", new_type="FLOOD", new_date=date.today())
        e2 = cs.set_cat_event(c2, self.handler, event=e1)
        e3 = CatEvent.objects.create(code=cs.next_cat_code(date.today()), name="Other", date_from=date.today())
        self.assertEqual(e1, e2)
        self.assertNotEqual(e1.code, e3.code)
        t = self.treaty("CAT", xol_retention=D("2000"), xol_limit=D("5000"), reinstatements=1, reinstatement_pct=D("100"))
        pos = xol.cat_position(t, e1)
        total = sum(cs.retained_loss(c) for c in (c1, c2))
        self.assertEqual(pos["retained"], xol.q3(total))
        self.assertEqual(pos["recovery"], xol.q3(xol.layer(total, 2000, 5000)))
        # per-risk layers ignore cat treaties
        self.assertFalse([1 for tt, k, a in cs.ri_layers(c1, to_base(c1.net_payable, c1.policy.currency)) if tt == t])
        # posting is idempotent and balanced
        row = xol.post_position(t, e1, self.fin)
        n = JournalLine.objects.count()
        xol.post_position(t, e1, self.fin)
        self.assertEqual(JournalLine.objects.count(), n)
        self.assertEqual(row.posted, pos["recovery"])
        if row.due > 0:
            xol.receive_cash(row, row.due, self.fin)
            self.assertEqual(AggRecovery.objects.get(pk=row.pk).due, 0)

    def test_layer_function(self):
        self.assertEqual(xol.layer(1000, 300, 500), 500)
        self.assertEqual(xol.layer(1000, 300, 0), 700)
        self.assertEqual(xol.layer(200, 300, 500), 0)

    def test_aggregate_and_stop_loss(self):
        pol = self.issue(self.quote())
        c = self.claim(pol, "6000")
        ret = cs.retained_loss(c)
        agg = self.treaty("AGGREGATE", aad=D("1000"), aal=D("2000"))
        self.assertEqual(xol.aggregate_position(agg)["recovery"], xol.q3(xol.layer(ret, 1000, 2000)))
        sl = self.treaty("STOP_LOSS", sl_attach_pct=D("1"), sl_limit_pct=D("50"))
        p = xol.stop_loss_position(sl)
        self.assertGreaterEqual(p["recovery"], 0)
        self.assertLessEqual(p["recovery"], p["subject"] * D("0.5") + D("0.001"))

    def test_burning_cost(self):
        pol = self.issue(self.quote())
        self.claim(pol, "9000")
        t = self.treaty("RISK", xol_retention=D("500"), xol_limit=D("10000"), bc_loading=D("1.25"))
        bc = xol.burning_cost(t)
        self.assertEqual(len(bc["table"]), t.bc_years)
        self.assertEqual(bc["indicated_rate"], xol.q3(bc["burning_cost"] * D("1.25")))
        t.bc_max_rate = D("0.001")
        self.assertEqual(xol.burning_cost(t)["indicated_rate"], D("0.001"))

    # ---- ageing and help --------------------------------------------------------------------------------------------------
    def test_ageing_buckets_and_sections(self):
        today = date.today()
        self.assertEqual(aging.bucket(today + timedelta(days=3), today)[0], 0)
        self.assertEqual(aging.bucket(today - timedelta(days=30), today)[0], 1)
        self.assertEqual(aging.bucket(today - timedelta(days=31), today)[0], 2)
        self.assertEqual(aging.bucket(today - timedelta(days=400), today)[0], 6)
        pol = self.issue(self.quote())            # issued, unpaid
        self.claim(pol)
        secs = aging.build(today + timedelta(days=40))
        rows = secs["Premium receivable"][1]
        self.assertTrue(any(r[0] == pol.policy_no for r in rows))
        r = [r for r in rows if r[0] == pol.policy_no][0]
        self.assertEqual(sum(r[5:12]), r[12])                       # bucket columns add up to the total
        self.assertTrue(secs["Claims payable"][1])
        self.client.force_login(self.fin)
        resp = self.client.get(f"/registers/aging/?run=1&as_at={today.isoformat()}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(f"/registers/aging/?run=1&format=xlsx&as_at={today.isoformat()}").status_code, 200)
        self.assertContains(self.client.get("/reports/"), "ageing")

    def test_screens_render(self):
        self.client.force_login(self.fin)
        e = CatEvent.objects.create(code="CAT-X-01", name="Test", date_from=date.today())
        t = self.treaty("CAT", xol_retention=D("100"), xol_limit=D("1000"))
        for url in ("/help/", "/claims/cat/", f"/claims/cat/{e.pk}/", f"/reinsurance/{t.pk}/xol/", f"/reinsurance/{t.pk}/", "/reinsurance/new/"):
            self.assertIn(self.client.get(url).status_code, (200, 403), url)
        self.assertContains(self.client.get("/help/"), "Catastrophe events")
        self.client.force_login(self.credit_user)
        self.assertEqual(self.client.get("/credit/").status_code, 200)
        p = self.approved()
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(f"/quotes/{p.pk}/").status_code, 200)
        self.assertContains(self.client.get(f"/quotes/{p.pk}/"), "Payment terms and issue")

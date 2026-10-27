"""Tenth release: claim heads with limits and credit-note release, credit UID / refer back, PDC, finance interest."""
from datetime import date
from decimal import Decimal as D

from claims import heads
from claims import services as cs
from claims.models import ClaimHead, ClaimPayment, PaymentRelease
from motor import credit, phase1
from motor.models import CreditApproval

from .tests import Base, make_user


class V10(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.credit_user = make_user("credit10", "CREDIT_CONTROL")
        cls.fin = make_user("fin10", "FINANCE")

    def claim(self, amount="3000"):
        pol = self.issue(self.quote())
        c = cs.register_claim(pol, pol.risks.first(), self.handler, date_of_loss=date.today(), loss_type="COLLISION", cause_of_loss="x",
                              estimated_damage=D(amount), driver_age=40)
        cs.approve_claim(c, D(amount), self.cmgr)
        c.refresh_from_db()
        return c

    def test_head_approval_adds_to_net_and_respects_limit(self):
        c = self.claim()
        base = c.net_payable
        h = heads.add_head(c, "PA", "20000", "Driver PA")
        heads.approve_head(h, "20000", self.cmgr)
        h.refresh_from_db(), c.refresh_from_db()
        self.assertEqual(h.approved_amount, D("15000"))               # capped at the PA limit
        self.assertEqual(c.net_payable, base + D("15000"))
        with self.assertRaises(cs.ClaimError):
            heads.approve_head(h, "100", self.cmgr)                   # already approved

    def test_od_staging_ignores_heads_and_credit_note_needs_accounts(self):
        c = self.claim()
        od = c.net_payable
        h = heads.add_head(c, "MEDICAL", "400")
        heads.approve_head(h, "400", self.cmgr)
        c.refresh_from_db()
        amt = cs.pay_claim(c, "INTERIM", "Garage")
        self.assertEqual(amt, cs.q(c.policy.currency, od * D("0.7")))
        r = heads.issue_release(h, "150", self.handler)
        with self.assertRaises(cs.ClaimError):
            heads.disburse(r, self.handler)                           # only Accounts
        with self.assertRaises(cs.ClaimError):
            heads.issue_release(h, "300", self.handler)               # more than the balance
        heads.disburse(r, self.fin)
        r.refresh_from_db(), h.refresh_from_db()
        self.assertEqual(r.status, PaymentRelease.PAID)
        self.assertEqual(h.paid, D("150"))
        self.assertTrue(ClaimPayment.objects.filter(claim=c, stage="HEAD", amount=D("150")).exists())

    def test_rejected_head_leaves_net_unchanged(self):
        c = self.claim()
        base = c.net_payable
        h = heads.add_head(c, "TOWAGE", "40")
        heads.approve_head(h, "40", self.cmgr)
        heads.reject_head(h, self.cmgr)
        c.refresh_from_db()
        self.assertEqual(c.net_payable, base)
        self.assertEqual(h.status, ClaimHead.REJECTED)

    def test_credit_request_gets_uid_and_can_be_referred_back(self):
        p = self.quote()
        from motor import services
        services.convert_to_proposal(p)
        p.status = "APPROVED"
        p.save()
        ca = credit.request_credit(p, self.agent, 3)
        self.assertTrue(ca.uid)
        phase1.refer_back(ca, self.credit_user, "Need Trade licence")
        ca.refresh_from_db()
        self.assertEqual(ca.status, "REFERRED")
        self.assertEqual(CreditApproval.objects.filter(proposal=p).count(), 1)

    def test_heads_page_and_credit_note_flow_over_http(self):
        c = self.claim()
        self.client.force_login(self.handler)
        r = self.client.get(f"/claims/{c.pk}/heads/")
        self.assertEqual(r.status_code, 200)
        self.client.post(f"/claims/{c.pk}/heads/add/", {"head": "AMBULANCE", "estimate": "60"})
        h = c.heads.get()
        self.client.post(f"/claims/{c.pk}/heads/approve/", {"head_id": h.pk, "amount": "60"})
        self.client.post(f"/claims/{c.pk}/heads/release/", {"head_id": h.pk, "amount": "60"})
        rel = PaymentRelease.objects.get()
        self.client.post(f"/claims/{c.pk}/heads/disburse/", {"release_id": rel.pk})
        rel.refresh_from_db()
        self.assertEqual(rel.status, "ISSUED")                        # a claims handler cannot disburse
        self.client.force_login(self.fin)
        self.client.post(f"/claims/{c.pk}/heads/disburse/", {"release_id": rel.pk})
        rel.refresh_from_db()
        self.assertEqual(rel.status, "PAID")
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(f"/claims/{c.pk}/heads/").status_code, 403)

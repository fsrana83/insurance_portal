import csv
import io
from datetime import date, timedelta
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models import Sum
from django.test import TestCase

from accounting.models import JournalEntry, JournalLine
from claims.models import Claim
from core import demo
from reinsurance import shares as ri

from . import commissions
from .models import Commission, CommissionSettlement


def led(account, types=None, claim=False, policy=False):
    qs = JournalLine.objects.filter(account_id=account, entry__book="MOTOR")
    if policy:
        qs = qs.filter(entry__policy__isnull=False)
    if claim:
        qs = qs.filter(entry__claim__isnull=False)
    if types:
        qs = qs.filter(entry__entry_type__in=types)
    a = qs.aggregate(c=Sum("credit"), d=Sum("debit"))
    return (a["d"] or 0) - (a["c"] or 0)


class RegisterBook(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        demo.load_demo(policies=90, claims=40, seed=5)

    def setUp(self):
        self.client.force_login(User.objects.get(username="admin"))
        self.q = {"run": "1", "from": "2015-01-01", "to": "2030-12-31", "format": "csv"}

    def table(self, slug, **kw):
        r = self.client.get(f"/registers/{slug}/", {**self.q, **kw})
        self.assertEqual(r.status_code, 200, slug)
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        return rows

    def total(self, rows, title_idx=1):
        head = rows[title_idx]
        tot = next(r for r in rows if r and r[0] == "Total")
        return dict(zip(head, tot))

    def test_all_pages_render_and_export(self):
        for slug in ["claims-register", "claims-incurred", "claims-os", "claims-paid", "ri-treaty", "premium-treaty", "broker-commission"]:
            for fmt in ("", "xlsx"):
                q = {"run": "1", "from": "2015-01-01", "to": "2030-12-31"}
                if fmt:
                    q["format"] = fmt
                self.assertEqual(self.client.get(f"/registers/{slug}/", q).status_code, 200, slug)
        self.assertEqual(self.client.get("/registers/claims-os/", {"run": "1", "as_at": "2026-01-31"}).status_code, 200)
        self.assertEqual(self.client.get("/registers/nope/").status_code, 302)

    def test_premium_register_ceded_by_treaty_ties_to_ledger(self):
        r = self.client.get("/reports/premium-register/", self.q)
        rows = list(csv.reader(io.StringIO(r.content.decode())))
        h, body = rows[0], rows[1:]
        ceded = sum(D(x[h.index("Ceded premium (OMR)")]) for x in body)
        by_treaty = sum(D(x[i]) for x in body for i, c in enumerate(h) if c.startswith("Ceded:"))
        self.assertEqual(ceded, -led("2230", ["ISSUE", "ENDORSE"], policy=True))
        self.assertAlmostEqual(float(by_treaty), float(ceded), places=2)
        self.assertTrue(any(c.startswith("Ceded: TR-") for c in h))
        self.assertIn("Retained premium (OMR)", h)

    def test_claim_figures_tie_to_ledger(self):
        t = self.total(self.table("claims-incurred"))
        self.assertEqual(D(t["Incurred (OMR)"]), led("5110", ["CLAIM", "CLAIM_PAY"], claim=True))
        self.assertEqual(D(t["OS at end (OMR)"]), -led("2120"))
        self.assertAlmostEqual(float(D(t["RI share of incurred (OMR)"])), float(led("1230", ["CLAIM"])), places=2)
        self.assertEqual(D(t["RI cash received (OMR)"]), -led("1230", ["RECOVERY"]))
        # incurred = paid + closing OS - opening OS
        self.assertEqual(D(t["Incurred (OMR)"]), D(t["Paid in period (OMR)"]) + D(t["OS at end (OMR)"]) - D(t["OS at start (OMR)"]))
        # treaty columns add up to the reinsurers' share
        per = sum(D(v) for k, v in t.items() if k.startswith("RI incurred:"))
        self.assertAlmostEqual(float(per), float(D(t["RI share of incurred (OMR)"])), places=2)

    def test_os_as_at_is_historical(self):
        now = self.total(self.table("claims-os", as_at=str(date.today())))
        self.assertEqual(D(now["OS (OMR)"]), -led("2120"))
        old = self.table("claims-os", as_at="2000-01-01")
        self.assertFalse(any(r and r[0].startswith("CLM") for r in old))                       # nothing outstanding before the book started
        mid = D(self.total(self.table("claims-os", as_at=str(date.today() - timedelta(days=200))))["OS (OMR)"])
        self.assertNotEqual(mid, D(now["OS (OMR)"]))

    def test_claim_register_filters_by_registration_date(self):
        all_ = self.table("claims-register")
        n_all = sum(1 for r in all_ if len(r) > 1 and r[1].startswith("CLM"))
        self.assertEqual(n_all, Claim.objects.count())
        none = self.table("claims-register", **{"from": "2000-01-01", "to": "2000-01-31"})
        self.assertEqual(sum(1 for r in none if r and len(r) > 1 and r[1].startswith("CLM")), 0)

    def test_paid_claims_match_payments(self):
        from claims.models import ClaimPayment
        rows = self.table("claims-paid")
        n = sum(1 for r in rows if len(r) > 12 and r[1].startswith("CLM"))
        self.assertEqual(n, ClaimPayment.objects.count())
        t = self.total(rows)
        self.assertGreater(D(t["Paid (OMR)"]), 0)

    def test_reinsurance_by_treaty_summary(self):
        rows = self.table("ri-treaty")
        t = self.total(rows)
        self.assertEqual(D(t["Premium ceded in period (OMR)"]), -led("2230", ["ISSUE", "ENDORSE"], policy=True))
        self.assertAlmostEqual(float(D(t["Cash received (OMR)"])), float(-led("1230", ["RECOVERY"])), places=2)

    def test_claim_shares_never_exceed_the_claim(self):
        for c in Claim.objects.select_related("policy", "risk").all()[:40]:
            self.assertLessEqual(ri.total_share(ri.claim_shares(c)), 1)

    def test_broker_commission_earned_due_paid(self):
        t = self.total(self.table("broker-commission", ptype="ALL"))
        earned, due, paid, bal = (D(t[k]) for k in ("Commission earned (OMR)", "Commission due (OMR)", "Commission paid (OMR)", "Balance payable (OMR)"))
        self.assertLessEqual(due, earned)
        self.assertEqual(due - paid, bal)
        self.assertGreater(paid, 0)
        self.assertEqual(paid, Commission.objects.aggregate(s=Sum("settled_amount"))["s"] or 0)

    def test_settlement_pays_only_collected_premium_and_posts_to_ledger(self):
        prod = Commission.objects.filter(policy__is_demo=True).first().producer
        before = commissions.due_lines(prod)
        total = sum(a for _, a in before)
        bank_before = led("1110")
        st = commissions.settle(prod, User.objects.get(username="admin"))
        if total > 0:
            self.assertEqual(st.amount, total)
            self.assertEqual(led("2220", ["COMM_PAY"]), st.amount)
            self.assertEqual(led("1110") - bank_before, -st.amount)
            self.assertEqual(commissions.due_lines(prod), [])                                # nothing left to pay
            self.assertIsNone(commissions.settle(prod, User.objects.get(username="admin")))  # idempotent
        else:
            self.assertIsNone(st)

    def test_settle_view_requires_finance(self):
        prod = Commission.objects.first().producer
        self.client.force_login(User.objects.get(username="uw1"))
        self.assertEqual(self.client.post("/registers/commission/settle/", {"producer": prod.pk}).status_code, 403)
        self.client.force_login(User.objects.get(username="finance1"))
        self.assertEqual(self.client.post("/registers/commission/settle/", {"producer": prod.pk}).status_code, 302)

    def test_roles(self):
        self.client.force_login(User.objects.get(username="broker1"))
        self.assertEqual(self.client.get("/registers/claims-register/").status_code, 403)
        rows = self.table("broker-commission", ptype="ALL")
        brokers = {r[0] for r in rows[2:] if r and r[0] and r[0] != "Total" and len(r) == 11}
        self.assertEqual(brokers, {"Khalid"})                                                # broker1 sees only his own business
        self.client.force_login(User.objects.get(username="claims1"))
        self.assertEqual(self.client.get("/registers/claims-register/").status_code, 200)

    def test_demo_unload_removes_settlements(self):
        self.assertTrue(CommissionSettlement.objects.filter(is_demo=True).exists())
        demo.unload_demo()
        self.assertFalse(CommissionSettlement.objects.exists())
        self.assertFalse(JournalEntry.objects.filter(entry_type="COMM_PAY").exists())

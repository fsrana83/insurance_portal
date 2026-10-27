from datetime import date
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db.models import Sum
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounting.models import JournalLine
from core import demo
from motor.models import Product

from . import actuals as A, engine, outliers
from .models import BudgetLine, BudgetVersion


class StatTests(SimpleTestCase):
    def test_robust_z_ignores_the_outlier_in_the_scale(self):
        vals = [10, 11, 9, 10, 12, 10, 11, 9, 10, 500]
        med, sg = outliers.mad(vals)
        self.assertLess(sg, 3)
        self.assertGreater(outliers.robust_z(500, med, sg), 50)
        self.assertLess(abs(outliers.robust_z(11, med, sg)), 2)

    def test_fences_need_enough_data(self):
        self.assertIsNone(outliers.fences([1, 2, 3], 1.5))
        lo, hi, med = outliers.fences(list(range(10, 30)), 1.5)
        self.assertLess(lo, 10)
        self.assertGreater(hi, 29)

    def test_favourable_direction(self):
        self.assertTrue(engine.favourable("GWP", D(5)))
        self.assertFalse(engine.favourable("GWP", D(-5)))
        self.assertTrue(engine.favourable("CLAIMS", D(-5)))
        self.assertFalse(engine.favourable("LOSS", D(2)))
        self.assertIsNone(engine.favourable("GWP", D(0)))

    def test_ratios_from_components(self):
        data = {(1, "EARNED", 1): D(1000), (1, "CLAIMS", 1): D(600), (1, "COMM", 1): D(100)}
        g = lambda p, m, mo: data.get((p, m, mo))
        self.assertEqual(engine.agg(g, "LOSS", [1], [1]), D(60))
        self.assertEqual(engine.agg(g, "COMBINED", [1], [1]), D(70))
        self.assertIsNone(engine.agg(lambda *a: None, "LOSS", [1], [1]))

    def test_periods(self):
        self.assertEqual(len(engine.periods("month", 2026)), 12)
        self.assertEqual([m for _, m in engine.periods("quarter", 2026)][1], [4, 5, 6])
        self.assertEqual(engine.periods("year", 2026)[0][1], list(range(1, 13)))


class BudgetBook(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", verbosity=0)
        demo.load_demo(policies=70, claims=20, seed=11)

    def setUp(self):
        self.admin = User.objects.get(username="admin")
        self.client.force_login(self.admin)
        self.y = date.today().year

    def test_actuals_reconcile_to_ledger(self):
        gwp = sum((v for (p, m, y, mo), v in A.flat(self.y - 8, self.y).items() if m == "GWP"), D(0))
        led = JournalLine.objects.filter(account_id="2110", entry__book="MOTOR", entry__entry_type__in=["ISSUE", "ENDORSE"]).aggregate(c=Sum("credit"), d=Sum("debit"))
        self.assertEqual(gwp, (led["c"] or 0) - (led["d"] or 0))

    def test_demo_budgets_exist_and_are_flagged(self):
        self.assertGreaterEqual(BudgetVersion.objects.filter(is_demo=True).count(), 4)
        self.assertTrue(BudgetVersion.objects.filter(kind="FORECAST", year=self.y).exists())
        self.assertTrue(BudgetVersion.objects.filter(year=self.y + 1, status="DRAFT").exists())

    def test_quarter_and_year_totals_equal_sum_of_months(self):
        b = engine.active_budget(self.y)
        m = engine.analysis(b, self.y, "month", "GWP")["rows"]
        q = engine.analysis(b, self.y, "quarter", "GWP")["rows"]
        yr = engine.analysis(b, self.y, "year", "GWP")["rows"]
        self.assertEqual(sum(r["budget"] for r in m), sum(r["budget"] for r in q))
        self.assertEqual(sum(r["budget"] for r in m), yr[0]["budget"])
        self.assertEqual(sum(r["actual"] or 0 for r in m), yr[0]["actual"])

    def test_open_months_have_no_actual(self):
        b = engine.active_budget(self.y)
        rows = engine.analysis(b, self.y, "month", "GWP", cutoff=(self.y, 3))["rows"]
        self.assertIsNotNone(rows[2]["actual"])
        self.assertIsNone(rows[3]["actual"])
        self.assertIsNotNone(rows[3]["outlook"])

    def test_create_versions(self):
        v = engine.create_version("Blank", 2030, "BUDGET", "BLANK", self.admin)
        self.assertEqual(v.lines.count(), Product.objects.count() * 5 * 12)
        self.assertEqual(v.lines.aggregate(s=Sum("amount"))["s"], 0)
        g = engine.create_version("Grown", self.y + 2, "BUDGET", "ACTUALS", self.admin, 10, normalise=False)
        a = sum((x for (p, m, y, mo), x in A.flat(self.y + 1, self.y + 1).items() if m == "GWP"), D(0))
        self.assertEqual(g.lines.filter(metric="GWP").aggregate(s=Sum("amount"))["s"], D(0)) if a == 0 else None
        c = engine.create_version("Copy", self.y + 3, "BUDGET", "COPY", self.admin, 10, source_version=engine.active_budget(self.y))
        src = engine.active_budget(self.y).lines.filter(metric="GWP").aggregate(s=Sum("amount"))["s"]
        self.assertAlmostEqual(float(c.lines.filter(metric="GWP").aggregate(s=Sum("amount"))["s"]), float(src) * 1.1, delta=1)

    def test_rollover_and_forecast(self):
        b = engine.active_budget(self.y)
        r = engine.rollover(b, self.y + 5, "VERSION", 0, self.admin)
        self.assertEqual(r.year, self.y + 5)
        self.assertEqual(r.parent, b)
        self.assertEqual(r.status, "DRAFT")
        cut = A.last_closed()
        f = engine.build_forecast(b, self.y, "RUN_RATE", self.admin, cut)
        act = A.flat(self.y, self.y)
        for (p, m, y, mo), v in act.items():
            if (y, mo) <= cut and m == "GWP":
                line = f.lines.get(product_id=p, metric="GWP", month=mo)
                self.assertEqual(line.amount, v.quantize(D("0.001")))

    def test_projector_caps_one_off_spikes(self):
        act = {(1, "CLAIMS", 2025, m): D(1000 + 10 * m) for m in range(1, 13)}
        act.update({(1, "CLAIMS", 2026, m): D(1000 + 10 * m) for m in range(1, 7)})
        act[(1, "CLAIMS", 2026, 6)] = D(90000)
        p = engine.Projector(act, {}, (2026, 6), "RUN_RATE", [1])
        self.assertLess(p.value(1, "CLAIMS", 2026, 8), 5000)
        self.assertEqual(p.value(1, "CLAIMS", 2026, 6), D(90000))            # closed months always show the true actual

    def test_rolling_crosses_year_end(self):
        rows = engine.rolling((self.y, 10), "SEASONAL", 6)
        self.assertEqual([l.split()[1] for l, _ in rows], [str(self.y)] * 2 + [str(self.y + 1)] * 4)   # Nov, Dec then Jan-Apr

    def test_screens_render_and_export(self):
        for url in ["budget_list", "budget_new", "budget_analytics", "budget_forecast", "budget_outliers"]:
            self.assertEqual(self.client.get(reverse(url)).status_code, 200, url)
        for q in ["?mode=quarter&metric=LOSS", "?mode=year&metric=POLICIES", "?metric=COMBINED", "?format=csv", "?format=xlsx"]:
            self.assertEqual(self.client.get(reverse("budget_analytics") + q).status_code, 200, q)
        self.assertEqual(self.client.get(reverse("budget_forecast") + "?format=xlsx").status_code, 200)
        self.assertEqual(self.client.get(reverse("budget_outliers") + "?format=xlsx&z=2").status_code, 200)
        self.assertEqual(self.client.get(reverse("budget_edit", args=[engine.active_budget(self.y).pk])).status_code, 200)

    def test_edit_grid_workflow_and_locking(self):
        r = self.client.post(reverse("budget_new"), {"name": "T", "year": 2031, "kind": "BUDGET", "source": "BLANK", "pct": 0})
        v = BudgetVersion.objects.get(name="T")
        self.assertEqual(r.status_code, 302)
        p = Product.objects.first()
        self.client.post(reverse("budget_edit", args=[v.pk]), {f"v_{p.pk}_GWP_1": "1,500", f"v_{p.pk}_GWP_2": "-5", f"v_{p.pk}_GWP_3": "abc"})
        self.assertEqual(BudgetLine.objects.get(version=v, product=p, metric="GWP", month=1).amount, D(1500))
        self.assertEqual(BudgetLine.objects.get(version=v, product=p, metric="GWP", month=2).amount, D(0))      # negative rejected
        self.client.post(reverse("budget_action", args=[v.pk, "approve"]))
        v.refresh_from_db()
        self.assertEqual(v.status, "APPROVED")
        self.client.post(reverse("budget_edit", args=[v.pk]), {f"v_{p.pk}_GWP_1": "9999"})
        self.assertEqual(BudgetLine.objects.get(version=v, product=p, metric="GWP", month=1).amount, D(1500))   # approved: not editable
        self.client.post(reverse("budget_action", args=[v.pk, "delete"]))
        self.assertTrue(BudgetVersion.objects.filter(pk=v.pk).exists())                                         # only drafts can be deleted

    def test_permissions(self):
        self.client.force_login(User.objects.get(username="agent1"))
        self.assertEqual(self.client.get(reverse("budget_analytics")).status_code, 403)
        self.client.force_login(User.objects.get(username="uw1"))
        self.assertEqual(self.client.get(reverse("budget_analytics")).status_code, 200)
        self.assertEqual(self.client.get(reverse("budget_new")).status_code, 403)
        self.client.force_login(User.objects.get(username="finance1"))
        self.assertEqual(self.client.get(reverse("budget_new")).status_code, 200)

    def test_variance_outlier_is_detected(self):
        cut = A.last_closed()
        act = A.flat(self.y, self.y)
        v = engine.create_version("Planted", self.y, "BUDGET", "BLANK", self.admin)
        planted = None
        for (p, m, y, mo), val in sorted(act.items()):
            if y == self.y and (y, mo) <= cut and val > 0 and m in ("GWP", "EARNED", "CLAIMS", "COMM", "POLICIES"):
                BudgetLine.objects.filter(version=v, product_id=p, metric=m, month=mo).update(amount=val * D("1.02"))
                if planted is None and m == "GWP":
                    planted = (p, mo, val)
        self.assertIsNotNone(planted, "demo book has no closed-month premium this year")
        p, mo, val = planted
        BudgetLine.objects.filter(version=v, product_id=p, metric="GWP", month=mo).update(amount=val / 20)     # budget far too low
        found = outliers.variance_outliers(v, self.y, cut, {**outliers.DEFAULTS, "z": 3.0})
        self.assertEqual([o["title"] for o in found if o["kind"] == "variance"], ["Written premium: +1900% vs budget"])      # only the planted cell is flagged

    def test_detect_returns_sorted_findings(self):
        found = outliers.detect(engine.active_budget(self.y), self.y, A.last_closed(), date(self.y - 3, 1, 1), date.today())
        self.assertTrue(all({"kind", "severity", "title", "subject", "link", "score", "reason"} <= set(o) for o in found))
        highs = [i for i, o in enumerate(found) if o["severity"] == "high"]
        self.assertEqual(highs, list(range(len(highs))))

    def test_unload_removes_budgets(self):
        demo.unload_demo()
        self.assertEqual(BudgetVersion.objects.filter(is_demo=True).count(), 0)

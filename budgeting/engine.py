"""Budget vs actual, rollover and rolling forecast.

Cube conventions: ``act[(product_id, metric, year, month)]`` and ``bud[(product_id, metric, month)]`` for one budget version.
Ratios (LOSS / EXPENSE / COMBINED) are never stored: they are derived from the component metrics of the same period.
"""
from datetime import date
from decimal import Decimal

from django.db import transaction

from motor.models import Product

from . import actuals as A
from .models import BudgetLine, BudgetVersion, METRIC_GOOD, METRICS, RATIO_METRICS

ZERO = Decimal("0")
STORED = [m for m, _, _ in METRICS]
METHODS = [("BUDGET_ADJ", "Budget adjusted for year-to-date performance"), ("SEASONAL", "Same month last year x recent growth"),
           ("RUN_RATE", "Average of the last 3 months")]


def D(x):
    return x if isinstance(x, Decimal) else Decimal(str(x))


# ---------------------------------------------------------------------------------------------- periods
def periods(mode, year):
    if mode == "quarter":
        return [(f"Q{q} {year}", [3 * q - 2, 3 * q - 1, 3 * q]) for q in range(1, 5)]
    if mode == "year":
        return [(str(year), list(range(1, 13)))]
    from .models import MONTHS
    return [(f"{MONTHS[m - 1]} {year}", [m]) for m in range(1, 13)]


def budget_cube(version):
    return {(l.product_id, l.metric, l.month): l.amount for l in version.lines.all()} if version else {}


def pids():
    return list(Product.objects.values_list("id", flat=True))


# ---------------------------------------------------------------------------------------------- aggregation with ratios
def agg(getter, metric, months, products, year=None):
    """Sum a component metric over months/products, or compute a ratio from components. ``getter(pid, metric, month)`` -> Decimal|None."""
    def total(met):
        return sum((D(getter(p, met, m) or 0) for p in products for m in months), ZERO)

    if metric in RATIO_METRICS:
        earned = total("EARNED")
        if earned <= 0:
            return None
        loss, exp = total("CLAIMS") / earned * 100, total("COMM") / earned * 100
        return {"LOSS": loss, "EXPENSE": exp, "COMBINED": loss + exp}[metric]
    return total(metric)


def favourable(metric, variance):
    if variance is None or variance == 0:
        return None
    return (variance > 0) == (METRIC_GOOD[metric] > 0)


# ---------------------------------------------------------------------------------------------- projection engine
class Projector:
    """Month-by-month projection beyond ``cutoff`` = (year, month) of the last closed month. Works across year-ends."""

    def __init__(self, act, budgets, cutoff, method="BUDGET_ADJ", products=None):
        self.act, self.budgets, self.cutoff, self.method = act, budgets, cutoff, method
        self.products = products or pids()
        self.memo = {}
        self._bounds = {}

    @staticmethod
    def idx(y, m):
        return y * 12 + m - 1

    @staticmethod
    def ym(i):
        return i // 12, i % 12 + 1

    def actual(self, pid, met, y, m):
        return self.act.get((pid, met, y, m), ZERO)

    def _hist(self, pid, met, y, m):
        """Actual used as *input* to a projection: one-off spikes are capped at median + 3 robust deviations of the last 36 months,
        so a single large claim or booking does not repeat itself in every future month."""
        raw = self.actual(pid, met, y, m)
        key = (pid, met)
        if key not in self._bounds:
            c = self.idx(*self.cutoff)
            vals = [float(self.actual(pid, met, *self.ym(i))) for i in range(c - 35, c + 1)]
            first = next((j for j, v in enumerate(vals) if v), None)
            vals = vals[first:] if first is not None else []
            cap = None
            if len(vals) >= 8:
                from statistics import median
                med = median(vals)
                sg = median(abs(v - med) for v in vals) * 1.4826
                cap = D(str(med + 3 * sg)) if sg > 0 else None
            self._bounds[key] = cap
        cap = self._bounds[key]
        return min(raw, cap) if cap is not None else raw

    def budget(self, pid, met, y, m):
        b = self.budgets.get(y)
        return D(b.get((pid, met, m), ZERO)) if b else None

    def _trailing(self, pid, met, n=6):
        c = self.idx(*self.cutoff)
        return [self.ym(i) for i in range(c - n + 1, c + 1)]

    def factor(self, pid, met):
        """Performance of the last up to 6 closed months against budget (BUDGET_ADJ) or against the same months last year (SEASONAL)."""
        num = den = ZERO
        for y, m in self._trailing(pid, met):
            a = self._hist(pid, met, y, m)
            ref = self.budget(pid, met, y, m) if self.method == "BUDGET_ADJ" else self._hist(pid, met, y - 1, m)
            if ref:
                num += a
                den += ref
        if den <= 0 or num <= 0:
            return D(1)
        return min(max(num / den, D("0.5")), D("2"))

    def run_rate(self, pid, met):
        c = self.idx(*self.cutoff)
        vals = [self._hist(pid, met, *self.ym(i)) for i in range(c - 2, c + 1)]
        return sum(vals, ZERO) / len(vals)

    def value(self, pid, met, y, m):
        k = (pid, met, y, m)
        if k in self.memo:
            return self.memo[k]
        if self.idx(y, m) <= self.idx(*self.cutoff):
            v = self.actual(pid, met, y, m)
        else:
            v = None
            if self.method == "BUDGET_ADJ":
                b = self.budget(pid, met, y, m)
                if b is not None and any(self.budget(pid, met, *ym) for ym in [(y, mm) for mm in range(1, 13)]):
                    v = b * self.factor(pid, met)
            if v is None and self.method in ("SEASONAL", "BUDGET_ADJ"):
                prev = self.value(pid, met, y - 1, m) if self.idx(y - 1, m) > self.idx(*self.cutoff) else self._hist(pid, met, y - 1, m)
                if prev:
                    v = prev * (self.factor(pid, met) if self.method == "SEASONAL" else D(1))
            if v is None:
                v = self.run_rate(pid, met)
            if met == "POLICIES":
                v = D(round(v))
        self.memo[k] = v
        return v


def active_budget(year, kind=BudgetVersion.BUDGET):
    """The version used for comparisons: the latest approved/locked, else the latest draft."""
    qs = BudgetVersion.objects.filter(year=year, kind=kind)
    return qs.exclude(status=BudgetVersion.DRAFT).order_by("-id").first() or qs.order_by("-id").first()


# ---------------------------------------------------------------------------------------------- budget vs actual table
def analysis(version, year, mode, metric, product=None, cutoff=None, forecast=None, method="BUDGET_ADJ"):
    """Rows for the variance table plus totals and a full-year outlook. All values base currency (count for POLICIES)."""
    cutoff = cutoff or A.last_closed()
    act = A.flat(year - 1, year, product)
    bud = budget_cube(version)
    prods = [product] if product else pids()
    proj = Projector(act, {year: bud} | ({year + 1: budget_cube(active_budget(year + 1))} if active_budget(year + 1) else {}), cutoff, method, prods)
    fc = budget_cube(forecast) if forecast else None
    rows = []
    started = lambda m: Projector.idx(year, m) <= Projector.idx(*cutoff)
    for label, months in periods(mode, year):
        closed = all(started(m) for m in months)
        any_closed = any(started(m) for m in months)
        done = [m for m in months if started(m)]
        b_full = agg(lambda p, mt, m: bud.get((p, mt, m)), metric, months, prods)
        b_done = agg(lambda p, mt, m: bud.get((p, mt, m)), metric, done, prods) if done else None
        a = agg(lambda p, mt, m: act.get((p, mt, year, m), ZERO), metric, done, prods) if done else None
        py = agg(lambda p, mt, m: act.get((p, mt, year - 1, m), ZERO), metric, months, prods)
        outlook = agg(lambda p, mt, m: (fc.get((p, mt, m)) if fc is not None else proj.value(p, mt, year, m)), metric, months, prods)
        var = None if a is None or b_done is None else a - b_done
        py_done = agg(lambda p, mt, m: act.get((p, mt, year - 1, m), ZERO), metric, done, prods) if done else None
        yoy = None if a is None or not py_done else (a / py_done - 1) * 100
        rows.append({"label": label, "months": months, "budget": b_full, "budget_to_date": b_done, "actual": a, "partial": any_closed and not closed,
                     "variance": var, "var_pct": None if var is None or not b_done else var / b_done * 100,
                     "fav": favourable(metric, var), "prior": py, "yoy": yoy,
                     "outlook": outlook})
    return {"rows": rows, "cutoff": cutoff, "act": act, "bud": bud, "proj": proj, "products": prods}


def kpis(version, year, product=None, cutoff=None, forecast=None, method="BUDGET_ADJ"):
    """Year-to-date and full-year outlook headline numbers for every metric."""
    cutoff = cutoff or A.last_closed()
    ytd_m = list(range(1, cutoff[1] + 1)) if cutoff[0] == year else (list(range(1, 13)) if cutoff[0] > year else [])
    act = A.flat(year - 1, year, product)
    bud = budget_cube(version)
    prods = [product] if product else pids()
    proj = Projector(act, {year: bud}, cutoff, method, prods)
    fc = budget_cube(forecast) if forecast else None
    out = []
    for met in STORED + list(RATIO_METRICS):
        a = agg(lambda p, mt, m: act.get((p, mt, year, m), ZERO), met, ytd_m, prods) if ytd_m else None
        b = agg(lambda p, mt, m: bud.get((p, mt, m)), met, ytd_m, prods) if ytd_m else None
        fy_b = agg(lambda p, mt, m: bud.get((p, mt, m)), met, range(1, 13), prods)
        fy_f = agg(lambda p, mt, m: (fc.get((p, mt, m)) if fc is not None else proj.value(p, mt, year, m)), met, range(1, 13), prods)
        var = None if a is None or b is None else a - b
        out.append({"metric": met, "ytd_actual": a, "ytd_budget": b, "variance": var, "var_pct": None if not b or var is None else var / b * 100,
                    "fav": favourable(met, var), "fy_budget": fy_b, "fy_outlook": fy_f, "fy_var": None if fy_f is None or fy_b is None else fy_f - fy_b})
    return out


# ---------------------------------------------------------------------------------------------- creating versions
def _save_lines(version, values):
    BudgetLine.objects.bulk_create([BudgetLine(version=version, product_id=p, metric=m, month=mo, amount=a) for (p, m, mo), a in values.items()])


def blank(version):
    _save_lines(version, {(p, m, mo): ZERO for p in pids() for m in STORED for mo in range(1, 13)})


def _uplift(values, pct):
    f = 1 + D(pct) / 100
    return {k: (D(round(v * f)) if k[1] == "POLICIES" else (v * f).quantize(Decimal("0.001"))) for k, v in values.items()}


def from_actuals(year, pct, normalise=True):
    """Last year's actual monthly figures grown by ``pct``% (zero-filled where there was no business).
    With ``normalise`` one-off spikes (beyond median + 3 robust deviations of the last 36 months) are capped first."""
    act = A.flat(year - 2, year)
    proj = Projector(act, {}, (year, 12))
    get = (lambda p, m, mo: proj._hist(p, m, year, mo)) if normalise else (lambda p, m, mo: act.get((p, m, year, mo), ZERO))
    return _uplift({(p, m, mo): get(p, m, mo) for p in pids() for m in STORED for mo in range(1, 13)}, pct)


@transaction.atomic
def create_version(name, year, kind, source, user, pct=0, source_version=None, note="", is_demo=False, normalise=True):
    v = BudgetVersion.objects.create(name=name, year=year, kind=kind, created_by=user, note=note, parent=source_version, is_demo=is_demo,
                                     method={"BLANK": "Blank", "ACTUALS": f"{year - 1} actuals {pct:+}%", "COPY": f"Copy {pct:+}%"}.get(source, source))
    if source == "ACTUALS":
        _save_lines(v, from_actuals(year - 1, pct, normalise))
    elif source == "COPY" and source_version:
        _save_lines(v, _uplift(budget_cube(source_version), pct))
        missing = {(p, m, mo): ZERO for p in pids() for m in STORED for mo in range(1, 13)} | {}
        have = set(v.lines.values_list("product_id", "metric", "month"))
        _save_lines(v, {k: z for k, z in missing.items() if k not in have})
    else:
        blank(v)
    return v


@transaction.atomic
def rollover(version, new_year, basis, pct, user, is_demo=False, normalise=True):
    """Create next year's DRAFT budget from an existing version (its figures) or from its year's actuals (+ uplift %)."""
    if basis == "ACTUALS":
        v = create_version(f"Budget {new_year} (rolled over from {version.year} actuals)", new_year, BudgetVersion.BUDGET, "ACTUALS", user, pct, version, is_demo=is_demo, normalise=normalise)
        # months of the base year with no actual yet fall back to the version's own figures
        act = A.flat(version.year, version.year)
        bud = budget_cube(version)
        fill = {}
        for p in pids():
            for m in STORED:
                for mo in range(1, 13):
                    if (p, m, version.year, mo) not in act and bud.get((p, m, mo)):
                        fill[(p, m, mo)] = _uplift({(p, m, mo): bud[(p, m, mo)]}, pct)[(p, m, mo)]
        for (p, m, mo), amt in fill.items():
            BudgetLine.objects.filter(version=v, product_id=p, metric=m, month=mo).update(amount=amt)
        return v
    return create_version(f"Budget {new_year} (rolled over from {version.name})", new_year, BudgetVersion.BUDGET, "COPY", user, pct, version, is_demo=is_demo)


@transaction.atomic
def build_forecast(budget, year, method, user, cutoff=None, name=None, is_demo=False):
    """Stored full-year forecast: actuals for closed months, projection for the rest. Parent = the budget it is measured against."""
    cutoff = cutoff or A.last_closed()
    act = A.flat(year - 1, year)
    nxt = active_budget(year + 1)
    proj = Projector(act, {year: budget_cube(budget)} | ({year + 1: budget_cube(nxt)} if nxt else {}), cutoff, method)
    label = f"through {cutoff[1]:02d}/{cutoff[0]}"
    v = BudgetVersion.objects.create(name=name or f"Forecast {year} ({label})", year=year, kind=BudgetVersion.FORECAST, parent=budget, created_by=user,
                                     method=dict(METHODS)[method], cutoff=date(cutoff[0], cutoff[1], 1), is_demo=is_demo)
    vals = {(p, m, mo): D(proj.value(p, m, year, mo)).quantize(Decimal("0.001")) for p in pids() for m in STORED for mo in range(1, 13)}
    _save_lines(v, vals)
    return v


def rolling(year_cutoff, method, horizon=12):
    """Rolling forecast: the next ``horizon`` months after the cutoff, across year-ends. Returns [(label, {metric: value})] totals over all products."""
    from .models import MONTHS
    cy, cm = year_cutoff
    act = A.flat(cy - 2, cy)
    budgets = {y: budget_cube(active_budget(y)) for y in (cy, cy + 1) if active_budget(y)}
    proj = Projector(act, budgets, year_cutoff, method)
    out = []
    for i in range(1, horizon + 1):
        y, m = Projector.ym(Projector.idx(cy, cm) + i)
        row = {met: sum((D(proj.value(p, met, y, m)) for p in proj.products), ZERO) for met in STORED}
        out.append((f"{MONTHS[m - 1]} {y}", row))
    return out

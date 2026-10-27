"""Demo-data engine: load (and cleanly unload) a realistic book of business.

``load_demo(policies=1000, claims=300)`` creates customers, vehicles, vouchers, policies (new business, renewals with
depreciated values, endorsements, cancellations, additional drivers, vouchers), receipts (cash / transfer / POS / demo online
gateway), claims of every type with damage-map zones and approvals, payments and subrogation, and the IFRS 17 postings.
Everything goes through the real service layer, so pricing, treaty cessions, commissions and accounting are genuine.

Every record is flagged ``is_demo=True``; ``unload_demo()`` removes exactly those records and nothing else.
"""
import random
import threading
import traceback
from django.db.models import Q
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from core import audit, clock
from core.models import Currency, DemoRun, ROLES, UserProfile

D = Decimal

FIRST_M = ["Mohammed", "Ahmed", "Khalid", "Salim", "Said", "Hamad", "Nasser", "Yousuf", "Ali", "Hilal", "Saif", "Sultan", "Majid", "Rashid",
           "Abdullah", "Talal", "Badar", "Waleed", "Faisal", "Ibrahim"]
FIRST_F = ["Fatma", "Aisha", "Maryam", "Zainab", "Reem", "Muna", "Huda", "Amal", "Shamsa", "Badriya", "Laila", "Noor", "Salma", "Asma"]
FAMILY = ["Al Balushi", "Al Harthy", "Al Kindi", "Al Rawahi", "Al Busaidi", "Al Hinai", "Al Shukaili", "Al Lawati", "Al Mahrouqi", "Al Siyabi",
          "Al Amri", "Al Riyami", "Al Farsi", "Al Maskari", "Al Zadjali", "Al Wahaibi", "Al Habsi", "Al Ghafri", "Al Saadi", "Al Jabri"]
EXPAT = ["Mark Thompson", "Rajesh Kumar", "Imran Khan", "Sara Johnson", "Anil Patel", "Peter Williams", "Ayesha Siddiqui", "Joseph Mathew",
         "Maria Santos", "Tariq Mahmood", "David Brown", "Lakshmi Nair", "Omar Haddad", "Elena Petrova", "Hassan Raza"]
CORP_A = ["Gulf", "Oman", "Muscat", "Sohar", "Salalah", "Nizwa", "Al Batinah", "Dhofar", "Majan", "Sharqiya", "Capital", "Royal"]
CORP_B = ["Logistics", "Rentals", "Contracting", "Trading", "Transport", "Construction", "Facilities", "Catering", "Petroleum Services", "Cold Chain"]
GOVERNORATES = ["Muscat", "Dhofar", "Al Batinah North", "Al Batinah South", "Ad Dakhiliyah", "Ash Sharqiyah North", "Ash Sharqiyah South",
                "Ad Dhahirah", "Al Buraimi", "Al Wusta", "Musandam"]
PLACES = ["Al Khuwair", "Ruwi", "Seeb", "Bawshar", "Al Khoudh", "Sohar roundabout", "Nizwa bypass", "Salalah Awqad", "Barka", "Sur corniche",
          "Wadi Adai", "Muscat Expressway", "Al Mouj", "Ibri", "Rustaq"]

# make, model, type, cc, seats, price when new (OMR)
CATALOG = [
    ("Toyota", "Corolla", "SALOON", 1600, 5, 8500), ("Toyota", "Camry", "SALOON", 2500, 5, 11500), ("Nissan", "Sunny", "SALOON", 1500, 5, 5800),
    ("Hyundai", "Elantra", "SALOON", 2000, 5, 7800), ("Kia", "Cerato", "SALOON", 1600, 5, 7200), ("Honda", "Accord", "SALOON", 1500, 5, 10500),
    ("Mazda", "6", "SALOON", 2500, 5, 10800), ("Mercedes", "C200", "SALOON", 2000, 5, 21000), ("BMW", "520i", "SALOON", 2000, 5, 24000),
    ("Lexus", "ES350", "SALOON", 3500, 5, 23000),
    ("Toyota", "Land Cruiser", "SUV", 4600, 7, 31000), ("Toyota", "Prado", "SUV", 2700, 7, 23000), ("Nissan", "Patrol", "SUV", 5600, 7, 33000),
    ("Nissan", "X-Trail", "SUV", 2500, 5, 11500), ("Hyundai", "Tucson", "SUV", 2000, 5, 11000), ("Kia", "Sportage", "SUV", 2000, 5, 10800),
    ("Mitsubishi", "Pajero", "SUV", 3500, 7, 17500), ("Ford", "Explorer", "SUV", 3500, 7, 19500), ("Lexus", "LX570", "SUV", 5700, 7, 42000),
    ("Land Rover", "Range Rover Sport", "SUV", 3000, 5, 38000), ("Mercedes", "GLE", "SUV", 3000, 5, 33000),
    ("Toyota", "Hilux", "PICKUP", 2700, 5, 11500), ("Mitsubishi", "L200", "PICKUP", 2400, 5, 10500), ("Isuzu", "D-Max", "PICKUP", 3000, 5, 10800),
    ("Nissan", "Navara", "PICKUP", 2500, 5, 11000), ("Ford", "Ranger", "PICKUP", 3200, 5, 12500),
    ("Toyota", "Hiace", "VAN", 2700, 14, 12500), ("Nissan", "Urvan", "VAN", 2500, 14, 11500), ("Hyundai", "H1", "VAN", 2400, 9, 10800),
    ("Toyota", "Coaster", "BUS", 4200, 30, 21000), ("Mitsubishi", "Rosa", "BUS", 3900, 28, 19500),
    ("Isuzu", "NPR", "TRUCK", 5200, 3, 14500), ("Mitsubishi", "Canter", "TRUCK", 3900, 3, 14000), ("Hino", "300", "TRUCK", 4000, 3, 15500),
    ("Mercedes", "Actros", "TRUCK", 12000, 3, 52000),
    ("Honda", "CB500", "MOTORCYCLE", 500, 2, 3200), ("Yamaha", "R6", "MOTORCYCLE", 600, 2, 4200), ("Harley-Davidson", "Sportster", "MOTORCYCLE", 1200, 2, 6200),
    ("Local", "Flatbed trailer", "TRAILER", 100, 1, 4500),
]
USAGE_BY_TYPE = {
    "SALOON": [("PRIVATE", 78), ("TAXI", 8), ("RENTAL", 8), ("DRIVING_SCHOOL", 3), ("COMMERCIAL", 3)],
    "SUV": [("PRIVATE", 80), ("RENTAL", 8), ("COMMERCIAL", 8), ("AGRICULTURAL", 4)],
    "PICKUP": [("PRIVATE", 40), ("COMMERCIAL", 40), ("AGRICULTURAL", 20)],
    "VAN": [("COMMERCIAL", 70), ("PRIVATE", 15), ("TAXI", 15)],
    "BUS": [("COMMERCIAL", 100)], "TRUCK": [("COMMERCIAL", 100)], "MOTORCYCLE": [("PRIVATE", 100)], "TRAILER": [("COMMERCIAL", 100)],
}
COLORS = ["White", "Silver", "Black", "Grey", "Blue", "Red", "Beige", "Brown", "Green"]
OPTIONAL_RIDERS = ["GCC", "RSA", "AGENCY", "REPL_CAR", "PAB"]
VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"

# claim damage patterns: list of zone groups; the generator picks one and sets severities
ZONE_PATTERNS = {
    "front": ["front_bumper", "hood", "fl_wing", "fr_wing", "windscreen"],
    "rear": ["rear_bumper", "trunk", "rl_quarter", "rr_quarter", "rear_glass"],
    "left": ["fl_wing", "fl_door", "rl_door", "rl_quarter"],
    "right": ["fr_wing", "fr_door", "rr_door", "rr_quarter"],
    "top": ["roof", "hood", "windscreen", "trunk", "rear_glass"],
}


def _w(rnd, pairs):
    items, weights = zip(*pairs)
    return rnd.choices(items, weights)[0]


class Loader:
    def __init__(self, policies, claims, seed, progress, years=5):
        self.rnd = random.Random(seed)
        self.n_policies, self.n_claims = policies, claims
        self.years = max(1, min(int(years), 10))
        self.progress = progress or (lambda *a: None)
        self.today = date.today()
        self.cur = {c.code: c for c in Currency.objects.all()}
        from motor.models import Cover, Product
        self.comp, self.tpl = Product.objects.get(code="COMP"), Product.objects.get(code="TPL")
        self.riders = {c.code: c for c in Cover.objects.filter(kind="RIDER")}
        self.used_plates, self.used_chassis = set(), set()
        self.policy_dates = {}     # policy id -> issue datetime
        self.proposals = {}        # proposal id -> created datetime
        self.chain_last = []       # (policy, vehicles) for policies that can be cancelled / claimed
        self.counts = {"policies": 0, "claims": 0, "failed": 0}

    # ------------------------------------------------------------------ users
    def users(self):
        from core.management.commands.seed_demo import Command
        cmd = Command()
        mk = cmd.user
        self.admin = mk("admin", "ADMIN", first="Admin")
        self.pm = mk("pm1", "PRODUCTION_MANAGER", first="Maryam")
        self.uw = mk("uw1", "UNDERWRITER", first="Aisha")
        agents = [mk("agent1", "AGENT", "AGENT", self.pm, first="Salim")] + [mk(f"agent{i}", "AGENT", "AGENT", self.pm, first=n) for i, n in ((2, "Hilal"), (3, "Badar"), (4, "Zainab"))]
        brokers = [mk("broker1", "BROKER", "BROKER", self.pm, first="Khalid")] + [mk(f"broker{i}", "BROKER", "BROKER", self.pm, first=n) for i, n in ((2, "Muna"), (3, "Sultan"))]
        self.handler = mk("claims1", "CLAIMS_HANDLER", first="Nasser")
        self.cmgr = mk("claimsmgr1", "CLAIMS_MANAGER", first="Fatma")
        mk("finance1", "FINANCE", first="Hamad")
        self.credit = mk("credit1", "CREDIT_CONTROL", first="Rashid")
        self.cust_user = mk("customer1", "CUSTOMER", first="Yousuf")
        UserProfile.objects.filter(user__username__in=[u.username for u in agents + brokers] + ["pm1", "uw1", "claims1", "claimsmgr1", "finance1", "credit1", "customer1"]).update(is_demo=True)
        self.producers = [(u, 5) for u in agents] + [(u, 3) for u in brokers] + [(self.pm, 1)]

    # ------------------------------------------------------------------ vouchers
    def vouchers(self):
        from motor.models import Voucher
        t = self.today
        spec = [
            ("WELCOME10", "Welcome offer: 10% off new comprehensive policies", "PERCENT", "10", None, t - timedelta(days=900), t + timedelta(days=150), None, 0),
            ("LOYAL5", "Loyalty renewal discount", "PERCENT", "5", None, t - timedelta(days=900), t + timedelta(days=150), None, 0),
            ("FLEET75", "Fleet promotion: 7.5% off", "PERCENT", "7.5", None, t - timedelta(days=900), t + timedelta(days=90), None, 500),
            ("OMR10", "OMR 10 off any comprehensive policy", "FIXED", "10", "OMR", t - timedelta(days=900), t + timedelta(days=150), 70, 0),
            ("USD25", "USD 25 off for USD policies", "FIXED", "25", "USD", t - timedelta(days=900), t + timedelta(days=150), 40, 0),
            ("RAMADAN20", "Ramadan special: expired campaign", "PERCENT", "20", None, t - timedelta(days=330), t - timedelta(days=60), None, 0),
            ("NEXTYEAR", "Scheduled campaign (not live yet)", "PERCENT", "8", None, t + timedelta(days=30), t + timedelta(days=120), None, 0),
        ]
        self.live_vouchers = []
        for code, desc, typ, val, cur, vf, vt, mx, minp in spec:
            v = Voucher.objects.create(code=code, description=desc, discount_type=typ, value=D(val), currency=self.cur.get(cur) if cur else None,
                                       valid_from=vf, valid_to=vt, max_uses=mx, min_net_premium=D(minp), issued_by=self.pm, is_demo=True)
            if vf <= t <= vt:
                self.live_vouchers.append(v)
        for i in range(8):    # a batch of single-use codes
            Voucher.objects.create(code=f"PROMO-{self.rnd.randrange(16**5):05X}", description="Single-use promotional code", discount_type="PERCENT",
                                   value=D("12"), valid_from=t - timedelta(days=60), valid_to=t + timedelta(days=60), max_uses=1, issued_by=self.uw, is_demo=True)

    # ------------------------------------------------------------------ people & vehicles
    def person(self):
        r = self.rnd
        if r.random() < 0.1:
            name = r.choice(EXPAT)
            nat = r.choice(["Indian", "Pakistani", "British", "Filipino", "Jordanian", "Egyptian", "Sri Lankan"])
        else:
            first = r.choice(FIRST_M if r.random() < 0.72 else FIRST_F)
            name, nat = f"{first} {r.choice(FAMILY)}", "Omani"
        return name, nat

    def customer(self, corporate=False):
        from motor.models import Customer
        r = self.rnd
        if corporate:
            name = f"{r.choice(CORP_A)} {r.choice(CORP_B)} {r.choice(['LLC', 'SAOC', 'SPC'])}"
            c = Customer(customer_type="CORPORATE", name=name, cr_number=str(r.randrange(1000000, 9999999)), phone=f"2{r.randrange(4000000, 4999999)}")
        else:
            name, nat = self.person()
            dob = date(r.randint(1960, 2004), r.randint(1, 12), r.randint(1, 28))
            c = Customer(customer_type="INDIVIDUAL", name=name, civil_id=str(r.randrange(10000000, 99999999)), date_of_birth=dob, nationality=nat,
                         phone=f"9{r.randrange(1000000, 9999999)}")
        c.email = c.name.split()[0].lower() + str(r.randrange(10, 999)) + "@example.com"
        c.address = r.choice(GOVERNORATES)
        c.is_demo = True
        c.save()
        return c

    def vehicle_spec(self, customer, corporate, start):
        from motor.models import Vehicle
        r = self.rnd
        if corporate:
            make, model, vt, cc, seats, price = r.choice([x for x in CATALOG if x[2] in ("PICKUP", "VAN", "TRUCK", "BUS", "SUV", "SALOON")])
            usage = r.choice(["COMMERCIAL", "COMMERCIAL", "RENTAL"]) if vt in ("SALOON", "SUV") else "COMMERCIAL"
            if customer.government_owned or (customer.customer_type == "CORPORATE" and r.random() < 0.15):
                usage = "GOVERNMENT"
                if not customer.government_owned:
                    customer.government_owned = True
                    customer.save(update_fields=["government_owned"])
        else:
            make, model, vt, cc, seats, price = r.choice(CATALOG)
            usage = _w(r, USAGE_BY_TYPE[vt])
        year = r.randint(max(2012, start.year - 14), start.year)
        while True:
            chassis = "".join(r.choice(VIN_CHARS) for _ in range(17))
            if chassis not in self.used_chassis:
                self.used_chassis.add(chassis)
                break
        while True:
            code, num = "".join(r.choice("ABCDEFGHJKLMNPRSTUVXYZ") for _ in range(2)), str(r.randrange(1000, 9999))
            if (code, num) not in self.used_plates:
                self.used_plates.add((code, num))
                break
        v = Vehicle.objects.create(customer=customer, make=make, model=model, year=year, plate_code=code, plate_number=num, chassis_no=chassis,
                                   engine_no=f"{make[:2].upper()}{r.randrange(10**6, 10**7)}", color=r.choice(COLORS), vehicle_type=vt, usage=usage, cc=cc,
                                   seats=seats, is_demo=True)
        v._price = price
        return v

    def market_value(self, v, start):
        age = max(0, start.year - v.year)
        val = v._price * (0.86 ** age) * self.rnd.uniform(0.92, 1.08)
        return max(1500, round(val / 100) * 100)

    # ------------------------------------------------------------------ policy creation
    def make_policy(self, cust, vehicles, product, cur, producer, start, *, renewal_of=None, ncd=0, voucher=None, drivers=None):
        from motor import services
        from motor.models import Driver, Policy, Proposal, ProposalRisk

        r = self.rnd
        issue = min(start - timedelta(days=r.randint(0, 6)), self.today)
        if renewal_of is not None:
            issue = min(start - timedelta(days=r.randint(1, 20)), self.today)
        end = start + timedelta(days=364)
        with clock.posting_date(issue):
            p = Proposal.objects.create(
                number=services.next_quote_number(), status=Proposal.QUOTE, product=product, customer=cust, producer=producer, currency=cur,
                fx_rate=cur.rate_to_base, start_date=start, end_date=end, valid_until=self.today + timedelta(days=30), ncd_years=ncd,
                voucher=voucher, renewal_of=renewal_of, is_demo=True,
                flex={"sales_channel": r.choice(["Walk-in", "Online", "Referral", "Broker desk"])} if r.random() < 0.7 else {})
            for v in vehicles:
                si, note = D(0), ""
                if product.includes_od:
                    if renewal_of is not None and v.base_value:
                        val, note = services.vehicle_value(v, start, product, cur, cur.rate_to_base)
                        si = val
                    else:
                        si = cur.quantize(D(self.market_value(v, start)) / cur.rate_to_base)
                        si = (si / 100).quantize(D(1)) * 100 or si
                pr = ProposalRisk.objects.create(proposal=p, vehicle=v, sum_insured=si, si_note=note[:160])
                opt = [self.riders[c] for c in OPTIONAL_RIDERS if self.riders.get(c) and self.riders[c].allowed_for(product) and r.random() < (0.22 if product.includes_od else 0.1)]
                pr.riders.set(opt)
                for d in (drivers or {}).get(v.pk, []):
                    Driver.objects.create(proposal_risk=pr, **d)
            services.reprice_proposal(p)
            services.convert_to_proposal(p)
            if p.status == Proposal.PENDING:
                p.status, p.decision_by = Proposal.APPROVED, r.choice([self.pm, self.uw])
                p.save()
            pol = services.issue_policy(p, producer)
        Policy.objects.filter(pk=pol.pk).update(is_demo=True)
        pol.is_demo = True
        self.policy_dates[pol.pk] = datetime.combine(issue, time(r.randint(8, 16), r.randint(0, 59)), tzinfo=timezone.get_current_timezone())
        self.proposals[p.pk] = self.policy_dates[pol.pk] - timedelta(days=r.randint(0, 3))
        return pol

    def driver_specs(self, cust, vehicle, start):
        r = self.rnd
        out = []
        for _ in range(r.choice([1, 1, 2])):
            young = r.random() < 0.4
            age = r.randint(18, 24) if young else r.randint(26, 58)
            name = f"{r.choice(FIRST_M + FIRST_F)} {cust.name.split()[-1] if cust.customer_type == 'INDIVIDUAL' else r.choice(FAMILY)}"
            out.append(dict(name=name, licence_no=str(r.randrange(100000, 999999)), date_of_birth=date(start.year - age, r.randint(1, 12), r.randint(1, 28)),
                            licence_issue_date=date(start.year - max(1, age - 18), 1, 1), nationality="Omani",
                            relationship=r.choice(["SPOUSE", "CHILD", "FAMILY", "EMPLOYEE", "FRIEND"]), added_on=start))
        return out

    def build_policies(self):
        r = self.rnd
        made, attempts = 0, 0
        while made < self.n_policies and attempts < self.n_policies * 4:
            attempts += 1
            corporate = r.random() < 0.06
            # first policy anywhere in the window; each year ~78% renew until they lapse or reach today
            first = self.today - timedelta(days=r.randint(0, 365 * self.years - 1))
            room = (self.today - first).days // 365 + 1
            chain_len = 1
            while chain_len < room and r.random() < 0.78:
                chain_len += 1
            chain_len = min(chain_len, self.n_policies - made)
            starts = [first + timedelta(days=365 * k) for k in range(chain_len)]
            producer = r.choices([p for p, _ in self.producers], [w for _, w in self.producers])[0]
            cur = self.cur[_w(r, [("OMR", 82), ("USD", 7), ("AED", 5), ("SAR", 3), ("GBP", 3)])]
            product = self.comp if r.random() < 0.7 else self.tpl
            try:
                with transaction.atomic():
                    cust = self.customer(corporate)
                    n_veh = r.randint(3, 12) if corporate else 1
                    vehicles = [self.vehicle_spec(cust, corporate, starts[0]) for _ in range(n_veh)]
                    drivers = {}
                    if not corporate and product.includes_od and r.random() < 0.38:
                        drivers = {vehicles[0].pk: self.driver_specs(cust, vehicles[0], starts[0])}
                    prev, created = None, []
                    for i, st in enumerate(starts):
                        voucher = None
                        if product.includes_od and r.random() < 0.1 and self.live_vouchers:
                            voucher = r.choice(self.live_vouchers)
                        ncd = r.choice([0, 0, 1, 1, 2, 3, 5]) if prev is None else (0 if prev.claims.exists() else min(prev.proposal.ncd_years + 1, 5))
                        pol = self.make_policy(cust, [v for v in vehicles if v.pk], product, cur, producer, st, renewal_of=prev, ncd=ncd, voucher=voucher,
                                               drivers=drivers if prev is None else None)
                        created.append(pol)
                        prev = pol
                    self.chains.append((created, cust, vehicles))
                made += len(created)
                self.counts["policies"] = made
                self.progress("Policies", made, self.n_policies)
            except Exception:       # a rare unlucky combination (no rate, authority, etc.): skip it and carry on
                self.counts["failed"] += 1
                traceback.print_exc()

    # ------------------------------------------------------------------ lifecycle: endorsements, cancellations, expiry, payments
    def lifecycle(self):
        from motor import services
        from motor.models import Endorsement, Policy
        r = self.rnd
        all_pols = [(pol, last) for created, _, _ in self.chains for i, pol in enumerate(created) for last in [i == len(created) - 1]]
        for n, (pol, is_last) in enumerate(all_pols):
            pol.refresh_from_db()
            end_eff = min(self.today, pol.end_date)
            span = (end_eff - pol.start_date).days
            if span <= 20 or pol.status != "ACTIVE":
                continue
            roll = r.random()
            risk = pol.risks.first()
            try:
                if roll < 0.04 and is_last and span > 60:        # cancellation
                    eff = pol.start_date + timedelta(days=r.randint(30, span - 5))
                    with clock.posting_date(eff):
                        services.apply_endorsement(pol, Endorsement.CANCELLATION, eff, pol.producer)
                elif roll < 0.18:
                    eff = pol.start_date + timedelta(days=r.randint(5, span))
                    kind = r.choice(["ENGINE", "COLOR", "USAGE", "REGNO", "ADDDRIVER", "ADDITIONS", "REGNO"])
                    kw = {}
                    if kind == "ENGINE":
                        kw = {"new_engine_no": f"RE{r.randrange(10**6, 10**7)}"}
                    elif kind == "COLOR":
                        kw = {"new_color": r.choice(COLORS)}
                    elif kind == "USAGE":
                        if risk.usage != "COMMERCIAL":
                            kw = {"new_usage": "COMMERCIAL"}
                        else:
                            continue
                    elif kind == "REGNO":
                        while True:
                            code, num = "".join(r.choice("ABCDEFGHJKLMNPRSTUVXYZ") for _ in range(2)), str(r.randrange(1000, 9999))
                            if (code, num) not in self.used_plates:
                                self.used_plates.add((code, num))
                                break
                        kw = {"new_plate_code": code, "new_plate_number": num}
                    elif kind == "ADDDRIVER":
                        age = r.choice([19, 21, 23, 30, 41])
                        kw = {"driver_name": f"{r.choice(FIRST_M)} {r.choice(FAMILY)}", "driver_licence": str(r.randrange(100000, 999999)),
                              "driver_dob": date(eff.year - age, r.randint(1, 12), r.randint(1, 28)), "driver_relationship": r.choice(["SPOUSE", "CHILD", "FAMILY"]),
                              "driver_nationality": "Omani"}
                    elif kind == "ADDITIONS":
                        if not pol.product.includes_od:
                            continue
                        kw = {"accessories_value": D(r.choice([150, 300, 600, 900])), "note": r.choice(["Sound system", "Bull bar", "Alloy wheels"])}
                    with clock.posting_date(eff):
                        endo = services.apply_endorsement(pol, getattr(Endorsement, kind), eff, pol.producer, risk, **kw)
                    Endorsement.objects.filter(pk=endo.pk).update(created_at=datetime.combine(eff, time(11, 0), tzinfo=timezone.get_current_timezone()))
            except Exception:
                traceback.print_exc()
            if n % 25 == 0:
                self.progress("Endorsements", n, len(all_pols))
        Policy.objects.filter(is_demo=True, status="ACTIVE", end_date__lt=self.today).update(status="EXPIRED")

    def payments(self):
        from payments import services as ps
        from payments.models import Payment
        from motor.models import Policy
        r = self.rnd
        pols = list(Policy.objects.filter(is_demo=True).select_related("currency", "customer"))
        for n, pol in enumerate(pols):
            if pol.total_payable <= 0:
                continue
            issue = self.policy_dates.get(pol.pk)
            d0 = issue.date() if issue else pol.start_date
            roll = r.random()
            if roll < 0.12:
                continue                                  # unpaid
            if roll < 0.34:
                self.credit_policy(pol, d0)
                continue
            parts = [pol.total_payable] if roll < 0.74 else [None, None] if roll < 0.9 else [None]
            amounts = []
            if len(parts) == 2 or (len(parts) == 1 and parts[0] is None):
                first = pol.currency.quantize(pol.total_payable * D(r.choice(["0.5", "0.6", "0.4"])))
                amounts = [first] if len(parts) == 1 else [first, pol.total_payable - first]      # partial / two instalments
            else:
                amounts = parts
            pay_day = d0
            for amt in amounts:
                pay_day = min(pay_day + timedelta(days=r.randint(0, 9)), self.today)
                method = _w(r, [("CASH", 18), ("TRANSFER", 24), ("POS", 22), ("ONLINE", 30), ("CHEQUE", 6)])
                try:
                    with clock.posting_date(pay_day):
                        stamp = datetime.combine(pay_day, time(r.randint(8, 18), r.randint(0, 59)), tzinfo=timezone.get_current_timezone())
                        if method == "ONLINE":
                            pay = ps.request_online_payment(pol, amt, pol.producer, "Online payment (demo gateway)")
                            ps.mark_paid(pay, paid_at=stamp, gateway_txn_id="DEMO-" + "".join(r.choice("0123456789ABCDEF") for _ in range(10)),
                                         card_last4=str(r.randrange(1000, 9999)), reference="")
                        else:
                            ref = {"CHEQUE": f"CHQ{r.randrange(100000, 999999)}", "TRANSFER": f"TRF{r.randrange(10**6, 10**7)}", "POS": f"POS{r.randrange(10**5, 10**6)}"}.get(method, "")
                            pay = ps.record_receipt(pol, amt, method, pol.producer, reference=ref, paid_at=stamp)
                    Payment.objects.filter(pk=pay.pk).update(created_at=stamp, is_demo=True)
                except Exception:
                    traceback.print_exc()
                    break
            if n % 25 == 0:
                self.progress("Receipts", n, len(pols))

    def credit_policy(self, pol, d0):
        """A policy issued on credit: approved instalments, advance paid at issue, later instalments paid on time or late."""
        from payments import services as ps
        from payments.models import Payment
        from motor import credit
        from motor.models import CreditApproval
        r = self.rnd
        n = r.choice([2, 3, 4, 6, 9, 12])
        adv = pol.currency.quantize(pol.total_payable * D(r.choice(["0", "0", "0.1", "0.2"])))
        ca = CreditApproval.objects.create(proposal=pol.proposal, policy=pol, installments=n, advance_amount=adv, status=CreditApproval.APPROVED,
                                           requested_by=pol.producer, decided_by=self.credit, request_note="Corporate customer", is_demo=True)
        credit.build_schedule(pol, n, adv, start=d0)
        late = r.random() < 0.5
        for it in pol.installments.all():
            if it.due_date > self.today:
                break
            if late and it.number >= 1 and r.random() < 0.45:
                break                                     # stops paying: this and later instalments age
            pay_day = min(it.due_date + timedelta(days=r.randint(0, 6)), self.today)
            method = _w(r, [("CASH", 20), ("TRANSFER", 40), ("POS", 20), ("CHEQUE", 20)])
            ref = {"CHEQUE": f"CHQ{r.randrange(100000, 999999)}", "TRANSFER": f"TRF{r.randrange(10**6, 10**7)}"}.get(method, "")
            try:
                with clock.posting_date(pay_day):
                    stamp = datetime.combine(pay_day, time(r.randint(8, 18), r.randint(0, 59)), tzinfo=timezone.get_current_timezone())
                    pay = ps.record_receipt(pol, it.amount, method, pol.producer, reference=ref, paid_at=stamp, narration=f"Instalment {it.number}" if it.number else "Advance")
                Payment.objects.filter(pk=pay.pk).update(created_at=stamp, is_demo=True)
            except Exception:
                traceback.print_exc()
                break

    # ------------------------------------------------------------------ claims
    def zones_for(self, loss_type):
        r = self.rnd
        z = {}
        if loss_type == "COLLISION" or loss_type == "TP_PROPERTY":
            main = r.choice(["front", "front", "rear", "rear", "left", "right"])
            pool = ZONE_PATTERNS[main]
            for zone in r.sample(pool, r.randint(1, min(4, len(pool)))):
                z[zone] = _w(r, [(1, 35), (2, 45), (3, 20)])
        elif loss_type == "NATURAL":
            for zone in r.sample(ZONE_PATTERNS["top"], r.randint(2, 5)):
                z[zone] = _w(r, [(1, 45), (2, 45), (3, 10)])
        elif loss_type == "FIRE":
            for zone in r.sample(["hood", "front_bumper", "fl_wing", "fr_wing", "windscreen"], r.randint(2, 4)):
                z[zone] = _w(r, [(2, 30), (3, 70)])
        elif loss_type == "THEFT":
            for zone in r.sample(["windscreen", "fl_door", "fr_door", "rear_glass", "front_bumper"], r.randint(1, 3)):
                z[zone] = _w(r, [(1, 30), (2, 50), (3, 20)])
        elif loss_type == "OTHER":
            for zone in r.sample(list(ZONE_PATTERNS["left"] + ZONE_PATTERNS["right"]), r.randint(1, 2)):
                z[zone] = 1
        return z

    def damage_cost_omr(self, zones, vtype, make):
        from claims import ai
        r = self.rnd
        base = ai.rule_of_thumb(vtype, make, zones)
        return max(40.0, base * r.uniform(0.8, 1.3))

    def claims(self):
        from claims import services as cs
        from claims.models import Claim, Provider
        from motor.models import Policy
        r = self.rnd
        garages = list(Provider.objects.filter(provider_type="GARAGE"))
        cm = self.cmgr
        pols = list(Policy.objects.filter(is_demo=True, status__in=["ACTIVE", "EXPIRED", "CANCELLED"]).select_related("currency", "product").prefetch_related("risks__drivers"))
        r.shuffle(pols)
        made, i = 0, 0
        while made < self.n_claims and i < len(pols) * 3:
            pol = pols[i % len(pols)]
            i += 1
            lo = pol.start_date
            hi = min(self.today, pol.cancelled_on or pol.end_date)
            if (hi - lo).days < 3:
                continue
            risk = pol.risks.first()
            dol = lo + timedelta(days=r.randint(1, (hi - lo).days))
            comp = pol.product.includes_od
            loss = _w(r, [("COLLISION", 48), ("THEFT", 6), ("FIRE", 4), ("NATURAL", 9), ("TP_PROPERTY", 20), ("TP_INJURY", 5), ("OTHER", 8)]) if comp else \
                _w(r, [("NATURAL", 15), ("TP_PROPERTY", 55), ("TP_INJURY", 30)])
            zones = self.zones_for(loss)
            total_loss = (loss in ("THEFT", "FIRE") and r.random() < 0.35 and comp) or (loss == "COLLISION" and comp and r.random() < 0.05)
            cost = self.damage_cost_omr(zones, risk.vehicle_type, risk.make) if zones else r.choice([600, 1500, 4000, 9000])
            drv = None
            ds = list(risk.drivers.all())
            if ds and r.random() < 0.5:
                drv = r.choice(ds)
            age = r.randint(21, 62)
            kw = dict(date_of_loss=dol, loss_type=loss, cause_of_loss=self.cause(loss), location=r.choice(PLACES), governorate=r.choice(GOVERNORATES),
                      police_report_no=f"ROP/{dol.year}/{r.randrange(10000, 99999)}" if loss in ("COLLISION", "THEFT", "TP_INJURY", "TP_PROPERTY") else "",
                      estimated_damage=pol.currency.quantize(D(str(cost)) / pol.fx_rate), driver_age=None if drv else age,
                      young_driver=False if drv else (age < 25), undisclosed_driver=bool(not drv and r.random() < 0.05),
                      third_party_liable=loss in ("COLLISION", "TP_PROPERTY") and r.random() < 0.35, driver=drv, is_total_loss=total_loss,
                      damage_zones=zones, damage_notes="", **self.liable(loss), flex={"police_station": r.choice(["Seeb", "Ruwi", "Sohar", "Nizwa", "Salalah"])} if r.random() < 0.5 else {})
            reg_day = min(dol + timedelta(days=r.randint(0, 4)), self.today)
            try:
                with transaction.atomic(), clock.posting_date(reg_day):
                    claim = cs.register_claim(pol, risk, self.handler, **kw)
                    self.advance(claim, cs, garages, cm, reg_day, comp)
                made += 1
                Claim.objects.filter(pk=claim.pk).update(is_demo=True,
                                                         created_at=datetime.combine(reg_day, time(r.randint(8, 17), r.randint(0, 59)), tzinfo=timezone.get_current_timezone()))
                self.counts["claims"] = made
                self.progress("Claims", made, self.n_claims)
            except Exception:
                self.counts["failed"] += 1
                traceback.print_exc()

    def liable(self, loss):
        r = self.rnd
        if loss not in ("COLLISION", "TP_PROPERTY") or r.random() > 0.5:
            return {}
        ins = r.choice(["National Insurance", "Gulf Mutual", "Shield Insurance"])
        return dict(liable_party=r.choice(["Third-party driver", "Fleet operator"]), liable_insurer=ins, liable_insurer_email=ins.split()[0].lower() + "@claims.example",
                    their_policy_no=f"MV/{r.randrange(100000, 999999)}", their_vehicle=f"{r.choice(['Nissan Sunny', 'Toyota Corolla', 'Hyundai Accent'])} {r.randrange(1000, 9999)}")

    def injuries(self, claim, cs):
        """Bodily injuries on injury claims and the occasional severe accident (rarely with a death)."""
        r = self.rnd
        if claim.loss_type == "TP_INJURY" or (claim.loss_type == "COLLISION" and r.random() < 0.06):
            n = r.choice([1, 1, 2])
            for _ in range(n):
                sev = _w(r, [("MINOR", 45), ("MODERATE", 30), ("SEVERE", 15), ("DISABILITY", 6), ("DEATH", 4)])
                cs.add_injury(claim, self.handler, person_name=r.choice(["Salim Al Balushi", "Hamed Al Rashdi", "Mariam Al Kindi", "Ahmed Khan", "Rajesh Kumar"]),
                              role=r.choice(["TP_DRIVER", "TP_PASSENGER", "PEDESTRIAN", "PASSENGER"]), age=r.randint(18, 70), severity=sev,
                              injury_description={"MINOR": "Bruising", "MODERATE": "Fracture", "SEVERE": "Head injury", "DISABILITY": "Spinal injury", "DEATH": "Fatal injuries"}[sev],
                              compensation_reserve=D("0"), medical_cost=D(r.randint(50, 900)) if sev != "DEATH" else D("0"))

    def legal(self, claim, cs, day):
        r = self.rnd
        if r.random() > 0.05 or claim.status in ("REJECTED",):
            return
        cs.set_legal(claim, True, self.handler)
        cs.add_legal_action(claim, self.handler, action_type="DEMAND", action_date=day, legal_cost=D("120"), note="Demand before action")
        if r.random() < 0.6:
            cs.add_legal_action(claim, self.handler, action_type="FILED", action_date=min(day + timedelta(days=20), self.today), court="Primary Court, Muscat",
                                case_no=f"{day.year}/{r.randrange(100, 999)}", amount_claimed=claim.reserve, next_date=self.today + timedelta(days=r.randint(5, 40)))

    def finish_paperwork(self, claim, cs):
        """Garage invoice and settlement, and the excess, so the claim can be closed."""
        claim.refresh_from_db()
        for e in cs.active_estimates(claim):
            cs.record_invoice(e, f"INV{self.rnd.randrange(10**4, 10**5)}", e.total, self.today)
            cs.settle_estimate(e)
        cs.record_excess(claim, claim.excess_applied, self.today)

    def cause(self, loss):
        return self.rnd.choice({
            "COLLISION": ["Rear-ended while stopped at a traffic light.", "Collision at a roundabout; the other driver failed to give way.",
                          "Lost control on a wet road and hit the barrier.", "Side-swiped while changing lanes on the expressway."],
            "THEFT": ["Vehicle stolen from outside the residence overnight.", "Break-in: windows smashed and the audio unit stolen."],
            "FIRE": ["Engine compartment caught fire while driving.", "Electrical fault caused a fire while parked."],
            "NATURAL": ["Flash flood in the wadi submerged the vehicle.", "Hail and strong winds damaged the body panels."],
            "TP_PROPERTY": ["Insured vehicle hit a parked car in a car park.", "Insured vehicle hit a boundary wall while reversing."],
            "TP_INJURY": ["Pedestrian injured at a crossing.", "Passenger in the other vehicle injured in the collision."],
            "OTHER": ["Vandalism: scratches along both sides.", "Damage caused by a falling object."],
        }[loss])

    def advance(self, claim, cs, garages, cm, reg_day, comp):
        from claims.models import Claim, Subrogation
        r = self.rnd
        self.injuries(claim, cs)
        self.legal(claim, cs, reg_day)
        cur = claim.policy.currency
        status = _w(r, [("OPEN", 12), ("ASSESSED", 12), ("APPROVED", 14), ("SETTLED", 22), ("CLOSED", 30), ("REJECTED", 10)])
        # recent losses cannot be far along
        age = (self.today - reg_day).days
        if age < 10 and status in ("SETTLED", "CLOSED"):
            status = "APPROVED"
        if status == "OPEN":
            return
        step = reg_day + timedelta(days=min(r.randint(1, 5), age))
        with clock.posting_date(step):
            if status == "ASSESSED":
                claim.reserve = cur.quantize(claim.estimated_damage * D(r.choice(["1.0", "1.1", "0.9"])))
                claim.status = Claim.ASSESSED
                claim.save()
                from accounting import services as acc
                acc.sync_claim(claim, self.handler)
                return
            if status == "REJECTED":
                claim.status = Claim.REJECTED
                claim.save(update_fields=["status"])
                from accounting import services as acc
                acc.sync_claim(claim, self.handler)
                return
            garage = r.choice(garages)
            if not claim.is_total_loss and claim.loss_type not in ("TP_INJURY",) and garages:
                dmg = float(claim.estimated_damage)
                basis = r.choice(["BOTH", "BOTH", "FIXED", "LABOUR", "PARTS"])
                cs.check_estimate(claim, garage, basis, D(str(round(dmg * 0.4, 3))), D(str(round(dmg * 0.6, 3))), D(str(round(dmg, 3))) if basis == "FIXED" else D("0"), r.randint(5, 20))
            approved = D(0) if claim.is_total_loss else cur.quantize(claim.estimated_damage * D(str(r.uniform(0.85, 1.05))))
            cs.approve_claim(claim, approved or None, self.cmgr, total_loss=claim.is_total_loss)
        if status == "APPROVED":
            if r.random() < 0.5:
                with clock.posting_date(min(step + timedelta(days=r.randint(1, 6)), self.today)):
                    self.pay(claim, cs, "FULL" if (claim.is_total_loss or claim.loss_type == "TP_INJURY") else "INTERIM")
            return
        done = min(step + timedelta(days=r.randint(3, 12)), self.today)
        with clock.posting_date(done):
            if claim.is_total_loss or claim.loss_type == "TP_INJURY" or not claim.garage_id:
                self.pay(claim, cs, "FULL")
            else:
                self.pay(claim, cs, "INTERIM")
        if not (claim.is_total_loss or claim.loss_type == "TP_INJURY" or not claim.garage_id):
            comp_day = min(done + timedelta(days=r.randint(5, 38)), self.today)
            with clock.posting_date(comp_day):
                cs.complete_repair(claim, comp_day, rework=r.random() < 0.06, customer_score=r.randint(3, 5))
                if comp_day >= done:
                    self.pay(claim, cs, "FINAL")
        claim.refresh_from_db()
        if claim.third_party_liable and claim.status in ("APPROVED", "SETTLED") and hasattr(claim, "subrogation"):
            sub = claim.subrogation
            sub.liable_party = r.choice(["Third-party driver", "Al Khaleej Insurance policyholder", "Fleet operator"])
            sub.liable_insurer = r.choice(["National Insurance", "Gulf Mutual", "Shield Insurance"])
            sub.recovery_status = r.choice(["DEMAND_SENT", "IN_NEGOTIATION", "IN_LITIGATION"])
            sub.save()
        if status == "CLOSED":
            end_day = min(done + timedelta(days=45), self.today)
            with clock.posting_date(end_day):
                claim.refresh_from_db()
                if hasattr(claim, "subrogation"):
                    cs.record_subrogation_recovery(claim, claim.subrogation.reserve_amount or claim.net_payable)
                claim.refresh_from_db()
                try:
                    self.finish_paperwork(claim, cs)
                    cs.close_claim(claim)
                except cs.ClaimError:
                    pass

    def pay(self, claim, cs, stage):
        claim.refresh_from_db()
        try:
            cs.pay_claim(claim, stage, claim.garage.name if claim.garage_id else claim.policy.customer.name, f"CP{self.rnd.randrange(10**5, 10**6)}")
        except cs.ClaimError:
            pass

    # ------------------------------------------------------------------ reinsurance cash and commission settlements
    def ri_collections(self):
        """Reinsurers pay most settled claims some weeks after the claim is paid; the rest stays receivable."""
        from claims import services as cs
        from claims.models import Claim, Recovery
        r = random.Random(self.rnd.random())
        for claim in Claim.objects.filter(is_demo=True, status__in=["SETTLED", "CLOSED"]).prefetch_related("payments"):
            exp = claim.recoveries.filter(recovery_type="REINSURANCE", received_on__isnull=True).first()
            pays = list(claim.payments.all())
            if not exp or exp.amount <= 0 or not pays or r.random() > 0.65:
                continue
            day = min(max(p.paid_on for p in pays) + timedelta(days=r.randint(20, 75)), self.today)
            with clock.posting_date(day):
                cs.add_recovery(claim, Recovery.REINSURANCE, exp.amount, "Received from reinsurers (demo)")

    def xol_settlements(self):
        """Pay XoL deposit instalments as they fell due and lift expired treaties to their minimum premium."""
        from reinsurance import engine
        from reinsurance.models import MDPInstallment, Treaty
        for inst in MDPInstallment.objects.filter(paid=False, due_date__lte=self.today, treaty__treaty_type="XOL").select_related("treaty").order_by("due_date"):
            with clock.posting_date(inst.due_date):
                e = engine.pay_installment(inst, self.admin, reference=f"RI{inst.pk:05d}")
            if e:
                e.is_demo = True
                e.save(update_fields=["is_demo"])
        for t in Treaty.objects.filter(treaty_type="XOL", superseded_by__isnull=True, effective_to__lt=self.today):
            with clock.posting_date(min(t.effective_to + timedelta(days=30), self.today)):
                e = engine.post_xol_adjustment(t, self.admin)
            if e:
                e.is_demo = True
                e.save(update_fields=["is_demo"])

    def commission_settlements(self):
        """Pay brokers and agents the commission on premium collected for business older than 30 days."""
        from accounting.models import JournalEntry
        from motor import commissions
        from motor.models import Commission
        upto = self.today - timedelta(days=30)
        for pid in Commission.objects.filter(policy__is_demo=True).values_list("producer_id", flat=True).distinct():
            producer = User.objects.get(pk=pid)
            with clock.posting_date(self.today - timedelta(days=10)):
                st = commissions.settle(producer, self.admin, reference=f"BT{self.rnd.randrange(10**5, 10**6)}", note="Monthly commission settlement (demo)",
                                        upto=upto, is_demo=True)
            if st:
                JournalEntry.objects.filter(source=st.number).update(is_demo=True)

    # ------------------------------------------------------------------ finish
    def finish(self):
        from accounting import services as acc
        from motor.models import Proposal
        from motor.models import Policy
        ends, d = [], min(self.policy_dates.values()).date().replace(day=1) if self.policy_dates else self.today.replace(day=1)
        while d < self.today.replace(day=1):
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            ends.append(nxt - timedelta(days=1))
            d = nxt
        ends.append(self.today)
        self.progress("Revenue recognition (IFRS 17)", 0, len(ends))
        with audit.suspended():
            for i, e in enumerate(ends):
                with clock.posting_date(e):
                    acc.recognise_revenue(e, self.admin)
                self.progress("Revenue recognition (IFRS 17)", i + 1, len(ends))
            for pid, dt in self.policy_dates.items():
                Policy.objects.filter(pk=pid).update(issued_at=dt)
            for pid, dt in self.proposals.items():
                Proposal.objects.filter(pk=pid).update(created_at=dt)
            from accounting.models import FinancialDocument, JournalEntry
            JournalEntry.objects.filter(policy__is_demo=True).update(is_demo=True)
            JournalEntry.objects.filter(claim__is_demo=True).update(is_demo=True)
        from payments.models import Payment
        Payment.objects.filter(policy__is_demo=True).update(is_demo=True)
        FinancialDocument.objects.filter(policy__is_demo=True).update(is_demo=True)
        from motor.models import VoucherRedemption
        # tidy single-use / capped voucher states are derived from redemptions: nothing to store

    def budgets(self):
        """Approved budgets for every year of the demo window (to this year), a forecast and a draft rollover: all flagged demo."""
        from budgeting import actuals as A, engine
        from budgeting.models import BudgetLine, BudgetVersion
        r = self.rnd
        y = self.today.year
        cutoff = A.last_closed(self.today)
        self.progress("Budgets", 0, 3)
        for yr in range(y - self.years + 1, y + 1):
            act = A.flat(yr - 1, yr)
            v = BudgetVersion.objects.create(name=f"Budget {yr}", year=yr, kind="BUDGET", status="APPROVED", created_by=self.admin, is_demo=True,
                                             method="Demo: derived from actuals with random planning error", note="Demo data")
            rows = []
            for p in engine.pids():
                for met in engine.STORED:
                    closed = [act.get((p, met, yr, m)) for m in range(1, 13) if (yr, m) <= cutoff and act.get((p, met, yr, m))]
                    avg = sum(closed) / len(closed) if closed else Decimal(0)
                    for mo in range(1, 13):
                        base = act.get((p, met, yr, mo)) if (yr, mo) <= cutoff else None
                        if base is None and (yr, mo) <= cutoff:
                            base = avg * Decimal("0.8")           # closed month without any activity: plan from the average
                        elif base is None:
                            ly = act.get((p, met, yr - 1, mo))
                            base = ly * Decimal("1.08") if ly else avg
                        val = Decimal(str(round(float(base) * r.uniform(0.9, 1.12), 3)))
                        rows.append([p, met, mo, val])
            closed_months = [m for m in range(1, 13) if (yr, m) <= cutoff]
            if closed_months:             # a few deliberate planning misses so the variance outlier detector has something to find
                for met, factor in (("CLAIMS", 0.45), ("GWP", 1.7), ("COMM", 0.5)):
                    mo = r.choice(closed_months)
                    for row in rows:
                        if row[1] == met and row[2] == mo and row[0] == engine.pids()[0]:
                            row[3] = Decimal(str(round(float(row[3]) * factor, 3)))
            BudgetLine.objects.bulk_create([BudgetLine(version=v, product_id=p, metric=m, month=mo, amount=(Decimal(round(a)) if m == "POLICIES" else a)) for p, m, mo, a in rows])
            self.progress("Budgets", min(2, yr - (y - self.years)), 3)
        cur = BudgetVersion.objects.get(year=y, kind="BUDGET", is_demo=True)
        fv = engine.build_forecast(cur, y, "BUDGET_ADJ", self.admin, cutoff, is_demo=True)
        fv.status = "APPROVED"
        fv.save()
        engine.rollover(cur, y + 1, "ACTUALS", 6, self.admin, is_demo=True)
        self.progress("Budgets", 3, 3)

    def cat_and_xol(self):
        """Catastrophe events with flagged claims, plus cat XoL, aggregate XoL and stop-loss treaties for the years of the book."""
        from claims.models import CatEvent, Claim
        from reinsurance import engine, xol
        from reinsurance.models import Reinsurer, Treaty
        r = self.rnd
        reins = Reinsurer.objects.order_by("id").last()
        years = sorted({d.year for d in Claim.objects.filter(is_demo=True).values_list("date_of_loss", flat=True)})
        for y in years:
            f, t = date(y, 1, 1), date(y, 12, 31)
            specs = [(f"TR-CAT-{y}", f"Catastrophe XoL {y}", dict(xol_kind="CAT", xol_retention=D("2000"), xol_limit=D("80000"), est_gnrpi=D("150000"), rate_pct=D("1.8"), rank=4, reinstatements=1, reinstatement_pct=D("100"))),
                     (f"TR-AGG-{y}", f"Aggregate XoL {y}", dict(xol_kind="AGGREGATE", aad=D("60000"), aal=D("120000"), est_gnrpi=D("150000"), rate_pct=D("1.2"), rank=5)),
                     (f"TR-SL-{y}", f"Stop Loss {y}", dict(xol_kind="STOP_LOSS", sl_attach_pct=D("85"), sl_limit_pct=D("25"), est_gnrpi=D("150000"), rate_pct=D("0.9"), rank=6, premium_basis="BURNING_COST", bc_min_rate=D("0.5"), bc_max_rate=D("3")))]
            for ref, name, kw in specs:
                if not Treaty.objects.filter(reference=ref).exists():
                    x = Treaty.objects.create(reference=ref, name=name, treaty_type="XOL", reinsurer=reins, effective_from=f, effective_to=t, deposit_pct=D("90"), installments=4, **kw)
                    engine.schedule_mdp(x)
        names = [("Cyclone Shaheen floods", "CYCLONE"), ("Muscat flash floods", "FLOOD"), ("Sohar warehouse fire", "FIRE")]
        for n, y in enumerate(years[-2:] if len(years) > 1 else years):
            pool = list(Claim.objects.filter(is_demo=True, date_of_loss__year=y, loss_type__in=["COLLISION", "NATURAL", "FIRE", "OTHER"]).order_by("id")[:40])
            if len(pool) < 6:
                continue
            nm, typ = names[n % len(names)]
            day = date(y, r.randint(2, 11), r.randint(3, 25))
            ev = CatEvent.objects.create(code=f"CAT-{y}-{n + 1:02d}", name=nm, event_type=typ, date_from=day, date_to=day + timedelta(days=2), description="Demo event", is_demo=True)
            for c in r.sample(pool, min(len(pool), r.randint(8, 14))):
                Claim.objects.filter(pk=c.pk).update(cat_event=ev, date_of_loss=day + timedelta(days=r.randint(0, 2)))
            for t in Treaty.objects.filter(xol_kind="CAT", effective_from__year=y):
                row = xol.post_position(t, ev, self.admin)
                if row:
                    from reinsurance.models import AggRecovery
                    AggRecovery.objects.filter(pk=row.pk).update(is_demo=True)
                    from accounting.models import JournalEntry
                    JournalEntry.objects.filter(source=t.reference, entry_type="XOL_REC").update(is_demo=True)

    def pnc_book(self):
        """P&C treaties, ~45 policies across the classes with receipts / credit, and claims (some flagged as catastrophes)."""
        from claims.models import CatEvent
        from motor.models import Customer
        from core.models import Currency
        from pnc import services as sv
        from pnc.classes import seed_classes
        from pnc.models import PncClass, PncPolicy, PncRisk
        from reinsurance import engine
        from reinsurance.models import Reinsurer, Treaty
        r = self.rnd
        seed_classes()
        reins = Reinsurer.objects.order_by("id").first()
        y = clock.today().year
        specs = [("PNC-QS", "P&C Quota Share", dict(treaty_type="QUOTA", cession_pct=D("30"), rank=1, qs_limit=D("150000"))),
                 ("PNC-SUR", "P&C Surplus", dict(treaty_type="SURPLUS", retention=D("100000"), capacity=D("1000000"), rank=2)),
                 ("PNC-XOL", "P&C Risk XoL", dict(treaty_type="XOL", xol_kind="RISK", xol_retention=D("15000"), xol_limit=D("100000"), est_gnrpi=D("200000"), rate_pct=D("3"), rank=3,
                                                  deposit_pct=D("90"), installments=4))]
        for ref, name, kw in specs:
            if not Treaty.objects.filter(reference=ref).exists():
                Treaty.objects.create(reference=ref, name=name, reinsurer=reins, effective_from=date(y - 2, 1, 1), effective_to=date(y + 1, 12, 31), business="PNC", **kw)
        users = {n: User.objects.filter(username=n).first() for n in ("agent1", "broker1", "uw1", "claims1", "claimsmgr1", "finance1", "credit1")}
        if not users["agent1"] or not users["uw1"]:
            return
        customers = list(Customer.objects.filter(is_demo=True).order_by("id")[:60])
        classes = list(PncClass.objects.filter(active=True))
        omr, usd = Currency.objects.get(code="OMR"), Currency.objects.filter(code="USD").first()
        names = ["Warehouse", "Office block", "Factory", "Cargo shipment", "Vessel", "Construction site", "Generator plant", "Shop", "Hotel", "Workshop"]
        made, today = [], clock.today()
        for i in range(45):
            cls = classes[i % len(classes)]
            producer = users["agent1"] if i % 3 else (users["broker1"] or users["agent1"])
            start = today - timedelta(days=r.randint(5, 300))
            cur = usd if (usd and i % 9 == 0) else omr
            pol = PncPolicy.objects.create(quote_no=sv.seq("PQ"), pnc_class=cls, customer=customers[i % len(customers)] if customers else Customer.objects.first(), producer=producer,
                                           currency=cur, fx_rate=cur.rate_to_base, start_date=start, end_date=start + timedelta(days=364), interest=r.choice(names), is_demo=True)
            for n in range(1, r.randint(1, 3) + 1):
                si = D(r.choice([20000, 45000, 80000, 150000, 400000, 900000, 2500000])) / (cur.rate_to_base if cur.rate_to_base else 1)
                PncRisk.objects.create(policy=pol, number=n, description=f"{r.choice(names)} {n}", location=r.choice(["Muscat", "Sohar", "Salalah", "Sur", "Nizwa"]), sum_insured=si.quantize(D("1")),
                                       value_at_risk=(si * D("1.5")).quantize(D("1")) if cls.average_applies and r.random() < .3 else D(0), rate_pct=cls.rate_pct,
                                       loading_pct=D(r.choice([0, 0, 0, 10, -5])))
            try:
                sv.submit(pol, producer)
                if pol.status == "PENDING":
                    sv.decide(pol, users["uw1"], True, "Demo approval")
                if pol.status != "APPROVED":
                    continue
                if i % 4 == 3 and users["credit1"]:
                    c = sv.request_credit(pol, producer, r.choice([3, 6, 12]), (pol.total_payable * D("0.1")).quantize(D("1")))
                    sv.decide_credit(c, users["credit1"], True, "Demo credit")
                    sv.issue(pol, producer, "CREDIT", "TRANSFER", "DEMO-ADV", "DEMO-FAC")
                    due = sum(it.amount for it in pol.installments.all() if it.due_date <= today)
                    if due > pol.paid_amount:
                        sv.record_receipt(pol, min(due - pol.paid_amount, pol.balance_due), "TRANSFER", users["finance1"], "DEMO-INST")
                else:
                    sv.issue(pol, producer, "CASH", r.choice(["CASH", "CHEQUE", "TRANSFER"]), "DEMO-REF", "DEMO-FAC")
                PncPolicy.objects.filter(pk=pol.pk).update(is_demo=True)
                made.append(pol)
            except sv.PncError:
                continue
        # claims
        cat = CatEvent.objects.filter(is_demo=True).order_by("-id").first()
        for k, pol in enumerate(r.sample(made, min(len(made), 14))):
            risk = pol.risks.filter(active=True).first()
            ld = max(pol.start_date, today - timedelta(days=r.randint(1, 120)))
            try:
                c = sv.register_claim(pol, risk, users["claims1"], ld, r.choice(["FIRE", "THEFT", "FLOOD", "MACHINERY", "MARINE", "LIABILITY_PD"]),
                                      (risk.sum_insured * D(r.choice(["0.01", "0.03", "0.08", "0.2"]))).quantize(D("1")), "Demo loss", "Demo location")
                if k % 5 == 4 and cat:
                    c.cat_event = cat
                    c.save(update_fields=["cat_event"])
                if k % 7 == 6:
                    continue                                           # left open
                sv.assess(c, users["claims1"], (c.reserve * D(r.choice(["0.8", "1", "1.1"]))).quantize(D("1")))
                if k % 7 == 5:
                    continue
                sv.approve(c, users["claimsmgr1"])
                if k % 3:
                    sv.pay(c, users["claimsmgr1"], (c.outstanding / 2).quantize(D("1")), pol.customer.name)
                    if k % 2:
                        sv.pay(c, users["claimsmgr1"], c.outstanding, pol.customer.name, "FINAL")
                        if k % 4 == 1:
                            sv.close(c, users["claimsmgr1"])
                PncClaim_flag = type(c).objects.filter(pk=c.pk)
                PncClaim_flag.update(is_demo=True)
            except sv.PncError:
                continue
        from pnc import ledger
        ledger.recognise_all(today, users["finance1"])
        self.counts["pnc_policies"] = len(made)

    def run(self):
        self.chains = []
        self.progress("Preparing", 0, 1)
        self.users()
        self.vouchers()
        self.build_policies()
        self.lifecycle()
        self.payments()
        self.claims()
        self.cat_and_xol()
        self.ri_collections()
        self.finish()
        self.commission_settlements()
        self.xol_settlements()
        self.pnc_book()
        self.budgets()
        return self.counts


def load_demo(policies=1000, claims=300, seed=42, progress=None, years=5):
    """Create the demo book. Safe to call once: if demo policies already exist it adds nothing (unload first)."""
    from motor.models import Policy
    if Policy.objects.filter(is_demo=True).exists():
        raise RuntimeError("Demo data is already loaded. Unload it first.")
    from motor.models import Product
    if not Product.objects.filter(code="COMP").exists():
        call_command("seed_demo", "--no-demo", verbosity=0)
    from django.test.utils import override_settings
    with audit.suspended(), override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend"):
        # flag everything created during the run (Customer/Vehicle/Proposal/... are flagged at creation or just below)
        loader = Loader(policies, claims, seed, progress, years)
        counts = loader.run()
    from motor.models import Proposal
    Proposal.objects.filter(customer__is_demo=True).update(is_demo=True)
    from core.models import AuditLog
    AuditLog.objects.create(action="create", model="core.DemoRun", object_id="load", object_repr=f"Demo data loaded: {counts['policies']} policies, {counts['claims']} claims", data=counts)
    return counts


def demo_counts():
    from accounting.models import JournalEntry
    from claims.models import Claim
    from motor.models import Customer, Policy, Vehicle, Voucher
    from payments.models import Payment
    return {"policies": Policy.objects.filter(is_demo=True).count(), "claims": Claim.objects.filter(is_demo=True).count(),
            "customers": Customer.objects.filter(is_demo=True).count(), "vehicles": Vehicle.objects.filter(is_demo=True).count(),
            "vouchers": Voucher.objects.filter(is_demo=True).count(), "payments": Payment.objects.filter(is_demo=True).count(),
            "journals": JournalEntry.objects.filter(is_demo=True).count(), "budgets": __import__("budgeting.models", fromlist=["x"]).BudgetVersion.objects.filter(is_demo=True).count(), "users": UserProfile.objects.filter(is_demo=True).count()}


def unload_demo(remove_users=False, progress=None):
    """Delete every record flagged is_demo, in dependency order. Reference data and real records are untouched."""
    from accounting.models import FinancialDocument, JournalEntry
    from claims.models import Claim, Provider
    from motor.models import Customer, DocumentShare, Policy, Proposal, Vehicle, Voucher
    from payments.models import Payment
    from budgeting.models import BudgetVersion
    from motor.models import CommissionSettlement

    from pnc.models import PncClaim, PncPolicy
    steps = [
        ("P&C claims", lambda: PncClaim.objects.filter(is_demo=True) | PncClaim.objects.filter(policy__is_demo=True)),
        ("Claims", lambda: Claim.objects.filter(is_demo=True) | Claim.objects.filter(policy__is_demo=True)),
        ("Catastrophe events", lambda: __import__("claims.models", fromlist=["x"]).CatEvent.objects.filter(is_demo=True)),
        ("XoL recoveries", lambda: __import__("reinsurance.models", fromlist=["x"]).AggRecovery.objects.filter(is_demo=True)),
        ("Journals", lambda: JournalEntry.objects.filter(is_demo=True)),
        ("P&C policies", lambda: PncPolicy.objects.filter(is_demo=True)),
        ("Documents", lambda: FinancialDocument.objects.filter(is_demo=True)),
        ("Receipts", lambda: Payment.objects.filter(is_demo=True)),
        ("Shared links", lambda: DocumentShare.objects.filter(policy__is_demo=True)),
        ("Policies", lambda: Policy.objects.filter(is_demo=True)),
        ("Proposals", lambda: Proposal.objects.filter(is_demo=True) | Proposal.objects.filter(customer__is_demo=True)),
        ("Vehicles", lambda: Vehicle.objects.filter(is_demo=True)),
        ("Budgets", lambda: BudgetVersion.objects.filter(is_demo=True)),
        ("Commission settlements", lambda: CommissionSettlement.objects.filter(is_demo=True)),
        ("Vouchers", lambda: Voucher.objects.filter(is_demo=True)),
        ("Customers", lambda: Customer.objects.filter(is_demo=True)),
    ]
    removed = {}
    with audit.suspended():
        # a real (non-demo) policy or quote may have been written for a demo customer or vehicle: keep those and stop calling them demo
        kept = Customer.objects.filter(is_demo=True).filter(
            Q(pnc_policies__is_demo=False) | Q(policies__is_demo=False)).distinct()
        kept_ids = list(kept.values_list("id", flat=True))
        Customer.objects.filter(id__in=kept_ids).update(is_demo=False)
        Vehicle.objects.filter(customer_id__in=kept_ids).update(is_demo=False)
        removed["customers kept (used by real records)"] = len(kept_ids)
        for i, (label, qs) in enumerate(steps):
            if progress:
                progress(label, i, len(steps) + 1)
            n = qs().count()
            qs().delete()
            removed[label.lower()] = n
        from reinsurance.models import MDPInstallment
        MDPInstallment.objects.filter(paid=True).update(paid=False, paid_on=None, reference="")      # demo payments were posted in demo journals
        for p in Provider.objects.all():
            p.refresh_score()
        if remove_users:
            ids = list(User.objects.filter(profile__is_demo=True, is_superuser=False).values_list("id", flat=True))
            User.objects.filter(id__in=ids).delete()
            removed["users"] = len(ids)
    from core.models import AuditLog
    AuditLog.objects.create(action="delete", model="core.DemoRun", object_id="unload", object_repr="Demo data unloaded", data=removed)
    if progress:
        progress("Done", len(steps) + 1, len(steps) + 1)
    return removed


# --------------------------------------------------------------------------- background runner used by the web page
def start_background(action, params, user):
    """Start load/unload in a daemon thread and return the DemoRun row the page polls."""
    if DemoRun.objects.filter(status=DemoRun.RUNNING).exists():
        raise RuntimeError("Another demo-data job is already running.")
    run = DemoRun.objects.create(action=action, params=params, started_by=user)

    def work():
        close_old_connections()
        try:
            def progress(stage, done, total):
                DemoRun.objects.filter(pk=run.pk).update(stage=stage, done=done, total=total)

            if action == DemoRun.LOAD:
                counts = load_demo(params.get("policies", 1000), params.get("claims", 300), params.get("seed", 42), progress, params.get("years", 5))
                msg = f"Loaded {counts['policies']} policies and {counts['claims']} claims" + (f" ({counts['failed']} skipped)" if counts["failed"] else "") + "."
            else:
                removed = unload_demo(params.get("remove_users", False), progress)
                msg = "Removed " + ", ".join(f"{v} {k}" for k, v in removed.items() if v) + "."
            DemoRun.objects.filter(pk=run.pk).update(status=DemoRun.DONE, message=msg, finished_at=timezone.now())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            DemoRun.objects.filter(pk=run.pk).update(status=DemoRun.FAILED, message=f"{type(e).__name__}: {e}", finished_at=timezone.now())
        finally:
            connection.close()

    threading.Thread(target=work, daemon=True, name="demo-data").start()
    return run

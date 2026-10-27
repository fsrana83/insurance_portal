"""Standard P&C classes with indicative tariffs and risk questions. Rates are illustrative: replace with the company's filed tariff in Admin."""
from decimal import Decimal as D

from .models import PncClass


def t(key, label, type="text", choices=None):
    f = {"key": key, "label": label, "type": type}
    if choices:
        f["choices"] = choices
    return f


CONSTRUCTION = t("construction", "Construction", "choice", ["Concrete / masonry", "Steel frame", "Timber / light"])
CLASSES = [
    # code, name, group, si_label, rate, min, ded%, ded_min, comm, referral, average, fields
    ("FIRE", "Fire & Allied Perils", "FIRE", "Sum insured (buildings / contents)", "0.12", 25, 0, 100, 15, False, True,
     [CONSTRUCTION, t("occupancy", "Occupancy"), t("storeys", "Number of storeys", "number"), t("sprinklers", "Sprinklers fitted", "bool")]),
    ("BURGLARY", "Burglary & Theft", "FIRE", "Sum insured", "0.25", 25, 10, 100, 15, False, False, [t("alarm", "Alarm system", "bool"), t("guard", "Security guard", "bool")]),
    ("PROPERTY_AR", "Property All Risks", "FIRE", "Sum insured", "0.18", 50, 5, 250, 12.5, True, True, [CONSTRUCTION, t("occupancy", "Occupancy")]),
    ("BI", "Business Interruption", "FIRE", "Gross profit insured", "0.15", 50, 0, 0, 12.5, True, False, [t("indemnity", "Indemnity period (months)", "number")]),
    ("CARGO", "Marine Cargo", "MARINE", "Cargo value", "0.08", 25, 0, 50, 10, False, False,
     [t("voyage", "Voyage / route"), t("conveyance", "Conveyance", "choice", ["Sea", "Air", "Road", "Multimodal"]), t("packing", "Packing")]),
    ("HULL", "Marine Hull", "MARINE", "Insured vessel value", "0.60", 150, 0, 500, 7.5, True, True, [t("vessel", "Vessel name"), t("imo", "IMO / registration"), t("built", "Year built", "number")]),
    ("CAR", "Contractors All Risks", "ENGINEERING", "Contract works value", "0.25", 100, 0, 500, 10, True, False,
     [t("contractor", "Contractor"), t("project", "Project"), t("months", "Construction period (months)", "number"), t("maintenance", "Maintenance period (months)", "number")]),
    ("EAR", "Erection All Risks", "ENGINEERING", "Erection contract value", "0.30", 100, 0, 500, 10, True, False, [t("project", "Project"), t("months", "Erection period (months)", "number")]),
    ("MB", "Machinery Breakdown", "ENGINEERING", "Machinery value", "0.35", 50, 5, 250, 12.5, False, True, [t("machine", "Machine / plant"), t("age", "Age (years)", "number")]),
    ("PL", "Public Liability", "LIABILITY", "Limit of indemnity", "0.10", 50, 0, 250, 12.5, False, False, [t("activity", "Business activity"), t("territory", "Territorial limit")]),
    ("PROD", "Product Liability", "LIABILITY", "Limit of indemnity", "0.14", 75, 0, 500, 12.5, True, False, [t("product", "Product"), t("turnover", "Annual turnover", "number")]),
    ("PI", "Professional Indemnity", "LIABILITY", "Limit of indemnity", "0.80", 150, 0, 1000, 10, True, False, [t("profession", "Profession"), t("fees", "Annual fees", "number"), t("retro", "Retroactive date")]),
    ("WC", "Workmen's Compensation", "MISC", "Annual wage roll", "0.40", 25, 0, 0, 10, False, False, [t("employees", "Number of employees", "number"), t("occupation", "Main occupation")]),
    ("PA", "Personal Accident", "MISC", "Capital benefit", "0.20", 15, 0, 0, 15, False, False, [t("persons", "Persons covered", "number"), t("occupation", "Occupation")]),
    ("MONEY", "Money", "MISC", "Annual carrying / limit", "0.15", 25, 0, 100, 12.5, False, False, [t("safe", "Safe / strongroom", "bool")]),
    ("FIDELITY", "Fidelity Guarantee", "MISC", "Limit per employee", "0.30", 50, 0, 250, 12.5, True, False, [t("employees", "Number of employees", "number")]),
]


EXTRA = {
    "FIRE": [t("geocode", "Location / geocode"), t("protection", "Fire protection"), t("valuation", "Valuation basis", "choice", ["Replacement", "Market", "Book"]),
             t("survey", "Survey date"), t("cat_zone", "Catastrophe zone")],
    "BI": [t("declared_basis", "Declared basis (gross profit / revenue)")],
    "CARGO": [t("goods", "Goods"), t("incoterms", "Incoterms"), t("transshipment", "Transshipment", "bool"), t("storage", "Storage / port"),
              t("cover_type", "Cover type", "choice", ["Single transit", "Open cover declaration"]), t("per_conveyance", "Limit per conveyance", "number")],
    "HULL": [t("vessel_type", "Vessel type"), t("tonnage", "Tonnage", "number"), t("classification", "Classification society"), t("flag", "Flag"),
             t("nav_area", "Navigation area"), t("machinery", "Machinery terms"), t("survey_history", "Survey history")],
    "CAR": [t("site", "Site"), t("tpl_ext", "Third party liability extension", "bool")],
    "EAR": [t("site", "Site"), t("testing", "Testing / commissioning period (months)", "number"), t("dsu_ext", "Delay in start-up extension", "bool")],
    "MB": [t("breakdown_ext", "Machinery breakdown extension", "bool")],
    "PL": [t("basis", "Basis", "choice", ["Occurrence", "Claims-made"]), t("aggregate", "Annual aggregate", "number"), t("retro_date", "Retroactive date")],
    "PROD": [t("basis", "Basis", "choice", ["Occurrence", "Claims-made"]), t("aggregate", "Annual aggregate", "number"), t("retro_date", "Retroactive date")],
    "PI": [t("basis", "Basis", "choice", ["Claims-made", "Occurrence"]), t("aggregate", "Annual aggregate", "number")],
    "WC": [t("basis", "Basis", "choice", ["Occurrence", "Claims-made"]), t("payroll", "Payroll", "number")],
    "MONEY": [t("transit_limit", "Cash in transit limit", "number")],
    "FIDELITY": [t("discovery", "Discovery period (months)", "number")],
}
NEW_CLASSES = [
    ("CPM", "Contractors Plant & Machinery", "ENGINEERING", "Plant value", "0.45", 50, 5, 250, 10, False, True,
     [t("plant", "Plant schedule"), t("serials", "Serial numbers"), t("territory", "Operating territory"), t("maintenance", "Maintenance period (months)", "number")]),
    ("MEDICAL", "Medical (individual / family)", "MEDICAL", "Annual benefit limit", "3.00", 50, 10, 0, 7.5, True, False,
     [t("member", "Member / policy holder"), t("eligibility_from", "Eligibility from"), t("plan", "Benefit plan"), t("network", "Network"),
      t("copay", "Co-pay %", "number"), t("waiting", "Waiting period (days)", "number"), t("preauth", "Pre-authorisation required", "bool")]),
    ("GMEDICAL", "Group Medical", "MEDICAL", "Annual benefit limit per member", "2.50", 250, 0, 0, 5, True, False,
     [t("employer", "Employer"), t("members", "Number of members", "number"), t("dependants", "Dependants covered", "number"), t("categories", "Benefit categories"),
      t("census", "Member census reference"), t("tpa", "Third party administrator"), t("experience", "Experience period")]),
]


def seed_classes():
    n = 0
    for code, name, grp, label, rate, minp, dp, dm, comm, ref, avg, fields in CLASSES + NEW_CLASSES:
        fields = list(fields) + [f for f in EXTRA.get(code, []) if f["key"] not in {x["key"] for x in fields}]
        obj, created = PncClass.objects.get_or_create(code=code, defaults=dict(name=name, group=grp, si_label=label, rate_pct=D(rate), min_premium=D(minp), deductible_pct=D(dp),
                                                      deductible_min=D(dm), commission_pct=D(str(comm)), referral=ref, average_applies=avg, fields=fields))
        if not created:                      # existing set-up: add any new questions, keep everything the company has edited
            have = {x.get("key") for x in obj.fields}
            add = [f for f in fields if f["key"] not in have]
            if add:
                obj.fields = list(obj.fields) + add
                obj.save(update_fields=["fields"])
        n += created
    return n


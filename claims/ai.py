"""Damage map + AI repair-cost estimate.

Pipeline
  1. The handler marks the damaged zones (and severity 1-3) on the vehicle drawing.
  2. ``baseline()`` prices the damage from **your own history**: approved repair costs of earlier claims with a similar vehicle
     and similar damaged zones (weighted by similarity), falling back to a transparent rule-of-thumb table when there is not
     enough history.
  3. ``ask_ollama()`` sends the damage description, the baseline and the closest historical cases to a **local Ollama** model
     (no data leaves the machine) and asks for a JSON estimate. The LLM answer is sanity-bounded against the baseline.
  4. If Ollama is unreachable, the statistical baseline alone is returned and the UI says so.

All internal amounts are in the base currency (OMR) and converted to the policy currency at the end.
This is decision support for a surveyor / handler: it is not an approved settlement figure."""
import json
import urllib.error
import urllib.request
from decimal import Decimal

from django.conf import settings
from django.db.models import Q

from core.models import Company, Currency

D = Decimal

# zone id -> (label, indicative OMR cost for a "moderate" repair of a saloon: used only for the rule-of-thumb fallback / weighting)
ZONES = {
    "front_bumper": ("Front bumper", 90), "hood": ("Bonnet / hood", 160), "windscreen": ("Windscreen", 130), "roof": ("Roof", 220),
    "rear_glass": ("Rear glass", 110), "trunk": ("Boot / tailgate", 150), "rear_bumper": ("Rear bumper", 90),
    "fl_wing": ("Front-left wing", 120), "fl_door": ("Front-left door", 170), "rl_door": ("Rear-left door", 160), "rl_quarter": ("Rear-left quarter", 190),
    "fr_wing": ("Front-right wing", 120), "fr_door": ("Front-right door", 170), "rr_door": ("Rear-right door", 160), "rr_quarter": ("Rear-right quarter", 190),
}
SEVERITY = {1: ("Minor", "scratches, dents, paint", D("0.5"), 1.0), 2: ("Moderate", "panel repair or replacement", D("1.0"), 2.2),
            3: ("Severe", "replacement and structural work", D("1.9"), 4.0)}
CLASS_FACTOR = {"SALOON": 1.0, "SUV": 1.3, "PICKUP": 1.2, "VAN": 1.25, "BUS": 1.8, "TRUCK": 1.7, "MOTORCYCLE": 0.5, "TRAILER": 0.6}
LUXURY = {"MERCEDES", "BMW", "AUDI", "LEXUS", "PORSCHE", "LAND ROVER", "RANGE ROVER", "INFINITI", "JAGUAR", "BENTLEY", "VOLVO", "TESLA"}


def clean_zones(raw):
    out = {}
    for k, v in (raw or {}).items():
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if k in ZONES and v in SEVERITY:
            out[k] = v
    return out


def describe_zones(zones):
    return ", ".join(f"{ZONES[z][0]} ({SEVERITY[s][0].lower()})" for z, s in zones.items()) or "none marked"


def _points(zones):
    return sum((ZONES[z][1] / 100.0) * SEVERITY[s][3] for z, s in zones.items())


def _vehicle_factor(vehicle_type, make, year):
    f = CLASS_FACTOR.get((vehicle_type or "").upper(), 1.0)
    if (make or "").upper() in LUXURY:
        f *= 1.5
    return f


def rule_of_thumb(vehicle_type, make, zones):
    total = sum((ZONES[z][1] * float(SEVERITY[s][2]) for z, s in zones.items()), 0.0)
    return total * _vehicle_factor(vehicle_type, make, None)


def _wquantile(pairs, q):
    """Weighted quantile of [(value, weight)]."""
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * tot:
            return v
    return pairs[-1][0]


def baseline(vehicle_type, make, zones, exclude_claim_id=None):
    """Estimate (OMR) from company history + rules. Returns a dict with expected/low/high, sample size and the closest cases."""
    from .models import Claim

    zones = clean_zones(zones)
    pts = _points(zones)
    rule = rule_of_thumb(vehicle_type, make, zones)
    qs = Claim.objects.filter(approved_repair_cost__gt=0, is_total_loss=False).exclude(damage_zones={}).select_related("policy__currency", "risk")
    if exclude_claim_id:
        qs = qs.exclude(pk=exclude_claim_id)
    cases, samples = [], []
    mine = set(zones)
    for h in qs.order_by("-id")[:2500]:
        hz = clean_zones(h.damage_zones)
        hp = _points(hz)
        if not hz or hp <= 0:
            continue
        cost = float(h.approved_repair_cost * h.policy.fx_rate)
        overlap = len(mine & set(hz)) / max(len(mine | set(hz)), 1)
        sim = 1 + (2 if h.risk.vehicle_type == vehicle_type else 0) + (1 if (h.risk.make or "").upper() == (make or "").upper() else 0) + 2 * overlap
        samples.append((cost / hp, sim))
        cases.append((sim, h, cost))
    n = len(samples)
    if not zones:
        return {"expected": 0.0, "low": 0.0, "high": 0.0, "n": n, "method": "none", "rule": 0.0, "similar": []}
    if n >= 3:
        cpp = _wquantile(samples, 0.5)
        stat = {"expected": cpp * pts, "low": _wquantile(samples, 0.25) * pts, "high": _wquantile(samples, 0.75) * pts}
        w = min(n / 12.0, 1.0)                                # blend towards the rule-of-thumb when history is thin
        out = {k: stat[k] * w + rule * (1 - w) * (0.7 if k == "low" else 1.4 if k == "high" else 1.0) for k in stat}
        method = f"company history ({n} approved claims)" + ("" if w >= 1 else " blended with rule-of-thumb")
    else:
        out = {"expected": rule, "low": rule * 0.7, "high": rule * 1.4}
        method = "rule-of-thumb (not enough claim history yet)"
    cases.sort(key=lambda t: -t[0])
    similar = [{"claim": h.claim_no, "vehicle": f"{h.risk.make} {h.risk.model} {h.risk.year}", "zones": describe_zones(clean_zones(h.damage_zones)),
                "cost_omr": round(cost, 3), "loss_type": h.get_loss_type_display()} for _, h, cost in cases[:5]]
    return {**{k: round(v, 3) for k, v in out.items()}, "n": n, "method": method, "rule": round(rule, 3), "similar": similar}


# --------------------------------------------------------------------------------------------------------------- Ollama
def host():
    return (Company.get().ollama_host or settings.OLLAMA_HOST).rstrip("/")


def _request(path, payload=None, timeout=8):
    url = host() + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def list_models():
    """Every model installed in the local Ollama. Never raises."""
    try:
        data = _request("/api/tags", timeout=5)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "host": host(), "models": [], "error": f"Cannot reach Ollama at {host()} ({getattr(e, 'reason', e)}). Is it running?"}
    models = []
    for m in data.get("models", []):
        det = m.get("details") or {}
        models.append({"name": m.get("name") or m.get("model"), "size_gb": round((m.get("size") or 0) / 1e9, 2), "family": det.get("family", ""),
                       "parameters": det.get("parameter_size", ""), "quantization": det.get("quantization_level", ""),
                       "modified": (m.get("modified_at") or "")[:10]})
    models.sort(key=lambda m: m["name"])
    return {"ok": True, "host": host(), "models": models, "error": ""}


def _prompt(ctx, base):
    return (
        "You are an experienced motor claims surveyor in the Sultanate of Oman. Estimate the cost to repair the damage described, "
        "in Omani Rials (OMR). Use the company's own historical repair costs below as your main anchor.\n\n"
        f"VEHICLE: {ctx['vehicle']}\nVEHICLE TYPE: {ctx['vehicle_type']}   AGE: {ctx['age']} years   SUM INSURED: {ctx['sum_insured']} OMR\n"
        f"LOSS TYPE: {ctx['loss_type']}\nDAMAGED ZONES (severity): {ctx['zones']}\nHANDLER NOTES: {ctx['notes'] or 'none'}\n\n"
        f"STATISTICAL BASELINE from {base['n']} similar approved claims ({base['method']}): expected {base['expected']} OMR, "
        f"range {base['low']} to {base['high']} OMR.\nCLOSEST HISTORICAL CASES: {json.dumps(base['similar'])}\n\n"
        "Reply with ONLY a JSON object with these keys: "
        '{"expected": number, "low": number, "high": number, "labour_hours": number, "likely_parts": [strings], '
        '"total_loss_risk": "low|medium|high", "confidence": number between 0 and 1, "reasoning": "two or three sentences"}. '
        "All money values are OMR numbers without currency text. Do not add any other text."
    )


def ask_ollama(model, ctx, base, timeout=None):
    payload = {"model": model, "stream": False, "format": "json", "options": {"temperature": 0.2, "num_predict": 500},
               "messages": [{"role": "system", "content": "You are a precise motor claims estimator. Output valid JSON only."},
                            {"role": "user", "content": _prompt(ctx, base)}]}
    data = _request("/api/chat", payload, timeout=timeout or settings.OLLAMA_TIMEOUT)
    text = ((data.get("message") or {}).get("content") or "").strip()
    return json.loads(text)


def estimate(*, risk, zones, loss_type="COLLISION", notes="", model="", policy=None, claim_id=None, llm=True):
    """Return the estimate dict (amounts in the policy currency). ``policy`` supplies the currency."""
    zones = clean_zones(zones)
    policy = policy or risk.policy
    cur = policy.currency
    fx = policy.fx_rate
    out = {"zones": zones, "zone_text": describe_zones(zones), "currency": cur.code, "model": "", "source": "statistical", "warnings": []}
    if not zones:
        out["warnings"].append("Mark at least one damaged area on the vehicle drawing.")
        out.update(expected=0, low=0, high=0, n=0)
        return out
    base = baseline(risk.vehicle_type, risk.make, zones, claim_id)
    out["baseline"] = {k: base[k] for k in ("expected", "low", "high", "n", "method", "rule")}
    out["similar"] = base["similar"]
    expected, low, high = base["expected"], base["low"], base["high"]
    model = (model or Company.get().ollama_model) if llm else ""
    if model:
        ctx = {"vehicle": f"{risk.make} {risk.model} {risk.year}", "vehicle_type": risk.get_vehicle_type_display(),
               "age": max(0, __import__("datetime").date.today().year - risk.year), "sum_insured": round(float(risk.sum_insured * fx), 0),
               "loss_type": loss_type, "zones": describe_zones(zones), "notes": notes}
        try:
            ai = ask_ollama(model, ctx, base)
            e, lo, hi = float(ai.get("expected", 0)), float(ai.get("low", 0)), float(ai.get("high", 0))
            if e <= 0:
                raise ValueError("The model returned no estimate")
            lo, hi = min(lo or e * 0.8, e), max(hi or e * 1.25, e)
            if base["n"] >= 5:                              # keep the LLM within a believable band around company history
                floor, cap = base["low"] * 0.6, base["high"] * 1.8
                if e < floor or e > cap:
                    out["warnings"].append(f"The model's figure ({e:,.0f} OMR) was outside the band supported by company data and was pulled back.")
                    e = min(max(e, floor), cap)
                    lo, hi = min(lo, e), max(hi, e)
            expected, low, high = e, lo, hi
            out.update(source="ollama", model=model, reasoning=str(ai.get("reasoning", ""))[:600], likely_parts=[str(x) for x in (ai.get("likely_parts") or [])][:12],
                       labour_hours=ai.get("labour_hours"), total_loss_risk=ai.get("total_loss_risk", ""), confidence=ai.get("confidence"))
        except (urllib.error.URLError, OSError) as e:
            out["warnings"].append(f"Ollama is not reachable ({getattr(e, 'reason', e)}): showing the statistical estimate only.")
        except (ValueError, KeyError, TypeError) as e:
            out["warnings"].append(f"The model's answer could not be used ({e}): showing the statistical estimate only.")
    elif llm:
        out["warnings"].append("No AI model selected: showing the statistical estimate from company data. Pick a model from the list to add the LLM review.")
    conv = lambda v: float(cur.quantize(D(str(v)) / fx))
    out.update(expected=conv(expected), low=conv(low), high=conv(high), n=base["n"])
    if risk.sum_insured and out["expected"] > float(risk.sum_insured) * 0.7:
        out["warnings"].append("The estimate is above 70% of the sum insured: consider a total-loss assessment.")
    return out

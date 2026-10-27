"""Every change a claims handler can make to a claim, as one function, so the same code runs directly or after a second person confirms.

``prepare`` validates the posted data (so the maker gets errors at once); ``apply`` performs the change and returns messages.
"""
from datetime import date

from django.http import QueryDict

from core.models import Currency, convert

from . import forms as f
from . import services
from .models import Claim, LetterOfGuarantee, MedicalBill, Subrogation, SubroNotice

ACTIONS = ["reserve", "estimate_auto", "estimate", "approve", "pay", "repair", "recovery", "subrogation", "subro_recover", "surveyor", "guarantee", "bill", "close",
           "reject", "reopen", "settle", "invoice", "estimate_settle", "excess", "legal_flag", "legal_action", "injury", "notice", "cat_flag"]

LABELS = {"reserve": "Change the reserve", "estimate_auto": "Use the automatic estimate", "estimate": "Record a garage estimate", "approve": "Approve the claim",
          "pay": "Make a payment", "repair": "Record repair completion", "recovery": "Record a recovery", "subrogation": "Update subrogation",
          "subro_recover": "Record a subrogation recovery", "surveyor": "Change the surveyor", "guarantee": "Issue a letter of guarantee", "bill": "Record a hospital bill",
          "close": "Close the claim", "reject": "Reject the claim", "reopen": "Reopen the claim", "settle": "Mark the claim settled", "invoice": "Record a garage invoice",
          "estimate_settle": "Settle a garage estimate", "excess": "Record the excess", "legal_flag": "Change the legal flag", "legal_action": "Add a legal action",
          "injury": "Record an injury / death", "notice": "Send a notice to the at-fault insurer", "cat_flag": "Mark as a catastrophe claim"}


def to_post(payload):
    q = QueryDict(mutable=True)
    for k, v in payload.items():
        q.setlist(k, v if isinstance(v, list) else [v])
    return q


def _form(claim, action, P):
    if action == "reserve":
        return f.ReserveForm(P)
    if action == "estimate":
        return f.EstimateForm(P)
    if action == "approve":
        return f.ApproveForm(P)
    if action == "pay":
        return f.PayForm(P)
    if action == "repair":
        return f.RepairForm(P)
    if action == "recovery":
        return f.RecoveryForm(P)
    if action == "subrogation":
        return f.SubrogationForm(P)
    if action == "subro_recover":
        return f.SubroRecoverForm(P)
    if action == "surveyor":
        return f.SurveyorForm(P)
    if action == "guarantee":
        return f.GuaranteeForm(P)
    if action == "bill":
        return f.MedicalBillForm(claim, P)
    if action == "reopen":
        return f.ReopenForm(P)
    if action == "close":
        return f.CloseForm(P)
    if action == "invoice":
        return f.InvoiceForm(claim, P)
    if action == "estimate_settle":
        return f.SettleEstimateForm(claim, P)
    if action == "excess":
        return f.ExcessForm(P)
    if action == "legal_flag":
        return f.LegalFlagForm(P)
    if action == "legal_action":
        return f.LegalActionForm(P)
    if action == "injury":
        return f.InjuryForm(P)
    if action == "cat_flag":
        return f.CatFlagForm(P)
    return None


def prepare(claim, action, P):
    """Return (error or None). Checks the claim state and the posted data without changing anything."""
    if action not in ACTIONS:
        return "Unknown action."
    try:
        services.assert_editable(claim, action, P)
    except services.ClaimError as e:
        return str(e)
    form = _form(claim, action, P)
    if form is not None and not form.is_valid():
        return "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items())
    if action == "reopen" and not claim.locked:
        return "Only a settled, closed or rejected claim needs to be reopened."
    if action == "close":
        b = services.close_blockers(claim)
        if b:
            return " ".join(b)
        pre = services.closing_preview(claim)
        if pre["applies"] and pre["delta"] > 0:
            return f"The invoices ({pre['invoiced']}) are above the approved cost ({pre['estimate']}). Reopen and re-approve the higher amount first."
        if pre["applies"] and pre["delta"] < 0 and not form.cleaned_data.get("confirm_reversal"):
            return f"Tick the box to confirm reversing the estimate by {abs(pre['delta'])} {claim.policy.currency.code} before closing."
    return None


def apply(claim, action, P, user):
    """Perform the change. Returns [(level, text)]; raises services.ClaimError when it cannot be done."""
    cur = claim.policy.currency
    services.assert_editable(claim, action, P)
    form = _form(claim, action, P)
    if form is not None and not form.is_valid():
        raise services.ClaimError("; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))
    d = form.cleaned_data if form is not None else {}
    from accounting import services as acc
    out = []
    if action == "reserve":
        claim.reserve = cur.quantize(d["reserve"])
        claim.estimate_overridden = True
        claim.status = Claim.ASSESSED if claim.status == Claim.OPEN else claim.status
        claim.save()
        acc.sync_claim(claim, user)
        out.append(("success", "Reserve updated (the automatic estimate no longer changes it)."))
    elif action == "estimate_auto":
        services.refresh_estimate(claim, user, reset=True)
        out.append(("success", f"Reserve set to the automatic estimate: {claim.auto_estimate} {cur.code}."))
    elif action == "estimate":
        est = services.check_estimate(claim, d["garage"], d["pricing_basis"], d["labour_amount"], d["parts_cost"], d["fixed_amount"], d["estimated_days"], d["supplementary"])
        out.append(("warning", f"{d['garage'].name} is non-preferred and its quote is well above the reserve.") if est.exceeds_market else ("success", "Estimate recorded."))
    elif action == "approve":
        services.approve_claim(claim, d["approved_cost"] or None, user, d["total_loss"])
        out.append(("success", f"Claim approved. Payable after excess: {claim.net_payable} {cur.code}."))
    elif action == "pay":
        amt = services.pay_claim(claim, d["stage"], d["payee"], d["reference"])
        n = claim.treaty_recoveries.filter(payment__reference=d["reference"]).count() if d["reference"] else 0
        out.append(("success", f"Payment of {amt} {cur.code} recorded; reinsurers' share posted to the treaty recovery journal."))
    elif action == "repair":
        services.complete_repair(claim, d["completed_on"], d["rework"], d["customer_score"], d["final_bill"])
        out.append(("warning", f"Repair overran the FSA 30-day limit by {claim.repair_delay_days} day(s). Delay compensation: {claim.delay_compensation} {cur.code}.")
                   if claim.repair_delay_days else ("success", "Repair completed within the 30-day FSA limit."))
        if getattr(claim, "subrogation", None) and claim.notices.filter(kind="DEMAND").exists():
            out.append(("info", "Confirmation and demand notice created for the at-fault party's insurer."))
    elif action == "recovery":
        services.add_recovery(claim, d["recovery_type"], d["amount"], d["note"])
        out.append(("success", "Recovery recorded."))
    elif action == "subrogation":
        if claim.status not in (Claim.APPROVED, Claim.SETTLED, Claim.CLOSED) and not hasattr(claim, "subrogation"):
            raise services.ClaimError("Subrogation starts after the insured has been paid, or when the claim is opened against an at-fault party.")
        sub, _ = Subrogation.objects.get_or_create(claim=claim, defaults={"liable_party": d["liable_party"], "reserve_amount": claim.net_payable or claim.reserve})
        sub.liable_party, sub.liable_insurer, sub.recovery_status = d["liable_party"], d["liable_insurer"], d["recovery_status"]
        sub.legal_counsel, sub.note = d["legal_counsel"], d["note"]
        sub.reserve_amount = sub.reserve_amount or claim.net_payable
        sub.save()
        claim.third_party_liable = True
        claim.save(update_fields=["third_party_liable"])
        out.append(("success", "Subrogation updated."))
    elif action == "subro_recover":
        sub = services.record_subrogation_recovery(claim, d["amount"])
        out.append(("success", "Recovery recorded." + (f" Excess of {sub.excess_refunded} {cur.code} refunded to the insured." if sub.excess_refunded else "")))
    elif action == "surveyor":
        if claim.surveyor_id != d["surveyor"].id:
            claim.surveyor, claim.surveyor_assigned_on, claim.surveyor_report_on = d["surveyor"], date.today(), None
        if d["report_received"]:
            claim.surveyor_report_on = date.today()
        claim.save()
        out.append(("success", "Surveyor updated."))
    elif action == "guarantee":
        LetterOfGuarantee.objects.create(claim=claim, **d)
        out.append(("success", "Letter of Guarantee issued."))
    elif action == "bill":
        hosp = d["guarantee"].hospital
        cap = convert(hosp.room_cap_per_day, Currency.base(), cur)
        flagged = bool(cap and d["rate_charged"] > cap)
        MedicalBill.objects.create(claim=d["guarantee"], description=d["description"], days=d["days"], rate_charged=d["rate_charged"], flagged=flagged,
                                   flag_reason=f"Exceeds agreed tariff of {cap} {cur.code}/day" if flagged else "")
        out.append(("warning", "Bill flagged: exceeds the agreed hospital tariff.") if flagged else ("success", "Bill recorded."))
    elif action == "close":
        pre = services.close_claim(claim, user, confirmed=d.get("confirm_reversal", False))
        if pre["applies"] and pre["delta"]:
            out.append(("info", f"Estimate reversed by {abs(pre['delta'])} {cur.code}: {pre['estimate']} adjusted to the invoiced {pre['invoiced']} (logged on the claim)."))
            if pre["overpaid"]:
                out.append(("warning", f"{pre['overpaid']} {cur.code} was paid above the final cost and is now due back from the payee."))
        out.append(("success", "Claim financially closed."))
    elif action == "reject":
        claim.status = Claim.REJECTED
        claim.save(update_fields=["status"])
        acc.sync_claim(claim, user)
        out.append(("info", "Claim rejected."))
    elif action == "reopen":
        services.reopen_claim(claim, user, d["reason"])
        out.append(("success", f"Claim reopened ({claim.get_status_display()}). Fields are editable again."))
    elif action == "settle":
        services.settle_claim(claim)
        out.append(("success", "Claim marked settled; it is now read only."))
    elif action == "invoice":
        services.record_invoice(d["estimate"], d["invoice_no"], d["invoice_amount"], d["invoice_date"])
        out.append(("success", "Invoice recorded."))
    elif action == "estimate_settle":
        services.settle_estimate(d["estimate"])
        out.append(("success", "Estimate marked settled."))
    elif action == "excess":
        services.record_excess(claim, d["amount"], d["collected_on"] or date.today(), d["waiver_reason"])
        out.append(("success", "Excess recorded."))
    elif action == "legal_flag":
        services.set_legal(claim, d["legal"], user)
        out.append(("success", "Claim marked as legal." if d["legal"] else "Legal flag removed."))
    elif action == "legal_action":
        services.add_legal_action(claim, user, **d)
        out.append(("success", "Legal action recorded."))
    elif action == "injury":
        services.add_injury(claim, user, **d)
        out.append(("warning" if d["severity"] == "DEATH" else "success", "Injury / death recorded; the estimate was updated."))
    elif action == "cat_flag":
        ev = services.set_cat_event(claim, user, d.get("cat_event"), d.get("new_name", ""), d.get("new_type", "OTHER"), d.get("new_date"))
        out.append(("warning", f"Claim marked as part of {ev}: it counts towards the cat XoL recovery for the event.") if ev else ("success", "Catastrophe flag removed."))
    elif action == "notice":
        kind = P.get("kind")
        if kind not in dict(SubroNotice.KINDS):
            raise services.ClaimError("Choose the notice to send.")
        n = services.send_notice(claim, kind, user)
        out.append(("success", f"Notice created: {n.delivery}."))
    return out

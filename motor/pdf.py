"""Policy schedule / policy report (PDF). Amounts use ISO codes because the built-in PDF fonts lack the new currency glyphs."""
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table
from xml.sax.saxutils import escape as esc_

from core import flex as flexlib
from core import pdfkit as k
from core.models import Company, Currency, FlexField, convert
from core.templatetags.money import money_text


def policy_schedule_pdf(policy):
    """Formal policy schedule: parties, period, vehicles, limits, premium, excess, conditions, signature block."""
    company = Company.get()
    cur = policy.currency
    m = lambda x: money_text(x, cur)
    pr, base = policy.product, Currency.base()
    buf, doc = k.new_doc(f"Schedule {policy.policy_no}")
    story = k.letterhead(company, "POLICY SCHEDULE", policy.policy_no, f"Date of issue {policy.issued_at:%d %b %Y}",
                         extra=[f"Version {policy.version} · {policy.get_status_display()}"])
    story.append(Paragraph(f"<b>{esc_(pr.name)}</b> - Unified Motor Insurance Policy, Sultanate of Oman", k.BODY))
    story.append(Paragraph("This Schedule is part of the policy and must be read together with the Policy Wording, any Endorsements "
                           "and the Proposal Form. Please check all details and notify us of any error immediately.", k.SMALL))

    c = policy.customer
    story.append(Paragraph("1. The Insured and the Insurer", k.H2))
    story.append(k.kv([
        ("Insured", c.name), ("ID / CR no.", c.identifier or "-"),
        ("Address", getattr(c, "address", "") or "-"), ("Contact", " · ".join(x for x in (getattr(c, "phone", ""), getattr(c, "email", "")) if x) or "-"),
        ("Insurer", company.display_legal_name), ("Regulator", f"FSA licence {company.fsa_license_no}" if company.fsa_license_no else "Financial Services Authority, Oman"),
    ]))
    story.append(Paragraph("2. Period and basis of cover", k.H2))
    days = (policy.end_date - policy.start_date).days + 1
    story.append(k.kv([
        ("Period of insurance", f"{policy.start_date:%d %b %Y} to {policy.end_date:%d %b %Y} (both days inclusive, {days} days)"),
        ("Currency", f"{cur.code} ({cur.name})"),
        ("Product", pr.name), ("Payment status", {"PAID": "Paid in full", "PARTIAL": "Part paid", "UNPAID": "Unpaid"}[policy.payment_status]),
    ]))
    pf = flexlib.display(FlexField.POLICY, policy.flex) + flexlib.display(FlexField.CUSTOMER, c.flex)
    if pf:
        story.append(Spacer(1, 4))
        story.append(k.kv(pf[:8] if len(pf) % 2 == 0 else pf[:8] + [("", "")]))

    story.append(Paragraph("3. Insured vehicles", k.H2))
    rows = [["No.", "Vehicle", "Registration / Chassis", "Type / Usage", "Sum insured"]]
    risks = list(policy.risks.prefetch_related("riders", "drivers"))
    for n, r in enumerate(risks, 1):
        rows.append([str(n), f"{r.make} {r.model} {r.year}\n{r.color}, {r.cc}cc", f"{r.plate or '-'}\n{r.chassis_no}",
                     f"{r.get_vehicle_type_display()}\n{r.get_usage_display()}",
                     m(r.sum_insured) if pr.includes_od else "TPL only"])
    story.append(k.grid(rows, [10 * mm, 44 * mm, 52 * mm, 36 * mm, 32 * mm], align_right_cols=(4,)))

    drivers = [(r, d) for r in risks for d in r.drivers.all()]
    if drivers:
        story.append(Paragraph("4. Authorised drivers", k.H2))
        rows = [["Driver", "Licence no.", "Date of birth", "Relationship", "Vehicle"]]
        for r, d in drivers:
            rows.append([d.name + (" (young driver)" if d.age_on(policy.start_date) < 25 else ""), d.licence_no, f"{d.date_of_birth:%d %b %Y}",
                         d.get_relationship_display(), r.plate or r.chassis_no[-6:]])
        story.append(k.grid(rows, [50 * mm, 32 * mm, 28 * mm, 30 * mm, 34 * mm]))
    else:
        story.append(Paragraph("4. Authorised drivers", k.H2))
        story.append(Paragraph("The insured and drivers holding a valid licence with the insured's permission, subject to the driver excess below.", k.BODY))

    story.append(Paragraph("5. Covers and limits of liability", k.H2))
    tp = convert(getattr(pr, "tp_limit", 0) or 0, base, cur)
    rows = [["Cover", "Limit / basis"],
            ["Third party liability (compulsory)", f"Bodily injury per the unified policy; property damage up to {m(tp)}" if tp else "Per the unified policy"]]
    if pr.includes_od:
        rows.append(["Own damage", "Repair cost or market value at the time of loss, up to the sum insured"])
    for nm in sorted({cv.name for r in risks for cv in r.riders.all()}):
        rows.append([nm, "As per the Policy Wording"])
    rows.append(["Natural disasters / weather events", "Material damage to the insured vehicle is covered automatically under all motor policies"])
    story.append(k.grid(rows, [62 * mm, 112 * mm]))

    story.append(Paragraph("6. Premium", k.H2))
    rows = [("Net premium", m(policy.net_premium))]
    if policy.voucher_discount:
        rows.append(("  of which voucher discount", m(policy.voucher_discount)))
    rows += [("Policy fees", m(policy.fees)), ("VAT", m(policy.tax)), ("Total payable", m(policy.total_payable)),
             ("Paid to date", m(policy.paid_amount)), ("Balance due", m(max(policy.balance_due, 0)))]
    story.append(k.totals(rows, width=90 * mm, bold_last=False))

    story.append(Paragraph("7. Excess (the insured's contribution to each claim)", k.H2))
    story.append(k.grid([["Standard excess", "Young / imposed driver (under 25)", "Undisclosed driver"],
                         [m(convert(pr.standard_excess, base, cur)), m(convert(pr.young_driver_excess, base, cur)),
                          m(convert(pr.undisclosed_driver_excess, base, cur))]], [58 * mm, 58 * mm, 58 * mm]))
    ends = list(policy.endorsements.all())
    story.append(Paragraph("8. Endorsements", k.H2))
    if ends:
        rows = [["Number", "Type", "Effective", "Premium", "Total"]]
        for e in reversed(ends):
            rows.append([e.number, e.get_endorsement_type_display(), f"{e.effective_date:%d %b %Y}", m(e.premium_adjustment), m(e.total)])
        story.append(k.grid(rows, [44 * mm, 52 * mm, 26 * mm, 28 * mm, 28 * mm], align_right_cols=(3, 4)))
    else:
        story.append(Paragraph("None issued.", k.BODY))

    story.append(Paragraph("9. Conditions and warranties", k.H2))
    for t in ("Buses, trucks, trailers and heavy equipment are insured for commercial or government use only.",
              "Notify the insurer of any accident within the period required by the Policy Wording; do not admit liability or settle without consent.",
              "Repairs: approved repairs are completed within 30 days of the claim file being finalised; repair payments are released 70% before "
              "repairs begin and 30% on completion, as required by the Financial Services Authority.",
              "Only the drivers named on this schedule are disclosed drivers; the undisclosed-driver excess applies otherwise."):
        story.append(Paragraph("• " + esc_(t), k.SMALL))
    story.append(Spacer(1, 14))
    sig = Table([[Paragraph("<b>For the Insurer</b><br/><br/><br/>______________________________<br/>Authorised signatory<br/>" +
                            esc_(company.display_legal_name), k.SMALL),
                  Paragraph(f"<b>Insured's acknowledgement</b><br/><br/><br/>______________________________<br/>{esc_(c.name)}<br/>Date:", k.SMALL)]],
                colWidths=[87 * mm, 87 * mm])
    story.append(KeepTogether([sig]))
    story += [Spacer(1, 8)] + k.footer_text(company)
    return k.render(buf, doc, story)

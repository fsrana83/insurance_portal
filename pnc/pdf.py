"""Policy schedule PDF for P&C policies."""
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer

from core import pdfkit as k
from core.models import Company


def _m(x, cur):
    return f"{cur.code} {cur.quantize(x):,.{cur.decimals}f}"


def schedule(policy):
    cur = policy.currency
    buf, doc = k.new_doc(f"Schedule {policy.number}")
    st = k.letterhead(Company.get(), "POLICY SCHEDULE", policy.number, f"{policy.pnc_class.name}", [f"Version {policy.version}"])
    st.append(k.kv([("Insured", policy.customer.name), ("Class", policy.pnc_class.name), ("Period", f"{policy.start_date:%d %b %Y} to {policy.end_date:%d %b %Y}"),
                    ("Currency", cur.code), ("Interest", policy.interest or "-"), ("Status", policy.get_status_display())]))
    st.append(Paragraph("Risks insured", k.H2))
    rows = [[policy.pnc_class.si_label.title() if False else "No.", "Risk", "Location", policy.pnc_class.si_label, "Premium"]]
    for r in policy.risks.filter(active=True):
        rows.append([str(r.number), r.description, r.location or "-", _m(r.sum_insured, cur), _m(r.premium, cur)])
    st.append(k.grid(rows, [12 * mm, 62 * mm, 40 * mm, 34 * mm, 30 * mm], align_right_cols=(3, 4)))
    st.append(Spacer(1, 8))
    st.append(k.totals([("Net premium", _m(policy.net_premium, cur)), ("Policy fee", _m(policy.fees, cur)), ("VAT", _m(policy.tax, cur)), ("Total payable", _m(policy.total_payable, cur))]))
    if policy.conditions:
        st += [Paragraph("Special conditions and warranties", k.H2), Paragraph(k.esc(policy.conditions).replace("\n", "<br/>"), k.BODY)]
    ends = list(policy.endorsements.all())
    if ends:
        st.append(Paragraph("Endorsements", k.H2))
        st.append(k.grid([["No.", "Effective", "Change", "Premium"]] + [[e.number, f"{e.effective_date:%d %b %Y}", e.description, _m(e.total, cur)] for e in ends], [34 * mm, 26 * mm, 88 * mm, 30 * mm], align_right_cols=(3,)))
    if policy.installments.exists():
        st.append(Paragraph("Payment schedule", k.H2))
        st.append(k.grid([["#", "Due", "Amount"]] + [["Advance" if i.number == 0 else str(i.number), f"{i.due_date:%d %b %Y}", _m(i.amount, cur)] for i in policy.installments.all()], [30 * mm, 40 * mm, 40 * mm], align_right_cols=(2,)))
    st += k.footer_text()
    return k.render(buf, doc, st)

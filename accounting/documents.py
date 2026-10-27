"""PDFs: debit note, credit note, VAT tax invoice and premium receipt voucher."""
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer

from core import pdfkit as k
from core.models import Company, Currency
from core.templatetags.money import money_text

from .models import FinancialDocument


def _parties(fd_or_policy_customer, company):
    c = fd_or_policy_customer
    return k.kv([("Insured", c.name), ("ID / CR", c.identifier or "-"), ("Address", c.address or "-"), ("Mobile", c.phone or "-")])


def financial_document_pdf(fd):
    company = Company.get()
    cur, pol = fd.currency, fd.policy
    m = lambda x: money_text(x, cur)
    titles = {FinancialDocument.DEBIT_NOTE: "DEBIT NOTE", FinancialDocument.CREDIT_NOTE: "CREDIT NOTE", FinancialDocument.INVOICE: "TAX INVOICE"}
    buf, doc = k.new_doc(f"{fd.number}")
    story = k.letterhead(company, titles[fd.doc_type], fd.number, f"Date {fd.issued_on:%d %b %Y}",
                         extra=[f"Policy {pol.policy_no}"])
    story.append(_parties(fd.customer, company))
    story.append(Spacer(1, 8))
    story.append(k.kv([("Policy no.", pol.policy_no), ("Period", f"{pol.start_date:%d %b %Y} to {pol.end_date:%d %b %Y}"),
                       ("Product", pol.product.name), ("Currency", f"{cur.code} ({cur.name})")]))
    story.append(Spacer(1, 10))
    rows = [["Description", "Taxable amount", "VAT %", "VAT", "Total"]]
    rate = (fd.vat / (fd.net_premium + fd.fees) * 100) if (fd.net_premium + fd.fees) else 0
    rows.append([fd.description or "Motor insurance premium", m(fd.net_premium), f"{rate:.1f}%" if rate else "-", "", ""])
    if fd.fees:
        rows.append(["Policy / endorsement fee", m(fd.fees), f"{rate:.1f}%" if rate else "-", "", ""])
    rows.append(["Total", m(fd.net_premium + fd.fees), "", m(fd.vat), m(fd.total)])
    story.append(k.grid(rows, [66 * mm, 34 * mm, 18 * mm, 28 * mm, 32 * mm], align_right_cols=(1, 2, 3, 4)))
    story.append(Spacer(1, 8))
    tot = [("Net premium", m(fd.net_premium)), ("Fees", m(fd.fees)), ("VAT", m(fd.vat))]
    if cur.code != Currency.base().code:
        tot.append(("VAT in " + Currency.base().code, money_text(fd.vat_base, Currency.base())))
        tot.append(("Exchange rate", f"1 {cur.code} = {fd.fx_rate.normalize():f} {Currency.base().code}"))
    tot.append(("Total", m(fd.total)))
    story.append(k.totals(tot))
    story.append(Spacer(1, 10))
    if fd.doc_type == FinancialDocument.DEBIT_NOTE:
        story.append(Paragraph("Please settle this debit note on or before inception of cover. Cover is conditional on payment of premium.", k.SMALL))
    elif fd.doc_type == FinancialDocument.CREDIT_NOTE:
        story.append(Paragraph("This credit note reduces the premium due under the policy named above. Refunds are made to the original payer.", k.SMALL))
    else:
        story.append(Paragraph("Insurance premium is subject to VAT at the standard rate under the Oman VAT law. "
                               "VAT on foreign-currency invoices is also stated in Omani rials.", k.SMALL))
    story += [Spacer(1, 6)] + k.footer_text(company)
    return k.render(buf, doc, story)


def receipt_pdf(payment):
    company = Company.get()
    cur, pol = payment.currency, payment.policy
    m = lambda x: money_text(x, cur)
    buf, doc = k.new_doc(f"Receipt {payment.receipt_no or payment.number}")
    story = k.letterhead(company, "RECEIPT VOUCHER", payment.receipt_no or payment.number,
                         f"Date {(payment.paid_at or payment.created_at):%d %b %Y}", extra=[f"Policy {pol.policy_no}"])
    story.append(k.kv([("Received from", payment.payer_name or payment.customer.name), ("ID / CR", payment.customer.identifier or "-"),
                       ("Amount", m(payment.amount)), ("Payment method", payment.get_method_display()),
                       ("Reference", payment.reference or payment.gateway_txn_id or "-"), ("Policy no.", pol.policy_no)]))
    story.append(Spacer(1, 10))
    paid_to_date = pol.paid_amount
    story.append(k.totals([("Policy total payable", m(pol.total_payable)), ("Paid to date (incl. this receipt)", m(paid_to_date)),
                           ("Balance outstanding", m(max(pol.total_payable - paid_to_date, 0)))]))
    story.append(Spacer(1, 8))
    story.append(Paragraph(f"Being premium for {pol.product.name} policy {pol.policy_no}"
                           + (f" ({payment.narration})" if payment.narration else "") + ".", k.BODY))
    story += [Spacer(1, 18), Paragraph("Authorised signature: ______________________", k.SMALL), Spacer(1, 8)] + k.footer_text(company)
    return k.render(buf, doc, story)

"""Small helpers shared by every PDF (schedule, debit/credit note, VAT invoice, receipt): letterhead, tables, styles.

The built-in PDF fonts have no glyph for the new rial / dirham / riyal signs yet, so amounts on paper use ISO codes."""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .models import Company

GREEN = colors.HexColor("#0a3d43")
TEAL = colors.HexColor("#0f5c63")
SAND = colors.HexColor("#f3efe6")
LINE = colors.HexColor("#d8d2c4")
MUTED = colors.HexColor("#555555")

_ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=_ss["Title"], textColor=GREEN, fontSize=19, alignment=0, spaceAfter=2, leading=22)
H2 = ParagraphStyle("h2", parent=_ss["Heading3"], textColor=TEAL, spaceBefore=10, spaceAfter=4)
SMALL = ParagraphStyle("small", parent=_ss["Normal"], fontSize=8, leading=10, textColor=MUTED)
BODY = ParagraphStyle("body", parent=_ss["Normal"], fontSize=9, leading=12)
BOLD = ParagraphStyle("bold", parent=BODY, fontName="Helvetica-Bold")
RIGHT = ParagraphStyle("right", parent=BODY, alignment=2)


def esc(x):
    return (str(x) if x is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def new_doc(title, margins=(16, 14)):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=margins[0] * mm, rightMargin=margins[0] * mm, topMargin=margins[1] * mm,
                            bottomMargin=margins[1] * mm + 4 * mm, title=title)
    return buf, doc


def letterhead(company=None, title="", number="", date_text="", extra=None):
    """Company block on the left, document title / number on the right."""
    c = company or Company.get()
    left = []
    path = c.logo_path()
    if path:
        try:
            img = Image(path)
            ratio = img.imageWidth / float(img.imageHeight)
            h = 14 * mm
            img.drawHeight, img.drawWidth = h, min(h * ratio, 46 * mm)
            left.append(img)
        except Exception:
            pass
    left.append(Paragraph(f"<b>{esc(c.display_legal_name)}</b>", ParagraphStyle("c", parent=BODY, fontSize=11, textColor=GREEN)))
    bits = [c.address_line, " · ".join(x for x in (f"Tel {c.phone}" if c.phone else "", c.email, c.website) if x)]
    ids = " · ".join(x for x in (f"CR {c.cr_number}" if c.cr_number else "", f"VAT TRN {c.vat_number}" if c.vat_number else "",
                                 f"FSA licence {c.fsa_license_no}" if c.fsa_license_no else "") if x)
    for b in bits + [ids]:
        if b:
            left.append(Paragraph(esc(b), SMALL))
    right = [Paragraph(esc(title), ParagraphStyle("t", parent=H1, alignment=2, fontSize=17)),
             Paragraph(f"<b>{esc(number)}</b>", ParagraphStyle("n", parent=BODY, alignment=2, fontSize=11)),
             Paragraph(esc(date_text), ParagraphStyle("d", parent=SMALL, alignment=2))]
    for e in extra or []:
        right.append(Paragraph(esc(e), ParagraphStyle("e", parent=SMALL, alignment=2)))
    t = Table([[left, right]], colWidths=[100 * mm, 78 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, 0), 1.2, TEAL),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    return [t, Spacer(1, 8)]


def grid(data, widths, head=True, align_right_cols=(), font=8.5, zebra=False):
    rows = [[Paragraph(esc(c), BODY) if isinstance(c, str) else c for c in r] for r in data]
    t = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
    st = [("FONTSIZE", (0, 0), (-1, -1), font), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("GRID", (0, 0), (-1, -1), 0.25, LINE),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4)]
    if head:
        st += [("BACKGROUND", (0, 0), (-1, 0), TEAL), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
        # header paragraphs need white text
        for i, c in enumerate(data[0]):
            rows[0][i] = Paragraph(f'<font color="white"><b>{esc(c)}</b></font>', BODY)
        t = Table(rows, colWidths=widths, repeatRows=1)
    for c in align_right_cols:
        st.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    if zebra:
        st.append(("ROWBACKGROUNDS", (0, 1 if head else 0), (-1, -1), [colors.white, SAND]))
    t.setStyle(TableStyle(st))
    return t


def kv(pairs, widths=(32 * mm, 57 * mm, 32 * mm, 57 * mm)):
    """Two key/value pairs per row."""
    rows = []
    for i in range(0, len(pairs), 2):
        a = pairs[i]
        b = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
        rows.append([Paragraph(f"<b>{esc(a[0])}</b>", BODY), Paragraph(esc(a[1]), BODY), Paragraph(f"<b>{esc(b[0])}</b>", BODY), Paragraph(esc(b[1]), BODY)])
    t = Table(rows, colWidths=list(widths))
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, LINE), ("BACKGROUND", (0, 0), (0, -1), SAND), ("BACKGROUND", (2, 0), (2, -1), SAND),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4)]))
    return t


def totals(rows, width=70 * mm, bold_last=True):
    data = [[Paragraph(esc(a), BODY), Paragraph(esc(b), RIGHT)] for a, b in rows]
    t = Table(data, colWidths=[width * 0.5, width * 0.5], hAlign="RIGHT")
    st = [("GRID", (0, 0), (-1, -1), 0.25, LINE), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4)]
    if bold_last:
        st += [("BACKGROUND", (0, -1), (-1, -1), SAND), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]
    t.setStyle(TableStyle(st))
    return t


def footer_text(company=None):
    c = company or Company.get()
    out = [c.invoice_footer] if c.invoice_footer else []
    if c.bank_name or c.iban:
        out.append(" · ".join(x for x in (f"Bank: {c.bank_name}" if c.bank_name else "", f"A/C {c.bank_account}" if c.bank_account else "",
                                         f"IBAN {c.iban}" if c.iban else "", f"SWIFT {c.swift}" if c.swift else "") if x))
    return [Paragraph(esc(x), SMALL) for x in out if x]


def render(buf, doc, story):
    def _page(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(16 * mm, 9 * mm, Company.get().display_legal_name)
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, f"Page {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buf.getvalue()

"""Sending policy documents: secure links (WhatsApp), .eml drafts with PDF attachments (Outlook desktop), server e-mail.

* **Outlook desktop** - a browser cannot attach files to a desktop mail client, so we generate an ``.eml`` file marked
  ``X-Unsent: 1``. Opening it in classic Outlook for Windows shows a ready-to-send draft with the PDFs attached and the
  recipient filled in. (New Outlook / Outlook on the web do not open .eml drafts: use the "Send from server" button there.)
* **WhatsApp desktop** - the ``whatsapp://send`` link prefills the chat and message, but WhatsApp offers no way to pre-attach files.
  The message therefore carries an expiring secure link to the PDFs; the user can also download the PDFs and drag them into the chat.
"""
import re
import secrets
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formatdate
from urllib.parse import quote

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from accounting.documents import financial_document_pdf, receipt_pdf
from accounting.models import FinancialDocument
from core.models import Company
from payments.models import Payment

from .models import DocumentShare
from .pdf import policy_schedule_pdf


def available_documents(policy):
    """[(key, label)] of every document that exists for the policy."""
    docs = [("schedule", f"Policy schedule {policy.policy_no}")]
    for fd in policy.fin_documents.all().order_by("id"):
        docs.append((f"fd:{fd.pk}", f"{fd.get_doc_type_display()} {fd.number}"))
    for pay in policy.payments.filter(status=Payment.PAID).order_by("id"):
        docs.append((f"rv:{pay.pk}", f"Receipt {pay.receipt_no}"))
    return docs


def render_document(policy, key):
    """Return (filename, pdf_bytes) for a document key, or None when it does not belong to the policy."""
    if key == "schedule":
        return f"{policy.policy_no.replace('/', '-')}-schedule.pdf", policy_schedule_pdf(policy)
    kind, _, pk = key.partition(":")
    if kind == "fd":
        fd = FinancialDocument.objects.filter(pk=pk, policy=policy).first()
        if fd:
            return f"{fd.number.replace('/', '-')}.pdf", financial_document_pdf(fd)
    if kind == "rv":
        pay = Payment.objects.filter(pk=pk, policy=policy, status=Payment.PAID).first()
        if pay:
            return f"{pay.receipt_no.replace('/', '-')}.pdf", receipt_pdf(pay)
    return None


def normalise_phone(phone, default_code="968"):
    """Digits only, international format without '+': '9123 4567' -> '96891234567'."""
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("00"):
        d = d[2:]
    elif d.startswith("0") and len(d) > 8:
        d = default_code + d.lstrip("0")
    elif len(d) <= 9:
        d = default_code + d.lstrip("0")
    return d


def make_share(policy, keys, user, channel=""):
    return DocumentShare.objects.create(
        token=secrets.token_urlsafe(24), policy=policy, documents=list(keys), created_by=user, channel=channel,
        expires_at=timezone.now() + timedelta(days=settings.SHARE_LINK_VALIDITY_DAYS))


def base_url(request):
    c = Company.get()
    if c.public_base_url:
        return c.public_base_url.rstrip("/")
    return request.build_absolute_uri("/").rstrip("/")


def share_url(request, share):
    return base_url(request) + reverse("share_view", args=[share.token])


def default_message(policy, link=None):
    c = Company.get()
    lines = [f"Dear {policy.customer.name},", "",
             f"Please find your motor insurance documents for policy {policy.policy_no} "
             f"({policy.start_date:%d %b %Y} to {policy.end_date:%d %b %Y})."]
    if link:
        lines += ["", f"Download (link valid {settings.SHARE_LINK_VALIDITY_DAYS} days): {link}"]
    lines += ["", f"{c.name}" + (f" | {c.phone}" if c.phone else "")]
    return "\n".join(lines)


def whatsapp_url(phone, text):
    return f"whatsapp://send?phone={normalise_phone(phone, Company.get().default_phone_code)}&text={quote(text)}"


def whatsapp_web_url(phone, text):
    return f"https://wa.me/{normalise_phone(phone, Company.get().default_phone_code)}?text={quote(text)}"


def build_eml(policy, keys, to, subject, body):
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg["X-Unsent"] = "1"                  # makes classic Outlook open it as an editable draft
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    for key in keys:
        r = render_document(policy, key)
        if r:
            msg.add_attachment(r[1], maintype="application", subtype="pdf", filename=r[0])
    return msg.as_bytes()

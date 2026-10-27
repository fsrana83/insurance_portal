from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounting.documents import receipt_pdf
from core.models import Company, user_role
from core.permissions import role_required, scope
from motor.models import Policy
from motor.sharing import base_url, normalise_phone, whatsapp_url

from . import services
from .forms import CheckoutForm, ReceiptForm, RequestForm
from .gateways import get_gateway
from .models import Payment

COLLECTORS = ("ADMIN", "PRODUCTION_MANAGER", "FINANCE", "AGENT", "BROKER", "UNDERWRITER", "CREDIT_CONTROL")


def _pdf(content, name):
    r = HttpResponse(content, content_type="application/pdf")
    r["Content-Disposition"] = f'inline; filename="{name}"'
    return r


@role_required(*COLLECTORS)
@require_POST
def record_receipt(request, policy_pk):
    pol = get_object_or_404(scope(request.user, Policy.objects.all()), pk=policy_pk)
    form = ReceiptForm(request.POST, currency=pol.currency)
    if form.is_valid():
        d = form.cleaned_data
        try:
            pay = services.record_receipt(pol, d["amount"], d["method"], request.user, d["reference"], d["payer_name"], d["narration"])
        except services.PaymentError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"Receipt {pay.receipt_no} issued for {pay.amount} {pol.currency.code}.")
    else:
        messages.error(request, "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))
    return redirect("policy_detail", pk=pol.pk)


@role_required(*COLLECTORS)
@require_POST
def request_payment(request, policy_pk):
    pol = get_object_or_404(scope(request.user, Policy.objects.all()), pk=policy_pk)
    form = RequestForm(request.POST, currency=pol.currency)
    if form.is_valid():
        try:
            pay = services.request_online_payment(pol, form.cleaned_data["amount"], request.user, form.cleaned_data["narration"])
        except services.PaymentError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"Payment request {pay.number} created. Share the link below with the customer.")
            return redirect(f"{reverse('policy_detail', args=[pol.pk])}#payments")
    else:
        messages.error(request, "; ".join(f"{k}: {', '.join(v)}" for k, v in form.errors.items()))
    return redirect("policy_detail", pk=pol.pk)


@role_required(*COLLECTORS)
@require_POST
def cancel_request(request, pk):
    pay = get_object_or_404(Payment.objects.filter(policy__in=scope(request.user, Policy.objects.all())), pk=pk)
    try:
        services.cancel(pay)
        messages.info(request, f"Request {pay.number} cancelled.")
    except services.PaymentError as e:
        messages.error(request, str(e))
    return redirect("policy_detail", pk=pay.policy_id)


@login_required
def receipt_download(request, pk):
    pay = get_object_or_404(Payment.objects.filter(policy__in=scope(request.user, Policy.objects.all()), status=Payment.PAID), pk=pk)
    return _pdf(receipt_pdf(pay), f"{pay.receipt_no.replace('/', '-')}.pdf")


@login_required
def payment_list(request):
    if user_role(request.user) == "CUSTOMER":
        raise PermissionDenied
    qs = Payment.objects.filter(policy__in=scope(request.user, Policy.objects.all())).select_related("policy", "customer", "currency")
    status, q = request.GET.get("status"), request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(receipt_no__icontains=q) | Q(policy__policy_no__icontains=q) | Q(customer__name__icontains=q))
    return render(request, "payments/payment_list.html", {"payments": qs[:300], "statuses": Payment.STATUSES, "status": status, "q": q})


def payment_link(request, pay):
    return base_url(request) + reverse("pay_online", args=[pay.token])


@role_required(*COLLECTORS)
def payment_share(request, pk):
    """Message helpers for a pending payment request (WhatsApp / e-mail text)."""
    pay = get_object_or_404(Payment.objects.filter(policy__in=scope(request.user, Policy.objects.all())), pk=pk)
    link = payment_link(request, pay)
    c = Company.get()
    text = (f"Dear {pay.customer.name}, please pay {pay.currency.code} {pay.amount} for motor policy {pay.policy.policy_no} "
            f"securely online: {link}\n{c.name}")
    return render(request, "payments/share.html", {"pay": pay, "link": link, "text": text,
                                                   "wa": whatsapp_url(pay.customer.phone, text) if pay.customer.phone else ""})


# --- public checkout (no login; the unguessable token is the credential) --------------------------------------
def pay_online(request, token):
    pay = get_object_or_404(Payment.objects.select_related("policy", "customer", "currency"), token=token, method=Payment.ONLINE)
    gw = get_gateway(pay.gateway or None)
    if pay.status == Payment.PENDING and pay.is_expired:
        pay.status = Payment.EXPIRED
        pay.save(update_fields=["status"])
    form = CheckoutForm(request.POST or None, initial={"holder": pay.customer.name})
    error = None
    if request.method == "POST" and pay.status == Payment.PENDING and form.is_valid():
        d = form.cleaned_data
        try:
            res = services.pay_online(pay, d["number"], d["expiry"], d["cvv"], d["holder"])
        except services.PaymentError as e:
            error = str(e)
        else:
            if res.success:
                return redirect("pay_online", token=token)
            error = res.message
    return render(request, "payments/checkout.html", {"pay": pay, "form": form, "gw": gw, "error": error, "company": Company.get(),
                                                      "test_cards": getattr(gw, "TEST_CARDS", [])})


def pay_receipt(request, token):
    pay = get_object_or_404(Payment, token=token, status=Payment.PAID)
    return _pdf(receipt_pdf(pay), f"{pay.receipt_no.replace('/', '-')}.pdf")

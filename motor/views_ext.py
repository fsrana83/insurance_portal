"""Customer profile, vouchers, document sharing and downloads."""
import secrets
import string
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import flex
from core.models import Company, FlexField, user_role
from core.permissions import CAN_QUOTE, role_required, scope

from . import sharing
from .forms import CustomerForm, VoucherForm
from .models import Customer, DocumentShare, Policy, Proposal, Voucher

STAFF = ("ADMIN", "AGENT", "BROKER", "PRODUCTION_MANAGER", "UNDERWRITER", "FINANCE", "CLAIMS_HANDLER", "CLAIMS_MANAGER")


def _policy_row(p, request):
    today = date.today()
    expired = p.end_date < today
    renewable = p.status in (Policy.ACTIVE, Policy.EXPIRED) and p.status != Policy.CANCELLED
    open_renewal = p.renewals.filter(status__in=[Proposal.QUOTE, Proposal.PROPOSAL, Proposal.PENDING, Proposal.APPROVED]).first()
    renewed = p.renewals.filter(status=Proposal.ISSUED).exists()
    return {
        "id": p.pk, "policy_no": p.policy_no, "product": p.product.name, "status": "EXPIRED" if (expired and p.status == Policy.ACTIVE) else p.status,
        "start": p.start_date.strftime("%d %b %Y"), "end": p.end_date.strftime("%d %b %Y"), "days_left": (p.end_date - today).days,
        "vehicles": ", ".join(f"{r.make} {r.model} {r.plate or ''}".strip() for r in p.risks.all()),
        "total": f"{p.currency.quantize(p.total_payable):,.{p.currency.decimals}f}", "currency": p.currency.code,
        "payment": p.payment_status, "url": reverse("policy_detail", args=[p.pk]),
        "renewable": renewable and not renewed, "renewed": renewed,
        "open_renewal": {"number": open_renewal.number, "url": reverse("quote_detail", args=[open_renewal.pk])} if open_renewal else None,
        "renew_url": reverse("renew", args=[p.pk]),
    }


@role_required(*CAN_QUOTE)
def customer_policies(request):
    """AJAX: every policy of a customer, so the user can renew one or issue a new policy."""
    cust = get_object_or_404(Customer, pk=request.GET.get("customer") or 0)
    pols = scope(request.user, cust.policies.select_related("product", "currency").prefetch_related("risks", "renewals")).order_by("-start_date")
    return JsonResponse({"customer": {"id": cust.pk, "name": cust.name, "identifier": cust.identifier, "phone": cust.phone},
                         "policies": [_policy_row(p, request) for p in pols[:50]],
                         "open_quotes": [{"number": q.number, "url": reverse("quote_detail", args=[q.pk]), "status": q.get_status_display()}
                                         for q in scope(request.user, cust.proposals.filter(status__in=["QUOTE", "PROPOSAL", "PENDING", "APPROVED"]))[:10]]})


@role_required(*CAN_QUOTE, "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE")
def customer_search(request):
    """AJAX: find customers by (partial) name, civil ID / CR, or mobile - one row per real customer."""
    from .customers import search

    if request.GET.get("id"):
        found = list(Customer.objects.filter(pk=request.GET["id"] if request.GET["id"].isdigit() else 0)[:1])
    else:
        q = request.GET.get("q", "").strip()
        found = search(q, limit=12) if len(q) >= 2 else []
    return JsonResponse({"results": [{"id": c.pk, "name": c.name, "identifier": c.identifier, "phone": c.phone, "type": c.get_customer_type_display(),
                                      "policies": getattr(c, "n_policies", None), "vehicles": getattr(c, "n_vehicles", None)} for c in found]})


@login_required
def customer_detail(request, pk):
    if user_role(request.user) == "CUSTOMER":
        raise PermissionDenied
    cust = get_object_or_404(Customer, pk=pk)
    form = CustomerForm(request.POST or None, instance=cust)
    if request.method == "POST":
        if user_role(request.user) == "FINANCE":
            raise PermissionDenied
        if form.is_valid():
            form.save()
            messages.success(request, "Customer details saved.")
            return redirect("customer_detail", pk=pk)
    pols = scope(request.user, cust.policies.select_related("product", "currency", "producer").prefetch_related("risks")).order_by("-start_date")
    return render(request, "motor/customer_detail.html", {
        "c": cust, "form": form, "policies": pols, "vehicles": cust.vehicles.all(),
        "quotes": scope(request.user, cust.proposals.exclude(status="ISSUED").select_related("product", "currency")),
        "claims": [cl for p in pols for cl in p.claims.all()],
        "producer_history": cust.producer_history.select_related("producer"), "can_assign": user_role(request.user) in ("PRODUCTION_MANAGER", "UNDERWRITER", "ADMIN"),
        "producers": User.objects.filter(is_active=True, groups__name__in=["Agent", "Broker"]).distinct().order_by("username"),
    })


# ---------------------------------------------------------------------------
# Vouchers
# ---------------------------------------------------------------------------
VOUCHER_ROLES = ("PRODUCTION_MANAGER", "UNDERWRITER")


def _gen_code(prefix):
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    while True:
        code = f"{prefix}-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not Voucher.objects.filter(code=code).exists():
            return code


@role_required(*VOUCHER_ROLES, "AGENT", "BROKER", "FINANCE")
def voucher_list(request):
    qs = Voucher.objects.select_related("currency", "customer").prefetch_related("products", "redemptions")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(code__icontains=q) | qs.filter(description__icontains=q)
    can_issue = user_role(request.user) in ("ADMIN",) + VOUCHER_ROLES
    return render(request, "motor/voucher_list.html", {"vouchers": qs[:300], "q": q, "can_issue": can_issue})


@role_required(*VOUCHER_ROLES)
def voucher_create(request):
    form = VoucherForm(request.POST or None, initial={"valid_to": date.today() + timedelta(days=90), "discount_type": Voucher.PERCENT})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        n = d["count"]
        made = []
        for _ in range(n):
            v = form.save(commit=False)
            v.pk = None
            v.code = (d["code"].strip().upper() if (d.get("code") and n == 1) else _gen_code((d.get("prefix") or "PROMO").strip().upper()))
            v.issued_by = request.user
            v.save()
            form.save_m2m()
            made.append(v)
        messages.success(request, f"{len(made)} voucher{'s' if len(made) != 1 else ''} issued: " + ", ".join(v.code for v in made[:8]) + ("…" if len(made) > 8 else ""))
        return redirect("voucher_list")
    return render(request, "motor/voucher_form.html", {"form": form, "title": "Issue discount vouchers"})


@role_required(*VOUCHER_ROLES)
def voucher_edit(request, pk):
    v = get_object_or_404(Voucher, pk=pk)
    form = VoucherForm(request.POST or None, instance=v)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Voucher {v.code} updated.")
        return redirect("voucher_list")
    return render(request, "motor/voucher_form.html", {"form": form, "title": f"Voucher {v.code}", "voucher": v,
                                                        "redemptions": v.redemptions.select_related("policy", "customer")})


@role_required(*VOUCHER_ROLES)
@require_POST
def voucher_toggle(request, pk):
    v = get_object_or_404(Voucher, pk=pk)
    v.active = not v.active
    v.save(update_fields=["active"])
    messages.info(request, f"Voucher {v.code} {'activated' if v.active else 'deactivated'}.")
    return redirect("voucher_list")


# ---------------------------------------------------------------------------
# Documents & sharing
# ---------------------------------------------------------------------------
def _own_policy(request, pk):
    return get_object_or_404(scope(request.user, Policy.objects.select_related("customer", "currency", "product")), pk=pk)


@login_required
def policy_document(request, pk, key):
    pol = _own_policy(request, pk)
    r = sharing.render_document(pol, key)
    if not r:
        raise Http404
    resp = HttpResponse(r[1], content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{r[0]}"'
    return resp


@role_required(*STAFF)
def policy_share(request, pk):
    pol = _own_policy(request, pk)
    docs = sharing.available_documents(pol)
    cust = pol.customer
    keys = request.POST.getlist("docs") or [k for k, _ in docs[:1]]
    valid = {k for k, _ in docs}
    keys = [k for k in keys if k in valid]
    ctx = {"p": pol, "docs": docs, "selected": keys, "email": request.POST.get("email", cust.email), "phone": request.POST.get("phone", cust.phone),
           "subject": request.POST.get("subject", f"Motor insurance policy {pol.policy_no}")}
    action = request.POST.get("action")
    if request.method == "POST" and action:
        if not keys:
            messages.error(request, "Tick at least one document.")
        elif action == "outlook":
            if not ctx["email"]:
                messages.error(request, "Enter the recipient's e-mail address.")
            else:
                share = sharing.make_share(pol, keys, request.user, "outlook")
                body = request.POST.get("message") or sharing.default_message(pol)
                eml = sharing.build_eml(pol, keys, ctx["email"], ctx["subject"], body)
                resp = HttpResponse(eml, content_type="message/rfc822")
                resp["Content-Disposition"] = f'attachment; filename="{pol.policy_no.replace("/", "-")}-draft.eml"'
                return resp
        elif action == "whatsapp":
            if not ctx["phone"]:
                messages.error(request, "Enter the recipient's mobile number.")
            else:
                share = sharing.make_share(pol, keys, request.user, "whatsapp")
                link = sharing.share_url(request, share)
                text = request.POST.get("message") or sharing.default_message(pol, link)
                if link not in text:
                    text += f"\n{link}"
                ctx.update(wa_desktop=sharing.whatsapp_url(ctx["phone"], text), wa_web=sharing.whatsapp_web_url(ctx["phone"], text),
                           wa_text=text, wa_link=link, wa_phone=sharing.normalise_phone(ctx["phone"], Company.get().default_phone_code))
        elif action == "email":
            if not ctx["email"]:
                messages.error(request, "Enter the recipient's e-mail address.")
            else:
                from django.core.mail import EmailMessage
                msg = EmailMessage(ctx["subject"], request.POST.get("message") or sharing.default_message(pol), to=[ctx["email"]])
                for k in keys:
                    r = sharing.render_document(pol, k)
                    if r:
                        msg.attach(r[0], r[1], "application/pdf")
                msg.send(fail_silently=True)
                from .models import PolicyDocument
                PolicyDocument.objects.create(policy=pol, kind=",".join(keys)[:20], sent_to=ctx["email"])
                messages.success(request, f"Documents e-mailed to {ctx['email']} (check the e-mail backend settings: the default prints to the server console).")
    ctx["message"] = request.POST.get("message") or sharing.default_message(pol)
    return render(request, "motor/share.html", ctx)


def share_view(request, token):
    """Public landing page for an expiring share link."""
    sh = get_object_or_404(DocumentShare.objects.select_related("policy__customer"), token=token)
    if sh.expires_at < timezone.now():
        return render(request, "motor/share_public.html", {"expired": True, "company": Company.get()}, status=410)
    labels = dict(sharing.available_documents(sh.policy))
    return render(request, "motor/share_public.html", {"sh": sh, "company": Company.get(),
                                                       "docs": [(k, labels[k]) for k in sh.documents if k in labels]})


def share_download(request, token, key):
    sh = get_object_or_404(DocumentShare, token=token)
    if sh.expires_at < timezone.now() or key not in sh.documents:
        raise Http404
    r = sharing.render_document(sh.policy, key)
    if not r:
        raise Http404
    DocumentShare.objects.filter(pk=sh.pk).update(downloads=sh.downloads + 1)
    resp = HttpResponse(r[1], content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="{r[0]}"'
    return resp

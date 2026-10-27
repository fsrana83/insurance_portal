"""Company setup and flex-field administration (Admin only)."""
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Company, FlexField
from .permissions import role_required


def _company_form():
    from django import forms
    from motor.forms import StyledMixin

    class CompanyForm(StyledMixin, forms.ModelForm):
        class Meta:
            model = Company
            exclude = []
            widgets = {"logo": forms.ClearableFileInput(attrs={"accept": "image/png,image/jpeg"})}

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._style()

        def clean_logo(self):
            f = self.cleaned_data.get("logo")
            if f and hasattr(f, "name") and not f.name.lower().endswith((".png", ".jpg", ".jpeg")):
                raise forms.ValidationError("Upload a PNG or JPG logo.")
            if f and getattr(f, "size", 0) > 2 * 1024 * 1024:
                raise forms.ValidationError("The logo must be smaller than 2 MB.")
            return f

    return CompanyForm


@role_required()          # Admin only
def company_setup(request):
    company = Company.get()
    Form = _company_form()
    form = Form(request.POST or None, request.FILES or None, instance=company)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Company details saved. They now appear on every document.")
        return redirect("company_setup")
    groups = [("Identity", ["name", "legal_name", "cr_number", "vat_number", "fsa_license_no", "logo"]),
              ("Contact", ["address", "city", "country", "phone", "email", "website", "whatsapp_number", "default_phone_code"]),
              ("Banking (printed on invoices)", ["bank_name", "bank_account", "iban", "swift", "invoice_footer"]),
              ("Links & AI", ["public_base_url", "ollama_host", "ollama_model"])]
    return render(request, "core/company_setup.html", {"form": form, "groups": [(t, [form[f] for f in names]) for t, names in groups],
                                                       "company": company})


@role_required("PRODUCTION_MANAGER", "UNDERWRITER")
def flex_list(request):
    return render(request, "core/flex_list.html", {"fields": FlexField.objects.all(), "entities": FlexField.ENTITIES})


@role_required("PRODUCTION_MANAGER", "UNDERWRITER")
def flex_edit(request, pk=None):
    from motor.forms import FlexFieldForm

    obj = get_object_or_404(FlexField, pk=pk) if pk else None
    form = FlexFieldForm(request.POST or None, instance=obj, initial={"entity": request.GET.get("entity", "POLICY")} if not obj else None)
    if request.method == "POST" and form.is_valid():
        f = form.save(commit=False)
        if not obj:
            f.key = form.cleaned_data["key"]
        f.save()
        messages.success(request, f"Flex field “{f.label}” saved. It appears on the {f.get_entity_display().lower()} form straight away.")
        return redirect("flex_list")
    return render(request, "core/flex_form.html", {"form": form, "obj": obj})


@role_required("PRODUCTION_MANAGER", "UNDERWRITER")
@require_POST
def flex_toggle(request, pk):
    f = get_object_or_404(FlexField, pk=pk)
    f.active = not f.active
    f.save(update_fields=["active"])
    messages.info(request, f"“{f.label}” is now {'active' if f.active else 'hidden'}. Stored values are kept.")
    return redirect("flex_list")


# --------------------------------------------------------------------------- demo data (Admin only)
@role_required()
def demo_data(request):
    from django.http import JsonResponse  # noqa: F401
    from . import demo
    from .models import DemoRun

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "load":
                n = max(10, min(int(request.POST.get("policies") or 1000), 5000))
                c = max(0, min(int(request.POST.get("claims") or 300), 2000))
                demo.start_background(DemoRun.LOAD, {"policies": n, "claims": c, "seed": 42, "years": max(1, min(int(request.POST.get("years") or 5), 10))}, request.user)
            elif action == "unload":
                demo.start_background(DemoRun.UNLOAD, {"remove_users": bool(request.POST.get("remove_users"))}, request.user)
        except (RuntimeError, ValueError) as e:
            messages.error(request, str(e))
        return redirect("demo_data")
    counts = demo.demo_counts()
    run = DemoRun.objects.order_by("-id").first()
    return render(request, "core/demo_data.html", {"counts": counts, "loaded": counts["policies"] > 0, "run": run})


@role_required()
def demo_status(request, pk):
    from django.http import JsonResponse
    from .models import DemoRun

    r = get_object_or_404(DemoRun, pk=pk)
    return JsonResponse({"status": r.status, "stage": r.stage, "done": r.done, "total": r.total, "message": r.message})


# ---- dual confirmation and change log -------------------------------------------------------------------------------
from django.core.paginator import Paginator
from django.http import HttpResponse

from . import confirm as dual
from .models import ChangeLog, PendingAction, user_role


@role_required("ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE", "AGENT", "BROKER")
def confirmations(request):
    view = request.GET.get("view", "pending")
    qs = PendingAction.objects.select_related("requested_by", "reviewed_by")
    role = user_role(request.user)
    if role in ("AGENT", "BROKER"):
        qs = qs.filter(requested_by=request.user)
    if view == "pending":
        qs = qs.filter(status=PendingAction.PENDING)
    elif view == "mine":
        qs = qs.filter(requested_by=request.user)
    rows = [(p, dual.can_review(request.user, p)) for p in qs[:200]]
    return render(request, "core/confirmations.html", {"rows": rows, "view": view})


@role_required("ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER")
@require_POST
def confirmation_review(request, pk, decision):
    pending = get_object_or_404(PendingAction, pk=pk)
    try:
        if decision == "confirm":
            ok, msgs = dual.confirm(pending, request.user)
            for lvl, text in msgs:
                getattr(messages, lvl if lvl in ("success", "warning", "error", "info") else "info")(request, f"{pending.number}: {text}")
        else:
            dual.reject(pending, request.user, request.POST.get("note", ""))
            messages.info(request, f"{pending.number} rejected.")
    except PermissionError as e:
        messages.error(request, str(e))
    return redirect(request.POST.get("next") or "confirmations")


@role_required("ADMIN", "PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE")
def changelog(request):
    """Who changed what, when: field by field for policies and claims. Nothing is listed until a period is run."""
    from datetime import date
    g = request.GET
    ctx = {"waiting": g.get("run") != "1", "g": g, "kinds": [("motor.Policy", "Policy"), ("claims.Claim", "Claim"), ("claims.RepairEstimate", "Garage estimate"),
                                                              ("claims.ClaimPayment", "Claim payment"), ("claims.Injury", "Injury / death"),
                                                              ("claims.LegalAction", "Legal action"), ("claims.Subrogation", "Subrogation"), ("motor.Endorsement", "Endorsement")]}
    if not ctx["waiting"]:
        try:
            a, b = date.fromisoformat(g.get("from", "")), date.fromisoformat(g.get("to", ""))
        except ValueError:
            ctx["waiting"], ctx["error"] = True, "Choose a valid period first."
            return render(request, "core/changelog.html", ctx)
        qs = ChangeLog.objects.select_related("user", "confirmed_by").filter(timestamp__date__gte=a, timestamp__date__lte=b)
        if g.get("ref"):
            qs = qs.filter(ref__icontains=g["ref"].strip())
        if g.get("model"):
            qs = qs.filter(model=g["model"])
        if g.get("user"):
            qs = qs.filter(user__username__icontains=g["user"].strip())
        if g.get("format") == "csv":
            import csv
            resp = HttpResponse(content_type="text/csv")
            resp["Content-Disposition"] = 'attachment; filename="change-log.csv"'
            w = csv.writer(resp)
            w.writerow(["When", "Reference", "Record", "Action", "Field", "Old value", "New value", "Changed by", "Confirmed by", "Reason"])
            for c in qs.iterator():
                w.writerow([c.timestamp.strftime("%Y-%m-%d %H:%M:%S"), c.ref, c.object_repr, c.action, c.field, c.old_value, c.new_value,
                            c.user.get_username() if c.user else "", c.confirmed_by.get_username() if c.confirmed_by else "", c.reason])
            return resp
        ctx["page"] = Paginator(qs, 100).get_page(g.get("page"))
        ctx["query"] = "&".join(f"{k}={v}" for k, v in g.items() if k != "page")
    return render(request, "core/changelog.html", ctx)


@login_required
def help_page(request):
    from . import helpdocs
    secs = helpdocs.sections()
    for sec in secs:
        if sec.get("flow"):
            sec["svg"] = helpdocs.flow(sec["flow"], title=sec["title"])
    groups = {}
    for sec in secs:
        groups.setdefault(sec["group"], []).append(sec)
    return render(request, "core/help.html", {"sections": secs, "groups": groups})

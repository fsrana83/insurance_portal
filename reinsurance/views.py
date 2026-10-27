from decimal import Decimal

from django.contrib import messages
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import user_role
from core.permissions import role_required
from motor.models import Policy

from . import engine
from .forms import AmendmentForm, FacForm, MDPForm, TreatyForm
from .models import Cession, Treaty

ROLES = ("UNDERWRITER", "FINANCE")


@role_required(*ROLES)
def treaty_list(request):
    current = Treaty.objects.filter(superseded_by__isnull=True).select_related("reinsurer").order_by("rank")
    rows = []
    for t in current:
        used, cap, pct = engine.capacity_utilisation(t) if t.treaty_type == "SURPLUS" else (None, None, None)
        rows.append({"t": t, "used": used, "cap": cap, "pct": min(pct, Decimal(100)) if pct is not None else None,
                     "versions": Treaty.objects.filter(reference=t.reference).count()})
    return render(request, "reinsurance/treaty_list.html", {"rows": rows, "fac_pending": Cession.objects.filter(is_facultative=True, fac_confirmed=False).count()})


@role_required("UNDERWRITER")
def treaty_create(request):
    form = TreatyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        t = form.save(commit=False)
        if not t.reference:
            t.reference = f"TR-{t.effective_from.year}-{Treaty.objects.values('reference').distinct().count() + 1:03d}"
        t.save()
        form.save_m2m()
        if t.treaty_type == Treaty.XOL:
            engine.schedule_mdp(t)
        messages.success(request, f"Treaty {t.reference} created.")
        return redirect("treaty_detail", pk=t.pk)
    return render(request, "reinsurance/treaty_form.html", {"form": form, "title": "New treaty"})


@role_required("UNDERWRITER")
def treaty_modify(request, pk):
    t = get_object_or_404(Treaty, pk=pk, superseded_by__isnull=True)
    form = TreatyForm(request.POST or None, instance=t, modify=True)
    note = AmendmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid() and note.is_valid():
        changes = {k: v for k, v in form.cleaned_data.items() if k not in ("covers", "reference", "treaty_type")}
        # NB the form instance was mutated by validation; reload the stored row before versioning
        original = Treaty.objects.get(pk=pk)
        new, warnings = engine.modify_treaty(original, note.cleaned_data["note"], **changes)
        new.covers.set(form.cleaned_data["covers"])
        if new.treaty_type == Treaty.XOL:
            engine.schedule_mdp(new)
        messages.success(request, f"Treaty amended: version {new.version} created. Version {original.version} is kept for history.")
        for w in warnings:
            messages.warning(request, w)
        return redirect("treaty_detail", pk=new.pk)
    return render(request, "reinsurance/treaty_form.html", {"form": form, "note": note, "title": f"Modify {t.name}", "modify": True, "t": t})


@role_required(*ROLES)
def treaty_detail(request, pk):
    t = get_object_or_404(Treaty.objects.select_related("reinsurer"), pk=pk)
    versions = Treaty.objects.filter(reference=t.reference).order_by("-version")
    role = user_role(request.user)
    ctx = {"t": t, "versions": versions, "is_current": t.superseded_by_id is None}
    if t.treaty_type == Treaty.SURPLUS:
        ctx["util"] = engine.capacity_utilisation(t)
    if t.treaty_type == Treaty.XOL:
        actual = engine.actual_gnrpi(t)
        form = MDPForm(request.POST or None, initial={"actual_gnrpi": actual})
        ctx.update({"installments": t.mdp_installments.all(), "mdp_form": form, "actual": actual})
        add, earned = engine.mdp_adjustment(t, actual)
        if request.method == "POST" and form.is_valid():
            add, earned = engine.mdp_adjustment(t, form.cleaned_data["actual_gnrpi"])
            ctx["actual"] = form.cleaned_data["actual_gnrpi"]
        ctx.update({"additional": add, "earned": earned, "pos": engine.xol_position(t), "can_post": role in ("FINANCE", "ADMIN")})
    return render(request, "reinsurance/treaty_detail.html", ctx)


@role_required(*ROLES)
def fac_queue(request):
    pending = Cession.objects.filter(is_facultative=True, fac_confirmed=False).select_related("policy__customer", "policy__currency", "risk")
    done = Cession.objects.filter(is_facultative=True, fac_confirmed=True).select_related("policy", "fac_reinsurer")[:20]
    return render(request, "reinsurance/fac_queue.html", {"pending": pending, "done": done, "form": FacForm()})


@role_required("UNDERWRITER")
@require_POST
def fac_confirm(request, pk):
    c = get_object_or_404(Cession, pk=pk, is_facultative=True)
    form = FacForm(request.POST)
    if form.is_valid():
        engine.confirm_facultative(c, form.cleaned_data["reinsurer"])
        messages.success(request, f"Facultative placement confirmed with {form.cleaned_data['reinsurer']}. Policy {c.policy.policy_no} is now bound.")
    return redirect("fac_queue")


@role_required("FINANCE")
@require_POST
def installment_pay(request, pk):
    from .models import MDPInstallment

    inst = get_object_or_404(MDPInstallment, pk=pk)
    entry = engine.pay_installment(inst, request.user, reference=request.POST.get("reference", ""))
    messages.success(request, f"Instalment of {inst.amount} OMR paid and posted ({entry.number})." if entry else "Instalment was already paid.")
    return redirect("treaty_detail", pk=inst.treaty_id)


@role_required("FINANCE")
@require_POST
def xol_adjust(request, pk):
    t = get_object_or_404(Treaty, pk=pk, treaty_type=Treaty.XOL)
    entry = engine.post_xol_adjustment(t, request.user, force=request.POST.get("force") == "1")
    messages.success(request, f"Top-up to minimum premium posted ({entry.number})." if entry else
                     "Nothing to post: the treaty has not expired yet, or the accrued premium already reaches the minimum.")
    return redirect("treaty_detail", pk=t.pk)


@role_required(*ROLES)
def xol_analysis(request, pk):
    """Cat / aggregate / stop-loss positions, reinstatement premium and burning-cost pricing for an XoL treaty."""
    from claims.models import CatEvent
    from . import xol
    from .models import AggRecovery

    t = get_object_or_404(Treaty, pk=pk, treaty_type=Treaty.XOL)
    ctx = {"t": t, "bc": xol.burning_cost(t), "can_post": user_role(request.user) in ("FINANCE", "ADMIN"), "can_price": user_role(request.user) in ("UNDERWRITER", "ADMIN"),
           "saved": {(r.event_id): r for r in AggRecovery.objects.filter(treaty=t)}}
    if t.xol_kind == Treaty.CAT:
        events = CatEvent.objects.filter(claims__date_of_loss__gte=t.effective_from, claims__date_of_loss__lte=t.effective_to).distinct()
        ctx["positions"] = [(e, xol.cat_position(t, e), ctx["saved"].get(e.pk)) for e in events]
    elif t.xol_kind in (Treaty.AGGREGATE, Treaty.STOP_LOSS):
        ctx["positions"] = [(None, xol.position(t), ctx["saved"].get(None))]
    else:
        ctx["positions"] = []
    return render(request, "reinsurance/xol_analysis.html", ctx)


@role_required("FINANCE")
@require_POST
def xol_post(request, pk):
    from claims.models import CatEvent
    from . import xol
    t = get_object_or_404(Treaty, pk=pk, treaty_type=Treaty.XOL)
    event = get_object_or_404(CatEvent, pk=request.POST["event"]) if request.POST.get("event") else None
    row = xol.post_position(t, event, request.user)
    messages.success(request, f"{row.label}: recovery of {row.recovery} OMR posted." if row else "This treaty has no total-based recovery.")
    return redirect("xol_analysis", pk=pk)


@role_required("FINANCE")
@require_POST
def xol_receive(request, pk):
    from . import xol
    from .models import AggRecovery
    row = get_object_or_404(AggRecovery, pk=pk)
    try:
        xol.receive_cash(row, Decimal(request.POST.get("amount") or row.due), request.user)
        messages.success(request, "Cash from the reinsurer recorded.")
    except (ValueError, ArithmeticError) as e:
        messages.error(request, str(e))
    return redirect("xol_analysis", pk=row.treaty_id)


@role_required("UNDERWRITER")
@require_POST
def xol_apply_rate(request, pk):
    from . import xol
    t = get_object_or_404(Treaty, pk=pk, superseded_by__isnull=True, treaty_type=Treaty.XOL)
    bc = xol.burning_cost(t)
    new, _ = engine.modify_treaty(t, f"Rate set to burning cost indication {bc['indicated_rate']}%", rate_pct=bc["indicated_rate"])
    engine.schedule_mdp(new)
    messages.success(request, f"New version {new.version} created at the indicated rate of {bc['indicated_rate']}%.")
    return redirect("treaty_detail", pk=new.pk)

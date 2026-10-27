import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import audit
from core.models import user_role
from core.permissions import role_required
from reinsurance.models import Treaty

from . import nonmotor as nm
from .models import PncClaim, PncDeclaration, PncMember, PncPolicy, PncPreauth, PncReserveChange, PncRiRecovery
from .services import PncError
from .views import _claim, _err, _policy

STAFF = ("PRODUCTION_MANAGER", "UNDERWRITER", "CLAIMS_HANDLER", "CLAIMS_MANAGER", "FINANCE", "AGENT", "BROKER")


def _d(s):
    try:
        return nm._date(s)
    except ValueError as e:
        raise PncError(str(e))


@role_required(*STAFF)
def medical(request, pk):
    p = _policy(request, pk)
    return render(request, "pnc/medical.html", {"p": p, "members": p.members.all(), "preauths": PncPreauth.objects.filter(member__policy=p).select_related("member"),
                                                 "relations": PncMember.RELATIONS, "role": user_role(request.user),
                                                 "is_medical": p.pnc_class.group == "MEDICAL"})


@role_required(*STAFF)
@require_POST
def medical_action(request, pk, action):
    p = _policy(request, pk)
    P = request.POST
    try:
        with audit.acting(request.user):
            if action == "add":
                nm.add_member(p, P.get("name"), P.get("relation", "SELF"), P.get("category", ""), _d(P.get("dob")), _d(P.get("effective_from")))
                messages.success(request, "Member added.")
            elif action == "census":
                f = request.FILES.get("file")
                text = f.read().decode("utf-8-sig") if f else P.get("text", "")
                added, errors = nm.upload_census(p, text)
                messages.success(request, f"{added} member(s) added.")
                for e in errors[:15]:
                    messages.error(request, e)
            elif action == "remove":
                nm.remove_member(get_object_or_404(PncMember, pk=P.get("member_id"), policy=p), _d(P.get("effective")) or nm.clock.today())
                messages.success(request, "Member removed from the effective date.")
            elif action == "preauth":
                m = get_object_or_404(PncMember, pk=P.get("member_id"), policy=p)
                pa = nm.request_preauth(m, P.get("provider", ""), _d(P.get("service_date")) or nm.clock.today(), P.get("amount"), P.get("diagnosis", ""))
                messages.success(request, f"{pa.number} requested.")
            elif action in ("approve", "decline", "settle"):
                pa = get_object_or_404(PncPreauth, pk=P.get("preauth_id"), member__policy=p)
                if action == "settle":
                    nm.settle_preauth(pa, P.get("actual"), P.get("invoice_no", ""))
                else:
                    nm.decide_preauth(pa, request.user, action == "approve", P.get("amount") or None, P.get("note", ""))
                messages.success(request, f"{pa.number} updated.")
    except (PncError, ArithmeticError) as e:
        _err(request, e)
    return redirect("pnc_medical", pk=p.pk)


@role_required(*STAFF)
def declarations(request, pk):
    p = _policy(request, pk)
    risks = [(r, nm.declared_total(r), r.sum_insured - nm.declared_total(r)) for r in p.risks.filter(active=True)]
    return render(request, "pnc/declarations.html", {"p": p, "risks": risks, "decs": p.declarations.all()})


@role_required(*STAFF)
@require_POST
def declaration_action(request, pk, action):
    p = _policy(request, pk)
    P = request.POST
    try:
        with audit.acting(request.user):
            if action == "add":
                risk = get_object_or_404(p.risks, pk=P.get("risk_id"))
                d = nm.add_declaration(p, risk, request.user, P.get("voyage", ""), P.get("value"), P.get("conveyance", ""), P.get("invoice_no", ""), _d(P.get("declared_on")))
                messages.success(request, f"{d.number} declared, premium {d.premium}.")
            elif action == "cancel":
                nm.cancel_declaration(get_object_or_404(PncDeclaration, pk=P.get("dec_id"), policy=p))
                messages.success(request, "Declaration cancelled.")
    except (PncError, ArithmeticError) as e:
        _err(request, e)
    return redirect("pnc_declarations", pk=p.pk)


@role_required("CLAIMS_MANAGER")
def reserve_changes(request):
    return render(request, "pnc/reserve_changes.html", {"rows": PncReserveChange.objects.select_related("claim", "requested_by", "decided_by")[:200]})


@role_required("CLAIMS_MANAGER")
@require_POST
def reserve_decide(request, pk, action):
    ch = get_object_or_404(PncReserveChange, pk=pk)
    try:
        with audit.acting(request.user):
            nm.decide_reserve(ch, request.user, action == "approve")
        messages.success(request, "Reserve change " + ("applied." if action == "approve" else "rejected."))
    except PncError as e:
        _err(request, e)
    return redirect("pnc_reserve_changes")


@role_required("FINANCE", "CLAIMS_MANAGER")
def ri_recoveries(request):
    qs = PncRiRecovery.objects.select_related("claim__policy", "treaty")
    st = request.GET.get("status", "")
    if st:
        qs = qs.filter(status=st)
    return render(request, "pnc/ri.html", {"rows": qs[:300], "statuses": PncRiRecovery.STATUSES, "status": st, "treaties": Treaty.objects.filter(business="PNC")})


@role_required("FINANCE", "CLAIMS_MANAGER")
@require_POST
def ri_update(request, pk, action):
    rec = get_object_or_404(PncRiRecovery, pk=pk)
    try:
        with audit.acting(request.user):
            nm.ri_action(rec, request.user, action, request.POST.get("amount") or None, request.POST.get("note", ""))
        messages.success(request, "Recovery updated.")
    except (PncError, ArithmeticError) as e:
        _err(request, e)
    return redirect("pnc_ri")


@role_required("FINANCE", "CLAIMS_MANAGER")
def bordereau(request, kind):
    G = request.GET
    treaty = Treaty.objects.filter(pk=G.get("treaty")).first() if G.get("treaty") else None
    start, end = (nm._date(G.get("from")) if G.get("from") else None), (nm._date(G.get("to")) if G.get("to") else None)
    head, rows = (nm.PREMIUM_HEAD, nm.premium_bordereau(treaty, start, end)) if kind == "premium" else (nm.CLAIMS_HEAD, nm.claims_bordereau(treaty, start, end))
    r = HttpResponse(content_type="text/csv")
    r["Content-Disposition"] = f'attachment; filename="pnc_{kind}_bordereau.csv"'
    w = csv.writer(r)
    w.writerow(head)
    w.writerows(rows)
    return r

from django.urls import path

from . import views as v
from . import views_nm as nmv

urlpatterns = [
    path("", v.home, name="pnc_home"),
    path("policies/", v.policy_list, name="pnc_policy_list"),
    path("policies/new/", v.policy_new, name="pnc_policy_new"),
    path("policies/<int:pk>/", v.policy_detail, name="pnc_policy_detail"),
    path("policies/<int:pk>/edit/", v.policy_new, name="pnc_policy_edit"),
    path("policies/<int:pk>/risk/", v.risk_add, name="pnc_risk_add"),
    path("policies/<int:pk>/risk/<int:rid>/remove/", v.risk_remove, name="pnc_risk_remove"),
    path("policies/<int:pk>/submit/", v.submit, name="pnc_submit"),
    path("policies/<int:pk>/decide/<str:action>/", v.decide, name="pnc_decide"),
    path("policies/<int:pk>/credit/", v.credit_request, name="pnc_credit_request"),
    path("policies/<int:pk>/issue/", v.issue, name="pnc_issue"),
    path("policies/<int:pk>/receipt/", v.receipt, name="pnc_receipt"),
    path("policies/<int:pk>/pdf/", v.policy_pdf, name="pnc_policy_pdf"),
    path("policies/<int:pk>/endorse/", v.endorse, name="pnc_endorse"),
    path("policies/<int:pk>/claim/", v.claim_new, name="pnc_claim_new"),
    path("credit/", v.credit_list, name="pnc_credit_list"),
    path("credit/<int:pk>/<str:action>/", v.credit_decide, name="pnc_credit_decide"),
    path("claims/", v.claim_list, name="pnc_claim_list"),
    path("claims/<int:pk>/", v.claim_detail, name="pnc_claim_detail"),
    path("claims/<int:pk>/<str:action>/", v.claim_action, name="pnc_claim_action"),
    path("policies/<int:pk>/medical/", nmv.medical, name="pnc_medical"),
    path("policies/<int:pk>/medical/<str:action>/", nmv.medical_action, name="pnc_medical_action"),
    path("policies/<int:pk>/declarations/", nmv.declarations, name="pnc_declarations"),
    path("policies/<int:pk>/declarations/<str:action>/", nmv.declaration_action, name="pnc_declaration_action"),
    path("reserve-changes/", nmv.reserve_changes, name="pnc_reserve_changes"),
    path("reserve-changes/<int:pk>/<str:action>/", nmv.reserve_decide, name="pnc_reserve_decide"),
    path("ri/", nmv.ri_recoveries, name="pnc_ri"),
    path("ri/<int:pk>/<str:action>/", nmv.ri_update, name="pnc_ri_update"),
    path("ri/bordereau/<str:kind>/", nmv.bordereau, name="pnc_bordereau"),
    path("reports/", v.report, name="pnc_reports"),
    path("reports/<slug:slug>/", v.report, name="pnc_report"),
]

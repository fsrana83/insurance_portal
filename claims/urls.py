from django.urls import path

from . import views, views_heads

urlpatterns = [
    path("", views.claim_list, name="claim_list"),
    path("new/", views.claim_new, name="claim_new"),
    path("providers/", views.provider_list, name="provider_list"),
    path("ai/", views.ai_models, name="ai_models"),
    path("api/ollama-models/", views.ollama_models, name="ollama_models"),
    path("api/estimate/", views.claim_estimate, name="claim_estimate"),
    path("api/policy-search/", views.policy_search, name="policy_search"),
    path("api/policy/<int:policy_id>/vehicles/", views.policy_vehicles, name="policy_vehicles"),
    path("api/risk/<int:risk_id>/", views.risk_info, name="risk_info"),
    path("legal/", views.legal_list, name="legal_list"),
    path("notice/<int:pk>/", views.notice_view, name="notice_view"),
    path("guarantee/<int:pk>/", views.guarantee_letter, name="guarantee_letter"),
    path("cat/", views.cat_list, name="cat_list"),
    path("cat/<int:pk>/", views.cat_detail, name="cat_detail"),
    path("<int:pk>/heads/", views_heads.claim_heads, name="claim_heads"),
    path("<int:pk>/heads/<str:action>/", views_heads.head_action, name="head_action"),
    path("<int:pk>/", views.claim_detail, name="claim_detail"),
    path("<int:pk>/demand-letter/", views.demand_letter, name="demand_letter"),
    path("<int:pk>/<str:action>/", views.claim_action, name="claim_action"),
]

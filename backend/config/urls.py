from django.contrib import admin
from django.urls import path

from leadflow.crm.api.auth import LoginView
from leadflow.crm.api.auth import LogoutView
from leadflow.crm.api.auth import SessionView
from leadflow.crm.api.leads import LeadDetailView
from leadflow.crm.api.leads import LeadListView
from leadflow.crm.api.leads import LeadStatusView
from leadflow.crm.api.leads import TagDetailView
from leadflow.crm.api.leads import TagListView
from leadflow.crm.health import HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/auth/session/", SessionView.as_view(), name="crm-session"),
    path("api/auth/login/", LoginView.as_view(), name="crm-login"),
    path("api/auth/logout/", LogoutView.as_view(), name="crm-logout"),
    path("api/tags/", TagListView.as_view(), name="crm-tags"),
    path("api/tags/<int:tag_id>/", TagDetailView.as_view(), name="crm-tag-detail"),
    path("api/leads/", LeadListView.as_view(), name="crm-leads"),
    path("api/leads/<uuid:lead_id>/status/", LeadStatusView.as_view(), name="crm-lead-status"),
    path("api/leads/<uuid:lead_id>/", LeadDetailView.as_view(), name="crm-lead-detail"),
]

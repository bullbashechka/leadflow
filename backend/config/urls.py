from django.contrib import admin
from django.urls import path

from leadflow.crm.api.auth import LoginView
from leadflow.crm.api.auth import LogoutView
from leadflow.crm.api.auth import SessionView
from leadflow.crm.health import HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/auth/session/", SessionView.as_view(), name="crm-session"),
    path("api/auth/login/", LoginView.as_view(), name="crm-login"),
    path("api/auth/logout/", LogoutView.as_view(), name="crm-logout"),
]

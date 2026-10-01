from django.contrib import admin
from django.urls import path

from leadflow.crm.health import HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
]

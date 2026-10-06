from unittest.mock import patch

import pytest
from django.contrib.auth.hashers import make_password
from django.db import OperationalError
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_health_checks_process_without_login():
    response = APIClient().get("/api/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_readiness_reports_database_failure_without_details(settings):
    settings.CRM_DEMO_PASSWORD_HASH = make_password("synthetic-health")
    client = APIClient(enforce_csrf_checks=True)
    csrf = client.get("/api/auth/session/").json()["csrf_token"]
    assert (
        client.post(
            "/api/auth/login/",
            {"password": "synthetic-health"},
            format="json",
            HTTP_X_CSRFTOKEN=csrf,
        ).status_code
        == 200
    )
    with patch("leadflow.crm.health.check_database") as cursor:
        cursor.side_effect = OperationalError("private connection details")
        response = client.get("/api/readiness/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.parametrize(
    "path", ["/accounts/signup/", "/users/", "/api/users/", "/api/auth-token/"]
)
def test_public_account_routes_are_absent(path):
    assert APIClient().get(path).status_code == 404

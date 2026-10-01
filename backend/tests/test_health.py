from unittest.mock import patch

import pytest
from django.db import OperationalError
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_health_checks_real_database_without_login():
    response = APIClient().get("/api/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response["Cache-Control"] == "no-store"


def test_health_reports_database_failure_without_details():
    with patch("django.db.backends.postgresql.base.DatabaseWrapper.cursor") as cursor:
        cursor.side_effect = OperationalError("private connection details")
        response = APIClient().get("/api/health/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.parametrize(
    "path", ["/accounts/signup/", "/users/", "/api/users/", "/api/auth-token/"]
)
def test_public_account_routes_are_absent(path):
    assert APIClient().get(path).status_code == 404

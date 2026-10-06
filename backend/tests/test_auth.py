from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.db import close_old_connections
from django.test import override_settings
from django.urls import path
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView

from config.urls import urlpatterns as application_urls
from leadflow.crm.models import LoginAttempt


class ProtectedView(APIView):
    def get(self, request):
        return Response({"private": True})

    def post(self, request):
        return Response({"saved": True})


urlpatterns = [*application_urls, path("api/test-protected/", ProtectedView.as_view())]
pytestmark = pytest.mark.django_db
PASSWORD = "test-demo-password"


@pytest.fixture(autouse=True)
def auth_settings(settings):
    settings.CRM_DEMO_PASSWORD_HASH = make_password(PASSWORD)
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def client():
    return APIClient(enforce_csrf_checks=True)


def discover(client):
    response = client.get("/api/auth/session/")
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    data = response.json()
    assert data["csrf_token"]
    assert data["server_time"]
    return data


def login(client, password=PASSWORD):
    token = discover(client)["csrf_token"]
    return client.post(
        "/api/auth/login/", {"password": password}, format="json", HTTP_X_CSRFTOKEN=token
    )


def logout(client):
    token = discover(client)["csrf_token"]
    return client.post("/api/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=token)


def test_discovery_is_public_and_login_grants_access(client):
    assert discover(client)["authenticated"] is False
    before = timezone.now()
    response = login(client)
    assert response.status_code == 200
    data = response.json()
    assert data["authenticated"] is True
    expiry = timezone.datetime.fromisoformat(data["expires_at"])
    assert before + timedelta(hours=48) <= expiry <= timezone.now() + timedelta(hours=48)
    assert client.get("/api/test-protected/").json() == {"private": True}
    assert client.cookies["sessionid"]["httponly"]
    assert client.cookies["sessionid"]["samesite"] == "Lax"
    assert client.cookies["sessionid"]["max-age"]
    assert not client.cookies["sessionid"]["domain"]
    assert "sessionid" not in data


@pytest.mark.parametrize("method", ["get", "post"])
def test_anonymous_operations_require_login(client, method):
    response = getattr(client, method)("/api/test-protected/")
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"
    assert response["Cache-Control"] == "no-store"


def test_invalid_password_has_clear_error_without_access(client):
    response = login(client, "wrong")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_password"
    assert response.json()["message"]
    assert discover(client)["authenticated"] is False


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"password": None},
        {"password": ""},
        {"password": 12},
        {"password": PASSWORD, "extra": 1},
    ],
)
def test_login_rejects_invalid_inputs(client, body):
    token = discover(client)["csrf_token"]
    response = client.post("/api/auth/login/", body, format="json", HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert discover(client)["authenticated"] is False


@pytest.mark.parametrize(
    "encoded",
    ["", "malformed", "pbkdf2_sha256$x$salt$bad", "pbkdf2_sha256$1$salt$bad", "argon2$bad"],
)
def test_unconfigured_password_never_allows_login(client, settings, encoded):
    settings.CRM_DEMO_PASSWORD_HASH = encoded
    response = login(client)
    assert response.status_code == 503
    assert response.json()["code"] == "configuration_error"


@pytest.mark.parametrize("route", ["login", "logout"])
@pytest.mark.parametrize("logged_in", [False, True])
def test_login_and_logout_always_require_csrf(client, route, logged_in):
    if logged_in:
        assert login(client).status_code == 200
    body = {"password": PASSWORD} if route == "login" else {}
    response = client.post(f"/api/auth/{route}/", body, format="json")
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert discover(client)["authenticated"] is logged_in


def test_login_rotates_session_and_csrf(client):
    session = client.session
    session["anonymous"] = True
    session.save()
    old_key = session.session_key
    old_token = discover(client)["csrf_token"]
    assert login(client).status_code == 200
    assert client.session.session_key != old_key
    response = client.post("/api/test-protected/", {}, HTTP_X_CSRFTOKEN=old_token)
    assert response.status_code == 403
    token = discover(client)["csrf_token"]
    assert client.post("/api/test-protected/", {}, HTTP_X_CSRFTOKEN=token).status_code == 200


def test_absolute_expiry_does_not_slide_and_repeated_login_does_not_extend(client):
    data = login(client).json()
    key = client.session.session_key
    expires = Session.objects.get(session_key=key).expire_date
    future = timezone.now() + timedelta(hours=47)
    with patch("django.utils.timezone.now", return_value=future):
        assert client.get("/api/test-protected/").status_code == 200
        assert discover(client)["expires_at"] == data["expires_at"]
        assert login(client).json()["expires_at"] == data["expires_at"]
    assert Session.objects.get(session_key=key).expire_date == expires
    assert client.session.session_key == key


def test_expiry_at_exact_boundary_requires_login(client):
    expiry = timezone.datetime.fromisoformat(login(client).json()["expires_at"])
    with patch("django.utils.timezone.now", return_value=expiry - timedelta(microseconds=1)):
        assert client.get("/api/test-protected/").status_code == 200
    with patch("django.utils.timezone.now", return_value=expiry):
        assert client.get("/api/test-protected/").status_code == 401
        assert discover(client)["authenticated"] is False


def test_logout_invalidates_shared_cookie_but_preserves_other_browser(client):
    assert login(client).status_code == 200
    tab = APIClient(enforce_csrf_checks=True)
    tab.cookies = client.cookies.copy()
    other = APIClient(enforce_csrf_checks=True)
    assert login(other).status_code == 200
    assert logout(client).status_code == 204
    assert tab.get("/api/test-protected/").status_code == 401
    assert other.get("/api/test-protected/").status_code == 200
    assert logout(client).status_code == 204


def test_password_change_revokes_all_previous_sessions(client, settings):
    other = APIClient(enforce_csrf_checks=True)
    assert login(client).status_code == login(other).status_code == 200
    settings.CRM_DEMO_PASSWORD_HASH = make_password("changed-password")
    assert client.get("/api/test-protected/").status_code == 401
    assert discover(other)["authenticated"] is False
    assert login(client).status_code == 401
    assert login(client, "changed-password").status_code == 200


def test_admin_login_is_separate_from_demo_access(client):
    user = get_user_model().objects.create_superuser(username="technical", password="admin-test")
    client.force_login(user)
    assert client.get("/api/test-protected/").status_code == 401
    assert discover(client)["authenticated"] is False
    assert login(client).status_code == 200
    assert client.get("/admin/").status_code == 302
    assert not client.session.get("_auth_user_id") == str(user.pk)


def test_demo_principal_has_no_usable_password_or_admin_permissions(client):
    assert login(client).status_code == 200
    user = get_user_model().objects.get(pk=client.session["_auth_user_id"])
    assert not user.has_usable_password()
    assert not user.is_staff and not user.is_superuser


def test_unexpected_api_failure_is_json_without_private_details(client, caplog):
    assert login(client).status_code == 200
    with patch.object(ProtectedView, "get", side_effect=RuntimeError("private test details")):
        response = client.get("/api/test-protected/")
    assert response.status_code == 500
    assert response.json() == {
        "code": "service_unavailable",
        "message": "Сервер временно недоступен.",
        "field_errors": {},
    }
    assert response["Cache-Control"] == "no-store"
    failure = next(record for record in caplog.records if record.name == "leadflow.crm.api.errors")
    assert failure.exc_info is not None
    assert failure.exc_info[2] is not None
    assert "RuntimeError" in failure.getMessage()
    assert "private test details" not in caplog.text


def test_unknown_api_route_uses_json_errors(client):
    response = client.get("/api/not-implemented/")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert response["Cache-Control"] == "no-store"


def test_csrf_rejects_untrusted_origin_even_with_valid_token(client):
    token = discover(client)["csrf_token"]
    response = client.post(
        "/api/auth/login/",
        {"password": PASSWORD},
        format="json",
        HTTP_X_CSRFTOKEN=token,
        HTTP_ORIGIN="https://untrusted.example",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert discover(client)["authenticated"] is False


def test_cleanup_preserves_active_sessions_and_recent_counters(client):
    assert login(client).status_code == 200
    active_key = client.session.session_key
    old = APIClient(enforce_csrf_checks=True)
    assert login(old).status_code == 200
    expired_key = old.session.session_key
    Session.objects.filter(pk=expired_key).update(expire_date=timezone.now() - timedelta(seconds=1))
    LoginAttempt.objects.create(
        source_key="expired-test-source", started_at=timezone.now() - timedelta(days=2)
    )
    call_command("cleanup_crm_auth")
    assert Session.objects.filter(pk=active_key).exists()
    assert not Session.objects.filter(pk=expired_key).exists()
    assert not LoginAttempt.objects.filter(pk="expired-test-source").exists()
    assert LoginAttempt.objects.count() == 2


def test_throttle_limits_attempts_and_recovers_after_one_minute(client):
    token = discover(client)["csrf_token"]
    start = timezone.now()
    with patch("django.utils.timezone.now", return_value=start):
        for _ in range(10):
            assert login(client, "wrong").status_code == 401
        response = login(client)
        assert response.status_code == 429
        assert response.json()["code"] == "rate_limited"
        assert int(response["Retry-After"]) == 60
        # An arbitrary forwarded address must not bypass the limit.
        assert (
            client.post(
                "/api/auth/login/",
                {"password": PASSWORD},
                format="json",
                HTTP_X_CSRFTOKEN=token,
                HTTP_X_FORWARDED_FOR="192.0.2.2",
            ).status_code
            == 429
        )
    with patch("django.utils.timezone.now", return_value=start + timedelta(seconds=60)):
        assert login(client).status_code == 200


@pytest.mark.django_db(transaction=True)
def test_throttle_is_atomic_across_workers():
    barrier = Barrier(12)
    encoded = make_password(PASSWORD)

    def attempt(_):
        close_old_connections()
        try:
            browser = APIClient(enforce_csrf_checks=True)
            token = discover(browser)["csrf_token"]
            barrier.wait(timeout=10)
            return browser.post(
                "/api/auth/login/", {"password": "wrong"}, format="json", HTTP_X_CSRFTOKEN=token
            ).status_code
        finally:
            close_old_connections()

    with (
        override_settings(CRM_DEMO_PASSWORD_HASH=encoded),
        ThreadPoolExecutor(max_workers=12) as pool,
    ):
        statuses = list(pool.map(attempt, range(12)))
    assert statuses.count(401) == 10
    assert statuses.count(429) == 2

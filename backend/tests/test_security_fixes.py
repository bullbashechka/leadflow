from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.management import call_command
from rest_framework.test import APIClient

from leadflow.crm.models import Lead
from leadflow.crm.services import create_lead
from leadflow.crm.validation import InputError
from leadflow.crm.validation import validate_contacts

pytestmark = pytest.mark.django_db
PASSWORD = "synthetic-security-test-password"


def signed_in(settings, *, username=None, address="198.51.100.1"):
    settings.CRM_DEMO_PASSWORD_HASH = make_password(PASSWORD)
    client = APIClient(enforce_csrf_checks=True)
    token = client.get("/api/auth/session/").json()["csrf_token"]
    body = {"password": PASSWORD}
    if username:
        body["username"] = username
    response = client.post(
        "/api/auth/login/", body, format="json", HTTP_X_CSRFTOKEN=token, REMOTE_ADDR=address
    )
    assert response.status_code == 200
    return client


@pytest.mark.parametrize("field", ["is_active", "is_staff", "is_superuser"])
def test_security_flag_round_trip_cannot_restore_an_offline_session(settings, field):
    settings.CRM_AUTH_MODE = "individual"
    user = get_user_model().objects.create_user(username="operator", password=PASSWORD)
    client = signed_in(settings, username=user.username)
    original = getattr(user, field)
    setattr(user, field, not original)
    user.save(update_fields=[field])
    setattr(user, field, original)
    user.save(update_fields=[field])
    assert client.get("/api/leads/").status_code == 401


def test_repeated_revocation_advances_session_revision(settings):
    settings.CRM_AUTH_MODE = "individual"
    user = get_user_model().objects.create_user(username="operator", password=PASSWORD)
    call_command("revoke_crm_operator", user.username)
    user.refresh_from_db()
    revision = user.auth_revision
    call_command("revoke_crm_operator", user.username)
    user.refresh_from_db()
    assert user.auth_revision > revision


def test_contacts_limit_is_shared_and_deduplication_does_not_spend_capacity():
    values = [f"user{index}@example.test" for index in range(20)]
    assert len(validate_contacts(values + [values[0]])) == 20
    with pytest.raises(InputError):
        validate_contacts(values + ["extra@example.test"])
    with pytest.raises(InputError):
        validate_contacts([values[0]] * 101)


def test_rejected_contacts_do_not_partially_create_a_lead():
    with pytest.raises(InputError):
        create_lead(
            uuid4(),
            {
                "name": "Synthetic",
                "request": "Test",
                "contacts": [f"user{i}@example.test" for i in range(21)],
            },
        )
    assert not Lead.objects.exists()


@pytest.mark.parametrize(
    "query", ["offset=" + str(2**80), "tag_id=" + str(2**80), "since_sequence=" + str(2**80)]
)
def test_huge_numeric_parameters_are_validation_errors(settings, query):
    client = signed_in(settings)
    response = client.get("/api/leads/?" + query)
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_oversized_json_is_a_safe_413_before_processing(settings):
    client = signed_in(settings)
    token = client.get("/api/auth/session/").json()["csrf_token"]
    response = client.post(
        "/api/leads/",
        '{"name":"' + "x" * (128 * 1024) + '"}',
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert response["Cache-Control"] == "no-store"
    assert not Lead.objects.exists()


def test_email_digits_do_not_match_unrelated_phone(settings):
    client = signed_in(settings)
    create_lead(uuid4(), {"name": "Synthetic", "request": "Test", "contacts": ["+77011234567"]})
    response = client.get("/api/leads/?q=missing1@example.test")
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_public_health_never_reads_the_database(django_assert_num_queries):
    with django_assert_num_queries(0):
        response = APIClient().get("/api/health/")
    assert response.status_code == 200


def test_readiness_is_not_public():
    assert APIClient().get("/api/readiness/").status_code == 401


def test_distributed_login_attempts_share_an_account_limit(settings):
    settings.CRM_AUTH_MODE = "individual"
    get_user_model().objects.create_user(username="operator", password=PASSWORD)
    for index in range(21):
        client = APIClient(enforce_csrf_checks=True)
        token = client.get("/api/auth/session/").json()["csrf_token"]
        response = client.post(
            "/api/auth/login/",
            {"username": "operator", "password": "wrong"},
            format="json",
            HTTP_X_CSRFTOKEN=token,
            REMOTE_ADDR=f"198.51.100.{index + 1}",
        )
        assert response.status_code == (401 if index < 20 else 429)
    assert 1 <= int(response["Retry-After"]) <= 60


@pytest.mark.parametrize("field", ["is_active", "is_staff", "is_superuser", "password"])
def test_queryset_security_round_trip_permanently_revokes_access(settings, field):
    settings.CRM_AUTH_MODE = "individual"
    user = get_user_model().objects.create_user(username="operator", password=PASSWORD)
    client = signed_in(settings, username=user.username)
    original = getattr(user, field)
    changed = make_password("changed") if field == "password" else not original
    get_user_model().objects.filter(pk=user.pk).update(**{field: changed})
    get_user_model().objects.filter(pk=user.pk).update(**{field: original})
    assert client.get("/api/leads/").status_code == 401


def test_stale_full_save_cannot_reset_a_revoked_session(settings):
    settings.CRM_AUTH_MODE = "individual"
    user = get_user_model().objects.create_user(username="operator", password=PASSWORD)
    client = signed_in(settings, username=user.username)
    get_user_model().objects.filter(pk=user.pk).update(is_active=False)
    get_user_model().objects.filter(pk=user.pk).update(is_active=True)
    user.name = "Updated from a stale instance"
    user.save()
    assert user.auth_revision == 3
    assert client.get("/api/leads/").status_code == 401


def test_distributed_accounts_share_the_global_login_quota(settings):
    settings.CRM_AUTH_MODE = "individual"
    for index in range(61):
        client = APIClient(enforce_csrf_checks=True)
        token = client.get("/api/auth/session/").json()["csrf_token"]
        response = client.post(
            "/api/auth/login/",
            {"username": f"synthetic-{index}", "password": "wrong"},
            format="json",
            HTTP_X_CSRFTOKEN=token,
            REMOTE_ADDR=f"198.51.100.{index + 1}",
        )
        assert response.status_code == (401 if index < 60 else 429)


@pytest.mark.parametrize("size, expected", [(128 * 1024, 200), (128 * 1024 + 1, 413)])
@pytest.mark.parametrize("chunked", [False, True])
def test_body_limit_counts_bytes_with_and_without_content_length(size, expected, chunked):
    from io import BytesIO

    from django.http import HttpResponse
    from django.test import RequestFactory

    from leadflow.crm.api.errors import APIBodyLimitMiddleware

    payload = ("я" * (size // 2)).encode() + b"x" * (size % 2)
    request = RequestFactory().post("/api/leads/", payload, content_type="application/json")
    if chunked:
        request.META.pop("CONTENT_LENGTH", None)
        request.META["wsgi.input_terminated"] = True
        request.META["wsgi.input"] = BytesIO(payload)

    def receive(req):
        assert req.body == payload
        return HttpResponse(status=200)

    response = APIBodyLimitMiddleware(receive)(request)
    assert response.status_code == expected


def test_runtime_database_timeouts_are_applied_to_real_postgres():
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SHOW statement_timeout")
        assert cursor.fetchone()[0] == "8s"
        cursor.execute("SHOW lock_timeout")
        assert cursor.fetchone()[0] == "2s"
    assert connection.settings_dict["OPTIONS"]["connect_timeout"] == 3


def test_postgres_statement_timeout_rolls_back_only_the_failed_transaction():
    from django.db import OperationalError
    from django.db import connection
    from django.db import transaction

    with pytest.raises(OperationalError, match="statement timeout"):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = 50")
                cursor.execute("SELECT pg_sleep(0.2)")
    assert Lead.objects.count() == 0

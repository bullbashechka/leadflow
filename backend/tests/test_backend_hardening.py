from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.models import QuerySet
from django.db.models.deletion import ProtectedError
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from leadflow.crm.models import CRMState
from leadflow.crm.models import Lead
from leadflow.crm.models import MutationReceipt
from leadflow.crm.models import SubmissionReceipt
from leadflow.crm.services import create_lead

pytestmark = pytest.mark.django_db
PASSWORD = "synthetic-manager-password"


@pytest.fixture(autouse=True)
def access_settings(settings):
    settings.CRM_AUTH_MODE = "demo"
    settings.CRM_DEMO_PASSWORD_HASH = make_password(PASSWORD)
    settings.TRUSTED_PROXY_CIDRS = []
    settings.CLIENT_IP_REQUIRE_VERIFIED_INGRESS = False
    settings.ADMIN_REQUIRE_NETWORK_ALLOWLIST = False


def login(client, username=None, **headers):
    csrf = client.get("/api/auth/session/", **headers).json()["csrf_token"]
    body = {"password": PASSWORD}
    if username is not None:
        body["username"] = username
    return client.post("/api/auth/login/", body, format="json", HTTP_X_CSRFTOKEN=csrf, **headers)


def payload(**changes):
    return {
        "name": "Synthetic client",
        "contacts": ["+77011234567", "alpha@example.com", "@another"],
        "request": "Website beta",
        **changes,
    }


def test_search_checks_separate_contacts_without_join_fanout():
    client = APIClient(enforce_csrf_checks=True)
    assert login(client).status_code == 200
    lead = create_lead(uuid4(), payload()).lead
    with CaptureQueriesContext(connection) as queries:
        response = client.get("/api/leads/", {"q": "alpha beta another"})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["results"]] == [str(lead.pk)]
    assert not any('JOIN "crm_leadcontact"' in query["sql"] for query in queries)


def test_first_delete_returns_original_uuid():
    client = APIClient(enforce_csrf_checks=True)
    assert login(client).status_code == 200
    lead = create_lead(uuid4(), payload()).lead
    lead_id = str(lead.pk)
    csrf = client.get("/api/auth/session/").json()["csrf_token"]
    body = {"operation_id": str(uuid4()), "expected_version": 1}
    first = client.delete(f"/api/leads/{lead_id}/", body, format="json", HTTP_X_CSRFTOKEN=csrf)
    replay = client.delete(f"/api/leads/{lead_id}/", body, format="json", HTTP_X_CSRFTOKEN=csrf)
    assert first.status_code == replay.status_code == 200
    assert first.json()["lead_id"] == replay.json()["lead_id"] == lead_id


def test_page_watermark_never_accepts_an_unseen_arrival():
    client = APIClient(enforce_csrf_checks=True)
    assert login(client).status_code == 200
    create_lead(uuid4(), payload())
    original_get = CRMState.objects.get
    inserted = []

    def concurrent_arrival(*args, **kwargs):
        inserted.append(create_lead(uuid4(), payload(name="Concurrent client")).lead)
        return original_get(*args, **kwargs)

    with patch.object(CRMState.objects, "get", side_effect=concurrent_arrival):
        response = client.get("/api/leads/")
    assert response.status_code == 200
    page = response.json()
    assert inserted
    assert page["latest_sequence"] == max(row["arrival_sequence"] for row in page["results"])
    assert page["count"] == len(page["results"])


def test_individual_login_has_personal_revocation_and_no_admin_access(settings):
    settings.CRM_AUTH_MODE = "individual"
    users = get_user_model()
    alice = users.objects.create_user(username="alice", password=PASSWORD)
    users.objects.create_user(username="bob", password=PASSWORD)
    first = APIClient(enforce_csrf_checks=True)
    second = APIClient(enforce_csrf_checks=True)
    assert first.get("/api/auth/session/").json()["auth_mode"] == "individual"
    assert login(first, "alice").status_code == 200
    assert login(second, "bob").status_code == 200
    assert first.get("/admin/").status_code == 302
    alice.set_password("changed-manager-password")
    alice.save(update_fields=["password"])
    assert first.get("/api/leads/").status_code == 401
    assert second.get("/api/leads/").status_code == 200


@pytest.mark.parametrize(
    "flags", [{"is_staff": True}, {"is_superuser": True}, {"is_active": False}]
)
def test_individual_login_denies_staff_superusers_and_inactive_users(settings, flags):
    settings.CRM_AUTH_MODE = "individual"
    user = get_user_model().objects.create_user(username="restricted", password=PASSWORD, **flags)
    client = APIClient(enforce_csrf_checks=True)
    assert login(client, user.username).status_code == 401
    assert client.get("/api/leads/").status_code == 401


def test_switching_auth_mode_invalidates_demo_access(settings):
    client = APIClient(enforce_csrf_checks=True)
    assert login(client).status_code == 200
    settings.CRM_AUTH_MODE = "individual"
    assert client.get("/api/leads/").status_code == 401
    assert client.get("/api/auth/session/").json()["authenticated"] is False


def test_trusted_proxy_separates_client_limits_and_ignores_spoofed_untrusted_headers(settings):
    settings.TRUSTED_PROXY_CIDRS = ["10.0.0.0/8"]
    client = APIClient(enforce_csrf_checks=True)
    for _ in range(10):
        browser = APIClient(enforce_csrf_checks=True)
        csrf = browser.get("/api/auth/session/").json()["csrf_token"]
        assert (
            browser.post(
                "/api/auth/login/",
                {"password": "wrong"},
                format="json",
                HTTP_X_CSRFTOKEN=csrf,
                REMOTE_ADDR="10.0.0.1",
                HTTP_X_FORWARDED_FOR="198.51.100.1, 10.0.0.2",
            ).status_code
            == 401
        )
    assert (
        login(client, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.2").status_code
        == 200
    )
    other = APIClient(enforce_csrf_checks=True)
    assert (
        login(
            other,
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="203.0.113.99, 198.51.100.1, 10.0.0.2",
        ).status_code
        == 429
    )


def test_admin_network_allowlist_is_closed_when_empty_and_rejects_spoofing(settings):
    settings.ADMIN_REQUIRE_NETWORK_ALLOWLIST = True
    settings.ADMIN_NETWORK_ALLOWLIST = []
    client = APIClient()
    assert client.get("/admin/").status_code == 403
    settings.ADMIN_NETWORK_ALLOWLIST = ["192.0.2.10/32"]
    assert (
        client.get(
            "/admin/", REMOTE_ADDR="192.0.2.11", HTTP_X_FORWARDED_FOR="192.0.2.10"
        ).status_code
        == 403
    )
    assert client.get("/admin/", REMOTE_ADDR="192.0.2.10").status_code == 302


def test_admin_login_throttle_is_independent_of_crm(settings):
    client = APIClient(enforce_csrf_checks=True)
    client.get("/admin/login/")
    csrf = client.cookies["csrftoken"].value
    for _ in range(10):
        assert (
            client.post(
                "/admin/login/",
                {"username": "unknown", "password": "wrong", "csrfmiddlewaretoken": csrf},
            ).status_code
            == 200
        )
    blocked = client.post(
        "/admin/login/", {"username": "unknown", "password": "wrong", "csrfmiddlewaretoken": csrf}
    )
    assert blocked.status_code == 429
    assert int(blocked["Retry-After"]) > 0
    assert login(APIClient(enforce_csrf_checks=True)).status_code == 200


def test_api_mutations_record_actor_and_cross_user_replay_is_rejected(settings):
    settings.CRM_AUTH_MODE = "individual"
    alice = get_user_model().objects.create_user(username="alice", password=PASSWORD)
    get_user_model().objects.create_user(username="bob", password=PASSWORD)
    first = APIClient(enforce_csrf_checks=True)
    second = APIClient(enforce_csrf_checks=True)
    assert login(first, "alice").status_code == login(second, "bob").status_code == 200
    submission_id = str(uuid4())
    body = payload(submission_id=submission_id)
    csrf = first.get("/api/auth/session/").json()["csrf_token"]
    created = first.post("/api/leads/", body, format="json", HTTP_X_CSRFTOKEN=csrf)
    assert created.status_code == 201
    lead_id = created.json()["id"]
    lead = Lead.objects.get(pk=lead_id)
    assert lead.created_by_id == alice.pk
    receipt = SubmissionReceipt.objects.get(pk=submission_id)
    assert receipt.actor_id == alice.pk
    bob_csrf = second.get("/api/auth/session/").json()["csrf_token"]
    replay = second.post("/api/leads/", body, format="json", HTTP_X_CSRFTOKEN=bob_csrf)
    assert replay.status_code == 403
    assert "id" not in replay.json()
    operation_id = str(uuid4())
    change = {"operation_id": operation_id, "expected_version": 1, "status": "closed"}
    changed = first.patch(
        f"/api/leads/{lead_id}/status/", change, format="json", HTTP_X_CSRFTOKEN=csrf
    )
    assert changed.status_code == 200
    assert MutationReceipt.objects.get(pk=operation_id).actor_id == alice.pk
    lead.refresh_from_db()
    assert lead.updated_by_id == alice.pk
    conflicting = second.patch(
        f"/api/leads/{lead_id}/status/", change, format="json", HTTP_X_CSRFTOKEN=bob_csrf
    )
    assert conflicting.status_code == 409
    assert "lead" not in conflicting.json()


def test_arrival_after_watermark_is_deferred_to_next_poll():
    client = APIClient(enforce_csrf_checks=True)
    assert login(client).status_code == 200
    first = create_lead(uuid4(), payload()).lead
    original_count = QuerySet.count
    inserted = []

    def concurrent_count(query):
        if query.model is Lead and not inserted:
            inserted.append(create_lead(uuid4(), payload(name="Later client")).lead)
        return original_count(query)

    with patch.object(QuerySet, "count", concurrent_count):
        response = client.get("/api/leads/")
    assert response.status_code == 200
    page = response.json()
    assert page["latest_sequence"] == first.arrival_sequence
    assert page["count"] == 1
    assert [row["id"] for row in page["results"]] == [str(first.pk)]
    next_poll = client.get("/api/leads/", {"since_sequence": page["latest_sequence"]}).json()
    assert next_poll["new_count"] == 1
    assert next_poll["results"][0]["id"] == str(inserted[0].pk)


@pytest.mark.parametrize("flag", ["is_staff", "is_superuser", "is_active"])
def test_existing_individual_session_is_revoked_when_permissions_change(settings, flag):
    settings.CRM_AUTH_MODE = "individual"
    user = get_user_model().objects.create_user(username="manager", password=PASSWORD)
    client = APIClient(enforce_csrf_checks=True)
    assert login(client, user.username).status_code == 200
    setattr(user, flag, flag != "is_active")
    user.save(update_fields=[flag])
    assert client.get("/api/leads/").status_code == 401


def test_untrusted_forwarding_header_cannot_reset_crm_limit():
    client = APIClient(enforce_csrf_checks=True)
    csrf = client.get("/api/auth/session/").json()["csrf_token"]
    for _ in range(10):
        assert (
            client.post(
                "/api/auth/login/",
                {"password": "wrong"},
                format="json",
                HTTP_X_CSRFTOKEN=csrf,
                REMOTE_ADDR="198.51.100.1",
                HTTP_X_FORWARDED_FOR="192.0.2.10",
            ).status_code
            == 401
        )
    assert (
        login(client, REMOTE_ADDR="198.51.100.1", HTTP_X_FORWARDED_FOR="192.0.2.11").status_code
        == 429
    )


def test_admin_invalid_csrf_does_not_consume_login_budget():
    client = APIClient(enforce_csrf_checks=True)
    for _ in range(11):
        assert (
            client.post("/admin/login/", {"username": "unknown", "password": "wrong"}).status_code
            == 403
        )
    client.get("/admin/login/")
    csrf = client.cookies["csrftoken"].value
    assert (
        client.post(
            "/admin/login/",
            {"username": "unknown", "password": "wrong", "csrfmiddlewaretoken": csrf},
        ).status_code
        == 200
    )


def test_admin_authenticated_staff_cannot_bypass_network_policy(settings, admin_user):
    settings.ADMIN_REQUIRE_NETWORK_ALLOWLIST = True
    settings.ADMIN_NETWORK_ALLOWLIST = []
    client = APIClient()
    client.force_login(admin_user)
    assert client.get("/admin/").status_code == 403


def test_admin_trusted_proxy_requires_verified_client_identity_in_production(settings):
    settings.ADMIN_REQUIRE_NETWORK_ALLOWLIST = True
    settings.ADMIN_NETWORK_ALLOWLIST = ["10.0.0.0/8", "192.0.2.10/32"]
    settings.TRUSTED_PROXY_CIDRS = ["10.0.0.0/8"]
    settings.CLIENT_IP_REQUIRE_VERIFIED_INGRESS = True
    client = APIClient()
    assert (
        client.get("/admin/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="192.0.2.10").status_code
        == 403
    )
    assert (
        client.get("/admin/", REMOTE_ADDR="10.0.0.1", LEADFLOW_CLIENT_IP="192.0.2.10").status_code
        == 302
    )


def test_actor_receipts_survive_deletion_and_replays_preserve_original_actor(settings):
    settings.CRM_AUTH_MODE = "individual"
    alice = get_user_model().objects.create_user(username="alice", password=PASSWORD)
    client = APIClient(enforce_csrf_checks=True)
    assert login(client, "alice").status_code == 200
    csrf = client.get("/api/auth/session/").json()["csrf_token"]
    submission_id = str(uuid4())
    created = client.post(
        "/api/leads/", payload(submission_id=submission_id), format="json", HTTP_X_CSRFTOKEN=csrf
    )
    lead_id = created.json()["id"]
    delete_id = str(uuid4())
    body = {"operation_id": delete_id, "expected_version": 1}
    first = client.delete(f"/api/leads/{lead_id}/", body, format="json", HTTP_X_CSRFTOKEN=csrf)
    retry = client.delete(f"/api/leads/{lead_id}/", body, format="json", HTTP_X_CSRFTOKEN=csrf)
    assert first.status_code == retry.status_code == 200
    receipt = SubmissionReceipt.objects.get(pk=submission_id)
    assert receipt.actor_id == alice.pk and receipt.payload == {}
    assert str(receipt.deleted_lead_id) == lead_id
    assert MutationReceipt.objects.get(pk=delete_id).actor_id == alice.pk
    with pytest.raises(ProtectedError):
        alice.delete()


def test_legacy_submission_replay_does_not_invent_an_actor():
    actor = get_user_model().objects.create_user(username="manager", password=PASSWORD)
    submission_id = uuid4()
    original = create_lead(submission_id, payload())
    replay = create_lead(submission_id, payload(), actor=actor)
    assert replay.replayed and replay.lead.pk == original.lead.pk
    assert SubmissionReceipt.objects.get(pk=submission_id).actor_id is None
    assert Lead.objects.get(pk=original.lead.pk).created_by_id is None

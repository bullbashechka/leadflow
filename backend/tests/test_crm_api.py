from uuid import UUID
from uuid import uuid4

import pytest
from django.contrib.auth.hashers import make_password
from django.utils import timezone
from rest_framework.test import APIClient

from leadflow.crm.models import Lead
from leadflow.crm.models import Tag
from leadflow.crm.services import create_lead

pytestmark = pytest.mark.django_db

PASSWORD = "test-demo-password"


@pytest.fixture
def crm_client(settings):
    settings.CRM_DEMO_PASSWORD_HASH = make_password(PASSWORD)
    return APIClient(enforce_csrf_checks=True)


def log_in(client):
    discovery = client.get("/api/auth/session/")
    assert discovery.status_code == 200
    return client.post(
        "/api/auth/login/",
        {"password": PASSWORD},
        format="json",
        HTTP_X_CSRFTOKEN=discovery.json()["csrf_token"],
    )


def lead_payload(**changes):
    return {
        "name": "Клиент",
        "contacts": ["+7 (701) 123-45-67", "@alexander"],
        "request": "Нужен сайт агентства",
        **changes,
    }


def post_lead(client, payload):
    csrf = client.get("/api/auth/session/").json()["csrf_token"]
    return client.post(
        "/api/leads/",
        payload,
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )


def test_lead_and_tag_endpoints_require_the_crm_session():
    client = APIClient()
    assert client.get("/api/tags/").json()["code"] == "authentication_required"
    assert client.get("/api/leads/").status_code == 401
    assert client.post("/api/leads/", {}, format="json").status_code == 401


def test_tags_list_uses_stable_contract(crm_client):
    assert log_in(crm_client).status_code == 200

    response = crm_client.get("/api/tags/")

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response.json() == {
        "results": [
            {"id": tag.pk, "name": tag.name, "is_system": tag.is_system}
            for tag in Tag.objects.order_by("pk")
        ]
    }


def test_lead_list_filters_orders_and_paginates_from_a_stable_anchor(crm_client):
    assert log_in(crm_client).status_code == 200
    website = Tag.objects.get(code="website")
    advertising = Tag.objects.get(code="advertising")
    first = create_lead(
        UUID("00000000-0000-0000-0000-000000000001"),
        lead_payload(tag_ids=[website.pk]),
    ).lead
    second = create_lead(
        UUID("00000000-0000-0000-0000-000000000002"),
        lead_payload(name="Два", tag_ids=[website.pk, advertising.pk]),
    ).lead
    third = create_lead(
        UUID("00000000-0000-0000-0000-000000000003"),
        lead_payload(name="Три", tag_ids=[advertising.pk]),
    ).lead
    moment = timezone.now()
    Lead.objects.filter(pk__in=[first.pk, second.pk, third.pk]).update(created_at=moment)

    page = crm_client.get(f"/api/leads/?tag_id={website.pk}&limit=1").json()
    next_page = crm_client.get(
        f"/api/leads/?tag_id={website.pk}&limit=1&before_id={page['results'][0]['id']}"
    ).json()
    empty = crm_client.get(
        f"/api/leads/?tag_id={website.pk}&limit=1&before_id={next_page['results'][0]['id']}"
    ).json()

    assert page["count"] == 2
    assert page["results"][0]["id"] == str(max([first.pk, second.pk]))
    assert page["results"][0]["created_at"].endswith(("+00:00", "Z"))
    assert page["next"] is not None
    assert page["results"][0]["contacts"] == [
        {"type": "phone", "value": "+7 (701) 123-45-67"},
        {"type": "telegram", "value": "@alexander"},
    ]
    expected_first = max([first, second], key=lambda lead: lead.pk.int)
    assert page["results"][0]["id"] == str(expected_first.pk)
    assert {tag["id"] for tag in page["results"][0]["tags"]} == set(
        expected_first.tags.values_list("pk", flat=True)
    )
    assert [item["id"] for item in next_page["results"]] == [str(min(first.pk, second.pk))]
    assert next_page["count"] == 2
    assert next_page["next"] is None
    assert empty["results"] == []
    assert empty["count"] == 2
    multi_tag_card = crm_client.get(f"/api/leads/{second.pk}/").json()
    assert {tag["id"] for tag in multi_tag_card["tags"]} == {
        website.pk,
        advertising.pk,
    }


@pytest.mark.parametrize(
    "query",
    [
        "tag_id=bad",
        "tag_id=999999",
        "tag_id=1&tag_id=2",
        "limit=0",
        "limit=101",
        "offset=-1",
        "before_id=not-a-uuid",
        "offset=1&before_id=00000000-0000-0000-0000-000000000001",
        "other=1",
    ],
)
def test_list_rejects_invalid_or_ambiguous_query_parameters(crm_client, query):
    assert log_in(crm_client).status_code == 200
    response = crm_client.get(f"/api/leads/?{query}")
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_unknown_lead_is_not_found(crm_client):
    assert log_in(crm_client).status_code == 200
    response = crm_client.get(f"/api/leads/{uuid4()}/")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_create_returns_manual_lead_and_exact_replay(crm_client):
    assert log_in(crm_client).status_code == 200
    payload = lead_payload(tag_ids=[Tag.objects.get(code="website").pk])
    payload["submission_id"] = str(uuid4())

    created = post_lead(crm_client, payload)
    replay = post_lead(crm_client, payload)

    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]
    assert created.json()["source"] == "manual"
    assert created.json()["status"] == "new"
    assert created.json()["name"] == payload["name"]
    assert created.json()["request"] == payload["request"]
    assert created.json()["contacts"][0]["value"] == payload["contacts"][0]
    assert len(created.json()["tags"]) == 1
    assert Lead.objects.count() == 1

    card = crm_client.get(f"/api/leads/{created.json()['id']}/")
    assert card.status_code == 200
    assert card.json() == created.json()


def test_create_conflict_and_validation_do_not_add_or_change_leads(crm_client):
    assert log_in(crm_client).status_code == 200
    submission_id = str(uuid4())
    payload = lead_payload() | {"submission_id": submission_id, "source": "telegram_bot"}
    rejected = post_lead(crm_client, payload)
    assert rejected.status_code == 400
    assert "source" in rejected.json()["field_errors"]

    payload.pop("source")
    payload["contacts"] = ["+7 701 123 45 67", "", "invalid"]
    invalid = post_lead(crm_client, payload)
    assert invalid.status_code == 400
    assert "contacts.1" in invalid.json()["field_errors"]
    assert "contacts.2" in invalid.json()["field_errors"]
    assert Lead.objects.count() == 0

    payload["contacts"] = ["+7 701 123 45 67"]
    created = post_lead(crm_client, payload)
    changed = post_lead(crm_client, payload | {"name": "Иное имя"})
    assert created.status_code == 201
    assert changed.status_code == 409
    assert changed.json()["code"] == "submission_conflict"
    assert Lead.objects.get().name == "Клиент"


def test_create_requires_csrf_and_rejects_non_object_bodies(crm_client):
    assert log_in(crm_client).status_code == 200
    token = crm_client.cookies["csrftoken"].value
    missing_csrf = APIClient(enforce_csrf_checks=True)
    missing_csrf.cookies.update(crm_client.cookies)
    assert missing_csrf.post("/api/leads/", {}, format="json").status_code == 403

    response = crm_client.post(
        "/api/leads/",
        ["unexpected"],
        format="json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"

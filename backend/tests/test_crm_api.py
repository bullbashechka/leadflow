from uuid import UUID
from uuid import uuid4

import pytest
from django.contrib.auth.hashers import make_password
from django.utils import timezone
from rest_framework.test import APIClient

from leadflow.crm.models import Lead
from leadflow.crm.models import LeadContact
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
            {"id": tag.pk, "name": tag.name, "is_system": tag.is_system, "lead_count": 0}
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
    assert page["results"][0]["id"] == str(second.pk)
    assert page["results"][0]["created_at"].endswith(("+00:00", "Z"))
    assert page["next"] is not None
    assert page["results"][0]["contacts"] == [
        {"type": "phone", "value": "+7 (701) 123-45-67"},
        {"type": "telegram", "value": "@alexander"},
    ]
    expected_first = second
    assert page["results"][0]["id"] == str(expected_first.pk)
    assert {tag["id"] for tag in page["results"][0]["tags"]} == set(
        expected_first.tags.values_list("pk", flat=True)
    )
    assert [item["id"] for item in next_page["results"]] == [str(first.pk)]
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


def mutate_lead(client, lead_id, values):
    csrf = client.get("/api/auth/session/").json()["csrf_token"]
    response = client.put(
        f"/api/leads/{lead_id}/",
        values,
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    return response


def test_lead_edit_saves_note_and_rejects_a_stale_version_with_current_values(crm_client):
    assert log_in(crm_client).status_code == 200
    created = post_lead(
        crm_client,
        lead_payload(submission_id=str(uuid4()), tag_ids=[Tag.objects.get(code="website").pk]),
    )
    lead_id = created.json()["id"]
    detail = crm_client.get(f"/api/leads/{lead_id}/").json()

    assert detail["version"] == 1
    assert detail["note"] == ""
    assert detail["is_demo"] is False

    values = {
        "operation_id": str(uuid4()),
        "expected_version": 1,
        "name": "Исправленное имя",
        "contacts": ["+7 (701) 765-43-21", "editor@example.com"],
        "request": "Новый запрос",
        "note": "Позвонить после обеда",
        "tag_ids": [detail["tags"][0]["id"]],
    }
    saved = mutate_lead(crm_client, lead_id, values)

    assert saved.status_code == 200
    result = saved.json()
    assert result["applied_version"] == 2
    assert result["replayed"] is False
    assert result["lead"]["version"] == 2
    assert result["lead"]["source"] == "manual"
    assert result["lead"]["created_at"] == detail["created_at"]
    assert result["lead"]["name"] == "Исправленное имя"
    assert result["lead"]["note"] == "Позвонить после обеда"
    assert [item["value"] for item in result["lead"]["contacts"]] == values["contacts"]

    stale = mutate_lead(
        crm_client,
        lead_id,
        values | {"operation_id": str(uuid4()), "name": "Старая правка"},
    )

    assert stale.status_code == 409
    assert stale.json()["code"] == "version_conflict"
    assert stale.json()["current_lead"]["name"] == "Исправленное имя"
    assert Lead.objects.get(pk=lead_id).name == "Исправленное имя"


def test_status_change_is_a_separate_versioned_idempotent_operation(crm_client):
    assert log_in(crm_client).status_code == 200
    created = post_lead(crm_client, lead_payload(submission_id=str(uuid4())))
    lead_id = created.json()["id"]
    csrf = crm_client.get("/api/auth/session/").json()["csrf_token"]
    operation_id = str(uuid4())

    def change_status(status):
        return crm_client.patch(
            f"/api/leads/{lead_id}/status/",
            {"operation_id": operation_id, "expected_version": 1, "status": status},
            format="json",
            HTTP_X_CSRFTOKEN=csrf,
        )

    first = change_status("in_progress")
    replay = change_status("in_progress")
    conflicting_replay = change_status("closed")

    assert first.status_code == 200
    assert first.json()["lead"]["status"] == "in_progress"
    assert first.json()["lead"]["version"] == 2
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["lead"]["status"] == "in_progress"
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json()["code"] == "operation_conflict"
    assert Lead.objects.get(pk=lead_id).status == "in_progress"


def test_lead_edit_note_limit_preserves_stored_data(crm_client):
    assert log_in(crm_client).status_code == 200
    created = post_lead(crm_client, lead_payload(submission_id=str(uuid4())))
    lead_id = created.json()["id"]
    invalid = mutate_lead(
        crm_client,
        lead_id,
        {
            "operation_id": str(uuid4()),
            "expected_version": 1,
            "name": "Несохранённое имя",
            "contacts": ["@alexander"],
            "request": "Новый запрос",
            "note": "н" * 5001,
            "tag_ids": [],
        },
    )

    assert invalid.status_code == 400
    assert "note" in invalid.json()["field_errors"]
    stored = Lead.objects.get(pk=lead_id)
    assert stored.name == "Клиент"
    assert stored.request == "Нужен сайт агентства"


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


def test_search_matches_every_term_across_fields_and_combines_tag_and_status(crm_client):
    assert log_in(crm_client).status_code == 200
    website = Tag.objects.get(code="website")
    advertising = Tag.objects.get(code="advertising")
    first = create_lead(
        UUID("00000000-0000-0000-0000-000000000011"),
        lead_payload(
            name="Алиса",
            contacts=["+7 (701) 123-45-67"],
            request="Нужен каталог",
            tag_ids=[website.pk],
        ),
    ).lead
    first.status = Lead.Status.IN_PROGRESS
    first.save(update_fields=["status"])
    create_lead(
        UUID("00000000-0000-0000-0000-000000000012"),
        lead_payload(
            name="Боб", contacts=["+7 (777) 000-00-00"], request="Алиса ищет", tag_ids=[website.pk]
        ),
    )
    create_lead(
        UUID("00000000-0000-0000-0000-000000000013"),
        lead_payload(name="Алиса", request="Каталог", tag_ids=[advertising.pk]),
    )

    response = crm_client.get(
        f"/api/leads/?q=алиса%20каталог&tag_id={website.pk}&status=in_progress"
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["results"]] == [str(first.pk)]


def test_phone_search_ignores_formatting_and_cursor_survives_anchor_deletion(crm_client):
    assert log_in(crm_client).status_code == 200
    leads = [
        create_lead(
            UUID(int=value),
            lead_payload(
                name=f"Лид {value}",
                contacts=["+7 (701) 123-45-67"] if value == 22 else [f"lead{value}@example.com"],
            ),
        ).lead
        for value in range(21, 24)
    ]
    LeadContact.objects.filter(lead=leads[1], type="phone").update(
        value="+7 (701) 123-45-67", key="+77011234567"
    )
    initial = crm_client.get("/api/leads/?limit=1").json()
    sequence = initial["results"][0]["arrival_sequence"]
    lead = leads[1]
    search = crm_client.get("/api/leads/?q=7011234567").json()
    lead_id = initial["results"][0]["id"]
    crm_client.delete(
        f"/api/leads/{lead_id}/",
        {"operation_id": str(uuid4()), "expected_version": 1},
        format="json",
        HTTP_X_CSRFTOKEN=crm_client.get("/api/auth/session/").json()["csrf_token"],
    )
    next_page = crm_client.get(f"/api/leads/?limit=1&before_sequence={sequence}")

    assert search["count"] == 1
    assert search["results"][0]["id"] == str(lead.pk)
    assert next_page.status_code == 200
    assert all(item["id"] != lead_id for item in next_page.json()["results"])


def test_tag_create_normalizes_and_duplicate_returns_existing_tag(crm_client):
    assert log_in(crm_client).status_code == 200
    csrf = crm_client.get("/api/auth/session/").json()["csrf_token"]

    created = crm_client.post(
        "/api/tags/",
        {"operation_id": str(uuid4()), "name": "  Web Design  "},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    duplicate = crm_client.post(
        "/api/tags/",
        {"operation_id": str(uuid4()), "name": "web design"},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )

    assert created.status_code == 201
    assert created.json()["tag"]["name"] == "Web Design"
    assert created.json()["created"] is True
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    assert duplicate.json()["tag"]["id"] == created.json()["tag"]["id"]
    assert Tag.objects.filter(name__iexact="web design").count() == 1


def test_duplicate_tag_create_replay_reserves_its_operation_id(crm_client):
    assert log_in(crm_client).status_code == 200
    existing = Tag.objects.create(name="Existing")
    csrf = crm_client.get("/api/auth/session/").json()["csrf_token"]
    operation_id = str(uuid4())

    duplicate = crm_client.post(
        "/api/tags/",
        {"operation_id": operation_id, "name": " existing "},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    replay = crm_client.post(
        "/api/tags/",
        {"operation_id": operation_id, "name": "existing"},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    conflicting_reuse = crm_client.post(
        "/api/tags/",
        {"operation_id": operation_id, "name": "Different"},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )

    assert duplicate.status_code == 200
    assert duplicate.json()["tag"]["id"] == existing.pk
    assert duplicate.json()["created"] is False
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["replayed"] is True
    assert conflicting_reuse.status_code == 409
    assert conflicting_reuse.json()["code"] == "operation_conflict"


def test_tag_delete_is_idempotent_removes_assignments_and_protects_system_tags(crm_client):
    assert log_in(crm_client).status_code == 200
    tag = Tag.objects.create(name="Seasonal")
    first = create_lead(UUID(int=31), lead_payload(tag_ids=[tag.pk])).lead
    second = create_lead(UUID(int=32), lead_payload(tag_ids=[tag.pk])).lead
    first_version = first.version
    second_version = second.version
    csrf = crm_client.get("/api/auth/session/").json()["csrf_token"]
    operation_id = str(uuid4())

    deleted = crm_client.delete(
        f"/api/tags/{tag.pk}/", {"operation_id": operation_id}, format="json", HTTP_X_CSRFTOKEN=csrf
    )
    replay = crm_client.delete(
        f"/api/tags/{tag.pk}/", {"operation_id": operation_id}, format="json", HTTP_X_CSRFTOKEN=csrf
    )
    protected = crm_client.delete(
        f"/api/tags/{Tag.objects.get(code='website').pk}/",
        {"operation_id": str(uuid4())},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )

    assert deleted.status_code == 200
    assert deleted.json()["affected_leads"] == 2
    assert replay.json()["replayed"] is True
    assert not Tag.objects.filter(pk=tag.pk).exists()
    assert first.tags.count() == 0 and second.tags.count() == 0
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.version == first_version + 1
    assert second.version == second_version + 1
    assert Lead.objects.count() == 2
    assert protected.status_code == 409


def test_lead_delete_tombstone_prevents_replay_from_restoring_the_lead(crm_client):
    assert log_in(crm_client).status_code == 200
    payload = lead_payload(submission_id=str(uuid4()), tag_ids=[Tag.objects.get(code="website").pk])
    created = post_lead(crm_client, payload)
    lead_id = created.json()["id"]
    csrf = crm_client.get("/api/auth/session/").json()["csrf_token"]
    operation_id = str(uuid4())

    deleted = crm_client.delete(
        f"/api/leads/{lead_id}/",
        {"operation_id": operation_id, "expected_version": 1},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    replay = crm_client.delete(
        f"/api/leads/{lead_id}/",
        {"operation_id": operation_id, "expected_version": 1},
        format="json",
        HTTP_X_CSRFTOKEN=csrf,
    )
    submission_replay = post_lead(crm_client, payload)

    assert deleted.status_code == 200
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert submission_replay.status_code == 410
    assert Lead.objects.count() == 0
    assert Tag.objects.filter(code="website").exists()

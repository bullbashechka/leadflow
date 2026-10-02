from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import close_old_connections
from django.db import transaction

from leadflow.crm.models import SYSTEM_TAGS
from leadflow.crm.models import Lead
from leadflow.crm.models import LeadContact
from leadflow.crm.models import SubmissionReceipt
from leadflow.crm.models import Tag
from leadflow.crm.services import SubmissionConflict
from leadflow.crm.services import SubmissionForbidden
from leadflow.crm.services import create_lead
from leadflow.crm.services import delete_tag
from leadflow.crm.validation import InputError


@pytest.fixture
def payload():
    return {
        "name": "Тестовый клиент",
        "contacts": ["+7 701 123-45-67", "@alexander"],
        "request": "Нужен сайт",
    }


@pytest.fixture
def tags(db):
    return [
        Tag.objects.get_or_create(code=code, defaults={"name": name})[0]
        for code, name in SYSTEM_TAGS.items()
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["name", "request"])
def test_null_character_returns_field_error_without_saving(payload, field):
    submission_id = uuid4()
    original = payload[field]
    payload[field] = "x\x00y"
    with pytest.raises(InputError) as error:
        create_lead(submission_id, payload)
    assert field in error.value.field_errors
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0
    payload[field] = original
    assert create_lead(submission_id, payload).lead.pk is not None


@pytest.mark.django_db
def test_initial_migration_has_system_tags():
    assert dict(Tag.objects.values_list("code", "name")) == SYSTEM_TAGS


@pytest.mark.django_db
def test_creation_persists_contacts_and_server_owned_fields(payload, tags):
    payload["tag_ids"] = [tags[0].pk, tags[2].pk]
    payload["contacts"].append("+77011234567")
    result = create_lead(uuid4(), payload)
    assert result is not None
    result.lead.refresh_from_db()
    assert not result.replayed
    assert result.lead.name == payload["name"]
    assert result.lead.request == payload["request"]
    assert result.lead.status == "new"
    assert result.lead.source == "manual"
    assert result.lead.created_at.utcoffset().total_seconds() == 0
    assert list(result.lead.contacts.values_list("value", flat=True)) == payload["contacts"][:2]
    assert set(result.lead.tags.values_list("pk", flat=True)) == set(payload["tag_ids"])
    assert result.lead.receipt.payload["contacts"] == payload["contacts"]


@pytest.mark.django_db
def test_identical_retry_returns_original_lead_without_tags(payload):
    submission_id = uuid4()
    first = create_lead(submission_id, payload)
    assert first is not None
    second = create_lead(submission_id, payload)
    assert second.replayed and second.lead.pk == first.lead.pk
    assert second.lead.created_at == first.lead.created_at
    assert not first.lead.tags.exists()
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1


@pytest.mark.django_db
def test_new_submission_with_same_contacts_is_separate(payload):
    first = create_lead(uuid4(), payload)
    second = create_lead(uuid4(), payload)
    assert first is not None and second is not None
    assert first.lead.pk != second.lead.pk
    assert Lead.objects.count() == 2


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change",
    [
        {"name": "Другой"},
        {"contacts": ["@alexander", "+7 701 123-45-67"]},
        {"request": "Другой запрос"},
        {"contacts": ["+77011234567", "@alexander"]},
    ],
)
def test_uuid_conflict_does_not_modify_lead(payload, change):
    submission_id = uuid4()
    original = create_lead(submission_id, payload)
    changed = deepcopy(payload) | change
    with pytest.raises(SubmissionConflict):
        create_lead(submission_id, changed)
    original.lead.refresh_from_db()
    assert original.lead.name == payload["name"]
    assert Lead.objects.count() == 1


@pytest.mark.django_db
def test_replay_compares_tags_as_a_set(payload, tags):
    submission_id = uuid4()
    payload["tag_ids"] = [tags[0].pk, tags[1].pk]
    first = create_lead(submission_id, payload)
    assert first is not None
    payload["tag_ids"].reverse()
    assert create_lead(submission_id, payload).lead.pk == first.lead.pk


@pytest.mark.django_db
def test_replay_checks_receipt_before_tag_availability(payload):
    tag = Tag.objects.create(name="Временный")
    payload["tag_ids"] = [tag.pk]
    submission_id = uuid4()
    first = create_lead(submission_id, payload)
    delete_tag(tag.pk)
    assert not Tag.objects.filter(pk=tag.pk).exists()
    assert create_lead(submission_id, payload).lead.pk == first.lead.pk


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change,field",
    [
        ({"name": " "}, "name"),
        ({"request": "x" * 2001}, "request"),
        ({"contacts": ["@alexander", "broken"]}, "contacts.1"),
        ({"contacts": []}, "contacts"),
        ({"tag_ids": [999999]}, "tag_ids"),
        ({"tag_ids": [True]}, "tag_ids"),
        ({"tag_ids": [1, 1]}, "tag_ids"),
        ({"tag_ids": "1"}, "tag_ids"),
        ({"source": "telegram_bot"}, "source"),
        ({"status": "closed"}, "status"),
        ({"contact": "@alexander"}, "contact"),
    ],
)
def test_invalid_create_has_no_partial_records(payload, change, field):
    with pytest.raises(InputError) as error:
        create_lead(uuid4(), payload | change)
    assert field in error.value.field_errors
    assert (
        Lead.objects.count()
        == LeadContact.objects.count()
        == SubmissionReceipt.objects.count()
        == 0
    )


@pytest.mark.django_db
def test_invalid_submission_id_is_a_field_error(payload):
    with pytest.raises(InputError) as error:
        create_lead("not-a-uuid", payload)
    assert "submission_id" in error.value.field_errors


@pytest.mark.django_db(transaction=True)
def test_failure_rolls_back_lead_contacts_and_receipt_and_allows_retry(payload):
    submission_id = uuid4()
    with patch("leadflow.crm.services.SubmissionReceipt.objects.create", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            create_lead(submission_id, payload)
    assert (
        Lead.objects.count()
        == LeadContact.objects.count()
        == SubmissionReceipt.objects.count()
        == 0
    )
    assert create_lead(submission_id, payload) is not None
    assert Lead.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_uuid_returns_one_committed_lead(payload):
    barrier = Barrier(2)
    submission_id = uuid4()

    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return create_lead(submission_id, payload)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert all(result is not None for result in results)
    assert results[0].lead.pk == results[1].lead.pk
    assert sorted(result.replayed for result in results) == [False, True]
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1


@pytest.mark.django_db
def test_receipt_rejects_wrong_channel_without_result(payload):
    submission_id = uuid4()
    create_lead(submission_id, payload)
    with pytest.raises(SubmissionForbidden):
        create_lead(submission_id, bot_user_id=42, draft_revision=0)


@pytest.mark.django_db
def test_system_tag_cannot_be_deleted_directly_or_in_bulk(tags):
    with pytest.raises(ValidationError), transaction.atomic():
        tags[0].delete()
    with pytest.raises(ValidationError), transaction.atomic():
        Tag.objects.filter(pk=tags[0].pk).delete()
    assert Tag.objects.filter(pk=tags[0].pk).exists()


@pytest.mark.django_db
def test_system_tag_cannot_be_deleted_by_service_or_admin(tags):
    with pytest.raises(ValidationError):
        delete_tag(tags[0].pk)
    assert not admin.site._registry[Tag].has_delete_permission(None, tags[0])
    assert "code" in admin.site._registry[Tag].get_readonly_fields(None, tags[0])


@pytest.mark.django_db
def test_case_insensitive_tag_unique_constraint():
    Tag.objects.create(name="Дополнительный")
    with pytest.raises(IntegrityError), transaction.atomic():
        Tag.objects.create(name="дополнительный")


@pytest.mark.django_db
def test_valid_unicode_email_is_saved_when_duplicate_key_expands(payload):
    value = "a" * 64 + "@" + ".".join(["ß" * 30] * 4) + ".com"
    assert len(value) < 254 < len(value.casefold())
    payload["contacts"] = [value]
    result = create_lead(uuid4(), payload)
    assert result.lead.contacts.get().value == value

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from leadflow.bot.models import BotUser
from leadflow.bot.models import Draft
from leadflow.bot.services import begin_edit
from leadflow.bot.services import bind_question
from leadflow.bot.services import cancel_draft
from leadflow.bot.services import confirm_draft
from leadflow.bot.services import get_dialogue
from leadflow.bot.services import remove_contact
from leadflow.bot.services import set_field
from leadflow.bot.services import start_draft
from leadflow.crm.models import SYSTEM_TAGS
from leadflow.crm.models import Lead
from leadflow.crm.models import SubmissionReceipt
from leadflow.crm.models import Tag
from leadflow.crm.services import StaleDraft
from leadflow.crm.services import SubmissionForbidden
from leadflow.crm.validation import InputError

pytestmark = pytest.mark.django_db


def review_draft(user_id=42):
    for code, name in SYSTEM_TAGS.items():
        Tag.objects.get_or_create(code=code, defaults={"name": name})
    draft = start_draft(user_id)
    assert draft is not None
    for field, value in [
        ("name", "Клиент"),
        ("contacts", "+77011234567"),
        ("continue_contacts", None),
        ("direction", "website"),
        ("request", "Нужен сайт"),
    ]:
        draft = set_field(user_id, draft.pk, draft.revision, field, value)
    return draft


def test_start_resume_and_restart_keep_one_draft():
    first = start_draft(42)
    assert first is not None
    changed = set_field(42, first.pk, first.revision, "name", "Клиент")
    resumed = start_draft(42)
    assert resumed.pk == first.pk
    assert resumed.values == changed.values
    restarted = start_draft(42, restart=True)
    assert restarted.pk != first.pk and restarted.values == {}
    assert Draft.objects.filter(user_id=42).count() == 1
    assert Lead.objects.count() == 0


def test_invalid_input_does_not_advance_or_change_values():
    draft = start_draft(42)
    assert draft is not None
    with pytest.raises(InputError):
        set_field(42, draft.pk, draft.revision, "name", " ")
    draft.refresh_from_db()
    assert draft.step == "name" and draft.revision == 0 and draft.values == {}


def test_cancel_deletes_values_and_old_operation_is_stale():
    draft = review_draft()
    assert cancel_draft(42, draft.pk, draft.revision) is None
    assert not Draft.objects.exists()
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0
    new = start_draft(42)
    with pytest.raises(StaleDraft):
        set_field(42, draft.pk, draft.revision, "name", "Другой")
    new.refresh_from_db()
    assert new.values == {}


def test_stale_revision_and_wrong_user_cannot_mutate():
    draft = start_draft(42)
    assert draft is not None
    changed = set_field(42, draft.pk, draft.revision, "name", "Клиент")
    with pytest.raises(StaleDraft):
        set_field(42, draft.pk, draft.revision, "name", "Другой")
    with pytest.raises(SubmissionForbidden):
        set_field(43, changed.pk, changed.revision, "contacts", "@alexander")
    assert get_dialogue(42).draft.values["name"] == "Клиент"


def test_confirmation_completes_draft_and_replay_leaves_new_draft_alone():
    draft = review_draft()
    result = confirm_draft(42, draft.pk, draft.revision)
    assert result is not None
    assert result.lead.source == "telegram_bot" and result.lead.status == "new"
    assert list(result.lead.tags.values_list("code", flat=True)) == ["website"]
    assert not Draft.objects.exists()
    state = get_dialogue(42)
    assert state.draft is None and state.last_receipt.lead_id == result.lead.pk
    new = start_draft(42)
    replay = confirm_draft(42, draft.pk, draft.revision)
    assert replay.replayed and replay.lead.pk == result.lead.pk
    assert get_dialogue(42).draft.pk == new.pk
    assert cancel_draft(42, draft.pk, draft.revision).replayed
    with pytest.raises(SubmissionForbidden):
        confirm_draft(43, draft.pk, draft.revision)
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1


def test_incomplete_draft_cannot_be_confirmed():
    draft = start_draft(42)
    assert draft is not None
    with pytest.raises(StaleDraft):
        confirm_draft(42, draft.pk, draft.revision)
    assert Lead.objects.count() == 0


def test_bot_save_failure_keeps_draft_for_retry():
    draft = review_draft()
    with patch("leadflow.crm.services.SubmissionReceipt.objects.create", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            confirm_draft(42, draft.pk, draft.revision)
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0
    assert get_dialogue(42).draft.values == draft.values
    assert BotUser.objects.get(pk=42).last_receipt_id is None
    assert confirm_draft(42, draft.pk, draft.revision) is not None


def test_bound_question_rejects_old_or_ambiguous_messages():
    draft = start_draft(42)
    assert draft is not None
    draft.question_id = 100
    draft.question_date = timezone.now().replace(microsecond=0)
    draft.save()
    for event in [
        None,
        {"chat_id": 42, "reply_to": 99},
        {"chat_id": 43, "reply_to": 100},
        {"chat_id": 42, "date": draft.question_date},
    ]:
        with pytest.raises(StaleDraft):
            set_field(42, draft.pk, draft.revision, "name", "Клиент", event=event)
    accepted = set_field(
        42,
        draft.pk,
        draft.revision,
        "name",
        "Клиент",
        event={"chat_id": 42, "date": draft.question_date + timedelta(seconds=1)},
    )
    assert accepted.step == "contacts"


def test_multiple_contacts_and_review_correction_share_validation():
    draft = review_draft()
    old_revision = draft.revision
    draft = begin_edit(42, draft.pk, draft.revision, "contacts")
    with pytest.raises(InputError):
        set_field(42, draft.pk, draft.revision, "contacts", "alexander")
    draft = set_field(42, draft.pk, draft.revision, "contacts", "@alexander")
    assert draft.step == "review"
    assert draft.values["contacts"] == ["+77011234567", "@alexander"]
    draft = begin_edit(42, draft.pk, draft.revision, "contacts", contact_index=0)
    draft = set_field(42, draft.pk, draft.revision, "contacts", "Alex@EXAMPLE.com")
    draft = remove_contact(42, draft.pk, draft.revision, 1)
    assert draft.values["contacts"] == ["Alex@EXAMPLE.com"]
    with pytest.raises(StaleDraft):
        confirm_draft(42, draft.pk, old_revision)
    result = confirm_draft(42, draft.pk, draft.revision)
    assert list(result.lead.contacts.values_list("value", flat=True)) == ["Alex@EXAMPLE.com"]


def test_removing_last_contact_requires_a_replacement_before_confirmation():
    draft = review_draft()
    draft = remove_contact(42, draft.pk, draft.revision, 0)
    assert draft.step == "contacts" and draft.values["contacts"] == []
    with pytest.raises(StaleDraft):
        confirm_draft(42, draft.pk, draft.revision)
    draft = set_field(42, draft.pk, draft.revision, "contacts", "@alexander")
    assert draft.step == "review"
    assert confirm_draft(42, draft.pk, draft.revision).lead is not None


def test_question_binding_survives_reload_and_old_binding_is_rejected():
    draft = start_draft(42)
    question_date = timezone.now().replace(microsecond=0)
    bind_question(42, draft.pk, draft.revision, 100, question_date)
    restored = get_dialogue(42).draft
    assert restored.question_id == 100 and restored.question_date == question_date
    changed = set_field(
        42, restored.pk, restored.revision, "name", "Клиент", event={"chat_id": 42, "reply_to": 100}
    )
    with pytest.raises(StaleDraft):
        bind_question(42, restored.pk, restored.revision, 101, question_date)
    assert get_dialogue(42).draft.revision == changed.revision


def test_contact_sharing_rejects_another_persons_number():
    draft = start_draft(42)
    draft = set_field(42, draft.pk, draft.revision, "name", "Клиент")
    question_date = timezone.now().replace(microsecond=0)
    bind_question(42, draft.pk, draft.revision, 100, question_date)
    event = {
        "chat_id": 42,
        "date": question_date + timedelta(seconds=1),
        "kind": "contact",
        "contact_user_id": 43,
    }
    with pytest.raises(StaleDraft):
        set_field(42, draft.pk, draft.revision, "contacts", "+77011234567", event=event)
    event["contact_user_id"] = 42
    changed = set_field(42, draft.pk, draft.revision, "contacts", "+77011234567", event=event)
    assert changed.values["contacts"] == ["+77011234567"]


def test_failure_while_completing_draft_rolls_back_the_entire_submission():
    draft = review_draft()
    with patch("leadflow.bot.models.Draft.delete", side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            confirm_draft(42, draft.pk, draft.revision)
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0
    assert get_dialogue(42).draft.pk == draft.pk
    assert BotUser.objects.get(pk=42).last_receipt_id is None
    assert confirm_draft(42, draft.pk, draft.revision).lead is not None


def test_contact_collection_can_add_then_continue():
    draft = start_draft(42)
    for field, value in [
        ("name", "Клиент"),
        ("contacts", "@alexander"),
        ("add_contact", None),
        ("contacts", "+77011234567"),
        ("continue_contacts", None),
    ]:
        draft = set_field(42, draft.pk, draft.revision, field, value)
    assert draft.step == "direction"
    assert draft.values["contacts"] == ["@alexander", "+77011234567"]


@pytest.mark.parametrize("field", ["name", "request"])
def test_null_character_preserves_draft_and_allows_correction(field):
    draft = review_draft()
    draft = begin_edit(42, draft.pk, draft.revision, field)
    with pytest.raises(InputError) as error:
        set_field(42, draft.pk, draft.revision, field, "x\x00y")
    assert field in error.value.field_errors
    restored = get_dialogue(42).draft
    assert restored.values == draft.values
    assert restored.revision == draft.revision and restored.step == field
    corrected = set_field(42, draft.pk, draft.revision, field, "Исправлено")
    assert corrected.step == "review" and corrected.values[field] == "Исправлено"


@pytest.mark.parametrize("seconds", [None, -1, 0, 1])
def test_contact_reply_requires_a_date_after_the_question(seconds):
    draft = start_draft(42)
    draft = set_field(42, draft.pk, draft.revision, "name", "Клиент")
    question_date = timezone.now().replace(microsecond=0)
    bind_question(42, draft.pk, draft.revision, 100, question_date)
    event = {
        "chat_id": 42,
        "reply_to": 100,
        "kind": "contact",
        "contact_user_id": 42,
    }
    if seconds is not None:
        event["date"] = question_date + timedelta(seconds=seconds)
    if seconds == 1:
        changed = set_field(42, draft.pk, draft.revision, "contacts", "+77011234567", event=event)
        assert changed.step == "contact_choice"
        assert changed.values["contacts"] == ["+77011234567"]
    else:
        with pytest.raises(StaleDraft):
            set_field(42, draft.pk, draft.revision, "contacts", "+77011234567", event=event)
        restored = get_dialogue(42).draft
        assert restored.values == draft.values
        assert restored.revision == draft.revision and restored.step == "contacts"

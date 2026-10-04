from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from leadflow.bot.models import BotPollingState
from leadflow.bot.models import BotUser
from leadflow.bot.models import Draft
from leadflow.bot.models import ProcessedUpdate
from leadflow.bot.services import append_request_part
from leadflow.bot.services import begin_edit
from leadflow.bot.services import bind_question
from leadflow.bot.services import cancel_draft
from leadflow.bot.services import confirm_draft
from leadflow.bot.services import confirm_draft_action
from leadflow.bot.services import continue_directions
from leadflow.bot.services import continue_request
from leadflow.bot.services import edit_input_message
from leadflow.bot.services import get_dialogue
from leadflow.bot.services import go_back
from leadflow.bot.services import keep_current_value
from leadflow.bot.services import keep_draft
from leadflow.bot.services import prepare_confirmation
from leadflow.bot.services import remove_contact
from leadflow.bot.services import request_draft_action
from leadflow.bot.services import resume_draft
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
    draft = set_field(user_id, draft.pk, draft.revision, "direction", "website")
    draft = continue_directions(user_id, draft.pk, draft.revision)
    draft = set_field(user_id, draft.pk, draft.revision, "request", "Нужен сайт")
    draft = continue_request(user_id, draft.pk, draft.revision)
    draft = set_field(user_id, draft.pk, draft.revision, "name", "Клиент")
    draft = set_field(user_id, draft.pk, draft.revision, "contacts", "+77011234567")
    draft = set_field(user_id, draft.pk, draft.revision, "continue_contacts", None)
    return draft


def name_draft(user_id=42):
    draft = start_draft(user_id)
    draft = set_field(user_id, draft.pk, draft.revision, "direction", "website")
    draft = continue_directions(user_id, draft.pk, draft.revision)
    draft = set_field(user_id, draft.pk, draft.revision, "request", "Нужен сайт")
    return continue_request(user_id, draft.pk, draft.revision)


def contact_draft(user_id=42):
    draft = name_draft(user_id)
    return set_field(user_id, draft.pk, draft.revision, "name", "Клиент")


def test_start_resume_and_restart_keep_one_draft():
    first = start_draft(42)
    assert first is not None
    changed = set_field(42, first.pk, first.revision, "direction", "website")
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
        set_field(42, draft.pk, draft.revision, "direction", "unsupported")
    draft.refresh_from_db()
    assert draft.step == Draft.Step.DIRECTION and draft.revision == 0 and draft.values == {}


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
    changed = set_field(42, draft.pk, draft.revision, "direction", "website")
    with pytest.raises(StaleDraft):
        set_field(42, draft.pk, draft.revision, "direction", "website")
    with pytest.raises(SubmissionForbidden):
        set_field(43, changed.pk, changed.revision, "direction", "website")
    assert get_dialogue(42).draft.values["directions"] == ["website"]


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


def test_multiple_selected_directions_are_saved_as_multiple_crm_tags():
    for code, name in SYSTEM_TAGS.items():
        Tag.objects.get_or_create(code=code, defaults={"name": name})
    draft = start_draft(42)
    draft = set_field(42, draft.pk, draft.revision, "direction", "website")
    draft = set_field(42, draft.pk, draft.revision, "direction", "advertising")
    assert draft.values["directions"] == ["website", "advertising"]
    draft = continue_directions(42, draft.pk, draft.revision)
    draft = set_field(42, draft.pk, draft.revision, "request", "Сайт и реклама")
    draft = continue_request(42, draft.pk, draft.revision)
    draft = set_field(42, draft.pk, draft.revision, "name", "Клиент")
    draft = set_field(42, draft.pk, draft.revision, "contacts", "client@example.com")
    draft = set_field(42, draft.pk, draft.revision, "continue_contacts", None)

    result = confirm_draft(42, draft.pk, draft.revision)

    assert set(result.lead.tags.values_list("code", flat=True)) == {"website", "advertising"}


def test_over_limit_request_part_can_be_fixed_by_editing_its_source_message():
    draft = start_draft(42)
    draft = set_field(42, draft.pk, draft.revision, "direction", "website")
    draft = continue_directions(42, draft.pk, draft.revision)
    question_date = timezone.now().replace(microsecond=0)
    bind_question(42, draft.pk, draft.revision, 100, question_date)
    first = "a" * 1500
    event = {
        "chat_id": 42,
        "date": question_date + timedelta(seconds=1),
        "reply_to": 100,
        "message_id": 101,
        "kind": "text",
    }
    draft, accepted, _ = append_request_part(
        42, draft.pk, draft.revision, first, event=event, source_message_id=101
    )
    assert accepted
    event["message_id"] = 102
    event["date"] += timedelta(seconds=1)
    draft, accepted, pending = append_request_part(
        42, draft.pk, draft.revision, "b" * 600, event=event, source_message_id=102
    )
    assert not accepted and draft.needs_correction
    assert draft.values["request"] == first

    draft, accepted = edit_input_message(
        42, draft.pk, draft.revision, pending.source_message_id, "b" * 400
    )

    assert accepted and not draft.needs_correction
    assert draft.values["request"] == f"{first}\n\n{'b' * 400}"
    pending.refresh_from_db()
    assert pending.active


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
    draft = name_draft(42)
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
    assert accepted.step == Draft.Step.CONTACTS


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


def test_request_addition_appends_as_paragraph_and_returns_to_review():
    draft = review_draft()
    draft = begin_edit(42, draft.pk, draft.revision, "request", mode="append")

    draft = set_field(42, draft.pk, draft.revision, "append_request", "Нужно SEO")

    assert draft.step == "review"
    assert draft.values["request"] == "Нужен сайт\n\nНужно SEO"


def test_request_addition_over_limit_preserves_original_value():
    draft = review_draft()
    draft.values["request"] = "x" * 1990
    draft.save(update_fields=["values"])
    draft = begin_edit(42, draft.pk, draft.revision, "request", mode="append")

    with pytest.raises(InputError):
        set_field(42, draft.pk, draft.revision, "append_request", "1234567890")

    unchanged = Draft.objects.get(pk=draft.pk)
    assert unchanged.step == "request"
    assert unchanged.values["request"] == "x" * 1990


def test_populated_draft_cancel_requires_an_explicit_confirmation():
    draft = review_draft()
    waiting = request_draft_action(42, draft.pk, draft.revision, Draft.PendingAction.CANCEL)

    assert waiting.pk == draft.pk
    assert waiting.pending_action == Draft.PendingAction.CANCEL
    assert Draft.objects.filter(pk=draft.pk).exists()
    assert Lead.objects.count() == 0

    assert confirm_draft_action(42, waiting.pk, waiting.revision) is None
    assert not Draft.objects.filter(pk=draft.pk).exists()
    assert Lead.objects.count() == 0


def test_user_can_keep_a_populated_draft_after_cancel_prompt():
    draft = review_draft()
    waiting = request_draft_action(42, draft.pk, draft.revision, Draft.PendingAction.CANCEL)

    kept = keep_draft(42, waiting.pk, waiting.revision)

    assert kept.pending_action == Draft.PendingAction.NONE
    assert kept.values == draft.values
    with pytest.raises(StaleDraft):
        confirm_draft_action(42, draft.pk, waiting.revision)


def test_empty_draft_can_restart_immediately_and_navigation_keeps_values():
    empty = start_draft(42)
    replacement = request_draft_action(42, empty.pk, empty.revision, Draft.PendingAction.RESTART)
    assert replacement.pk != empty.pk and replacement.values == {}

    draft = set_field(42, replacement.pk, replacement.revision, "direction", "website")
    draft = continue_directions(42, draft.pk, draft.revision)
    draft = set_field(42, draft.pk, draft.revision, "request", "Нужен сайт")
    draft = continue_request(42, draft.pk, draft.revision)
    draft = set_field(42, draft.pk, draft.revision, "name", "Клиент")
    draft = go_back(42, draft.pk, draft.revision)
    draft = go_back(42, draft.pk, draft.revision)
    assert draft.step == Draft.Step.REQUEST
    assert draft.values["name"] == "Клиент"
    with pytest.raises(StaleDraft):
        set_field(42, draft.pk, draft.revision, "name", "Неожиданный ответ")
    draft = keep_current_value(42, draft.pk, draft.revision)
    assert draft.step == Draft.Step.NAME
    assert draft.values["name"] == "Клиент"


def test_back_from_review_can_keep_the_saved_request():
    draft = review_draft()
    saved_values = draft.values.copy()
    draft = go_back(42, draft.pk, draft.revision)

    assert draft.step == Draft.Step.CONTACT_CHOICE
    draft = set_field(42, draft.pk, draft.revision, "continue_contacts", None)

    assert draft.step == Draft.Step.REVIEW
    assert draft.values == saved_values


@pytest.mark.parametrize("mode", ["replace", "append"])
def test_back_from_explicit_request_edit_returns_to_review_without_changing_values(mode):
    draft = review_draft()
    saved_values = draft.values.copy()
    draft = begin_edit(42, draft.pk, draft.revision, "request", mode=mode)

    draft = go_back(42, draft.pk, draft.revision)

    assert draft.step == Draft.Step.REVIEW
    assert draft.values == saved_values
    assert not draft.editing_field


def test_confirmed_submission_is_frozen_and_can_be_resumed():
    draft = review_draft()
    state, _ = BotPollingState.objects.get_or_create(bot_id=77)
    event = ProcessedUpdate.objects.create(polling_state=state, update_id=123)
    pending = prepare_confirmation(42, draft.pk, draft.revision, event)

    assert pending.submission_state == "pending"
    assert pending.pending_revision == draft.revision
    with pytest.raises(StaleDraft):
        set_field(42, draft.pk, pending.revision, "request", "Изменено")

    result = confirm_draft(42, draft.pk, pending.pending_revision)
    assert result.lead.source == "telegram_bot"
    assert not Draft.objects.filter(pk=draft.pk).exists()


@pytest.mark.parametrize("action", [Draft.PendingAction.CANCEL, Draft.PendingAction.RESTART])
def test_paused_draft_can_be_cancelled_or_restarted(action):
    draft = name_draft(42)
    draft = set_field(42, draft.pk, draft.revision, "name", "Клиент")
    draft.step = Draft.Step.PAUSED
    draft.return_step = Draft.Step.NAME
    draft.save(update_fields=["step", "return_step"])
    paused = draft
    assert paused.step == Draft.Step.PAUSED

    result = request_draft_action(42, paused.pk, paused.revision, action)
    assert result.values == paused.values
    assert result.pending_action == action
    result = confirm_draft_action(42, result.pk, result.revision)

    assert not Draft.objects.filter(pk=paused.pk).exists()
    if action == Draft.PendingAction.RESTART:
        assert result.pk != paused.pk
        assert result.step == Draft.Step.DIRECTION
        assert result.values == {}
    else:
        assert result is None
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0


@pytest.mark.parametrize("action", [Draft.PendingAction.CANCEL, Draft.PendingAction.RESTART])
def test_paused_draft_can_be_kept_after_an_action_prompt(action):
    draft = name_draft(42)
    draft = set_field(42, draft.pk, draft.revision, "name", "Клиент")
    draft.step = Draft.Step.PAUSED
    draft.return_step = Draft.Step.NAME
    draft.save(update_fields=["step", "return_step"])
    paused = draft
    waiting = request_draft_action(42, paused.pk, paused.revision, action)

    kept = keep_draft(42, waiting.pk, waiting.revision)

    assert kept.pk == paused.pk
    assert kept.values == paused.values
    assert kept.step == Draft.Step.PAUSED
    assert kept.pending_action == Draft.PendingAction.NONE
    with pytest.raises(StaleDraft):
        confirm_draft_action(42, waiting.pk, waiting.revision)
    resumed = resume_draft(42, kept.pk, kept.revision)
    assert resumed.step == Draft.Step.NAME
    assert resumed.values == paused.values


def test_back_from_direction_returns_through_contacts_to_name_and_keeps_values():
    draft = review_draft()
    saved_values = draft.values

    for step in [
        Draft.Step.CONTACT_CHOICE,
        Draft.Step.CONTACTS,
        Draft.Step.NAME,
        Draft.Step.REQUEST,
        Draft.Step.DIRECTION,
    ]:
        draft = go_back(42, draft.pk, draft.revision)
        assert draft.step == step
        assert draft.values == saved_values


def test_adding_contact_after_back_correction_appends_without_replacing_existing_contact():
    draft = name_draft(42)
    draft = set_field(42, draft.pk, draft.revision, "name", "Клиент")
    draft = set_field(42, draft.pk, draft.revision, "contacts", "old@example.com")
    draft = go_back(42, draft.pk, draft.revision)
    draft = set_field(42, draft.pk, draft.revision, "contacts", "updated@example.com")

    draft = set_field(42, draft.pk, draft.revision, "add_contact", None)
    draft = set_field(42, draft.pk, draft.revision, "contacts", "another@example.com")

    assert draft.values["contacts"] == ["updated@example.com", "another@example.com"]


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
    draft = name_draft(42)
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
    draft = contact_draft(42)
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
    draft = contact_draft(42)
    for field, value in [
        ("contacts", "@alexander"),
        ("add_contact", None),
        ("contacts", "+77011234567"),
        ("continue_contacts", None),
    ]:
        draft = set_field(42, draft.pk, draft.revision, field, value)
    assert draft.step == Draft.Step.REVIEW
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
    draft = contact_draft(42)
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

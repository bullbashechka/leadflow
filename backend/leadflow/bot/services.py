"""Persistent dialogue operations. Telegram transport stays outside this module."""

from dataclasses import dataclass

from django.db import transaction

from leadflow.crm.models import SYSTEM_TAGS
from leadflow.crm.models import SubmissionReceipt
from leadflow.crm.services import StaleDraft
from leadflow.crm.services import SubmissionForbidden
from leadflow.crm.services import SubmissionResult
from leadflow.crm.services import create_lead
from leadflow.crm.services import parse_submission_id
from leadflow.crm.validation import InputError
from leadflow.crm.validation import validate_contacts
from leadflow.crm.validation import validate_text

from .models import BotUser
from .models import Draft


@dataclass(frozen=True)
class Dialogue:
    draft: Draft | None
    last_receipt: SubmissionReceipt | None


def get_dialogue(user_id):
    with transaction.atomic():
        state = BotUser.objects.select_for_update().filter(pk=user_id).first()
        if not state:
            return Dialogue(None, None)
        return Dialogue(Draft.objects.filter(user=state).first(), state.last_receipt)


def start_draft(user_id, *, restart=False):
    if type(user_id) is not int or user_id <= 0:
        raise SubmissionForbidden("Invalid Telegram identity")
    with transaction.atomic():
        BotUser.objects.get_or_create(pk=user_id)
        state = BotUser.objects.select_for_update().get(pk=user_id)
        draft = Draft.objects.filter(user=state).first()
        if draft and draft.submission_state == "pending":
            raise StaleDraft("The confirmed submission is still being checked")
        if draft and not restart:
            return draft
        if draft:
            draft.delete()
        return Draft.objects.create(user=state)


def set_field(user_id, submission_id, revision, field, value, *, event=None):
    """Advance a current draft; event=None is for trusted internal callers only."""
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if field in {"name", "contacts", "request", "append_request"}:
            _check_event(draft, event)
        if draft.step == "name" and field == "name":
            draft.values["name"] = validate_text(value, "name", 100)
            draft.step = (
                "review"
                if draft.editing_field
                else "contact_choice"
                if draft.values.get("contacts")
                else "contacts"
            )
        elif draft.step == "contacts" and field == "contacts":
            values = list(draft.values.get("contacts", []))
            if draft.contact_index is None:
                values.append(value)
            else:
                values[draft.contact_index] = value
            contacts = validate_contacts(values)
            draft.values["contacts"] = [item.value for item in contacts]
            draft.step = "review" if draft.editing_field else "contact_choice"
        elif draft.step == "contact_choice" and field in {"add_contact", "continue_contacts"}:
            validate_contacts(draft.values.get("contacts", []))
            draft.contact_index = None
            draft.edit_mode = Draft.EditMode.NONE
            draft.step = "contacts" if field == "add_contact" else "direction"
        elif draft.step == "direction" and field == "direction":
            if not isinstance(value, str) or value not in SYSTEM_TAGS:
                raise InputError({"direction": ["Выберите одно из четырёх направлений."]})
            draft.values["direction"] = value
            draft.step = "review" if draft.editing_field else "request"
        elif draft.step == "request" and field in {"request", "append_request"}:
            request = validate_text(value, "request", 2000)
            if field == "append_request":
                if draft.editing_field != "request" or draft.edit_mode != Draft.EditMode.APPEND:
                    raise StaleDraft("Request addition is no longer current")
                request = validate_text(f"{draft.values['request']}\n\n{request}", "request", 2000)
            draft.values["request"] = request
            draft.step = "review"
        else:
            raise StaleDraft("Input does not match the current step")
        if draft.step == "review":
            draft.editing_field = ""
            draft.edit_mode = Draft.EditMode.NONE
            draft.contact_index = None
            draft.return_step = ""
        return _save_transition(draft)


def cancel_draft(user_id, submission_id, revision):
    with transaction.atomic():
        # Serialize cancellation with confirmation, restart and field changes.
        BotUser.objects.select_for_update().filter(pk=user_id).first()
        receipt = (
            SubmissionReceipt.objects.select_related("lead")
            .filter(
                pk=parse_submission_id(submission_id),
            )
            .first()
        )
        if receipt:
            if receipt.channel != "telegram_bot" or receipt.owner_id != user_id:
                raise SubmissionForbidden("Submission belongs to another owner")
            return SubmissionResult(receipt.lead, True)
        draft = _locked_draft(user_id, submission_id, revision)
        draft.delete()
        return None


def confirm_draft(user_id, submission_id, revision):
    return create_lead(submission_id, bot_user_id=user_id, draft_revision=revision)


def prepare_confirmation(user_id, submission_id, revision, processed_update):
    """Persist user intent before creating a lead, so unknown outcomes are resumable."""
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != Draft.Step.REVIEW or draft.pending_action:
            raise StaleDraft("The current review is no longer available")
        draft.submission_state = "pending"
        draft.pending_revision = draft.revision
        draft.pending_update = processed_update
        draft.pending_action = Draft.PendingAction.NONE
        draft.return_step = ""
        draft.revision += 1
        draft.question_id = None
        draft.question_date = None
        draft.save()
        return draft


def request_draft_action(user_id, submission_id, revision, action):
    if action not in {Draft.PendingAction.CANCEL, Draft.PendingAction.RESTART}:
        raise StaleDraft("The requested draft action is unavailable")
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision, allow_paused=True)
        if draft.pending_action:
            raise StaleDraft("The requested draft action is no longer current")
        if not draft.values:
            if action == Draft.PendingAction.CANCEL:
                draft.delete()
                return None
            draft.delete()
            return Draft.objects.create(user_id=user_id)
        draft.pending_action = action
        draft.revision += 1
        draft.question_id = None
        draft.question_date = None
        draft.save(update_fields=["pending_action", "revision", "question_id", "question_date"])
        return draft


def confirm_draft_action(user_id, submission_id, revision):
    with transaction.atomic():
        draft = _locked_draft(
            user_id, submission_id, revision, allow_paused=True, allow_pending_action=True
        )
        action = draft.pending_action
        if action not in {Draft.PendingAction.CANCEL, Draft.PendingAction.RESTART}:
            raise StaleDraft("No draft action is awaiting confirmation")
        if action == Draft.PendingAction.CANCEL:
            draft.delete()
            return None
        draft.delete()
        return Draft.objects.create(user_id=user_id)


def keep_draft(user_id, submission_id, revision):
    with transaction.atomic():
        draft = _locked_draft(
            user_id, submission_id, revision, allow_paused=True, allow_pending_action=True
        )
        if not draft.pending_action:
            raise StaleDraft("No draft action is awaiting a decision")
        draft.pending_action = Draft.PendingAction.NONE
        draft.revision += 1
        draft.question_id = None
        draft.question_date = None
        draft.save(update_fields=["pending_action", "revision", "question_id", "question_date"])
        return draft


def resume_draft(user_id, submission_id, revision):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision, allow_paused=True)
        if draft.step != Draft.Step.PAUSED:
            raise StaleDraft("The draft is not paused")
        draft.step = draft.return_step or Draft.Step.NAME
        draft.return_step = ""
        return _save_transition(draft)


def go_back(user_id, submission_id, revision):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.pending_action:
            raise StaleDraft("A draft action is awaiting confirmation")
        if draft.editing_field:
            draft.step = Draft.Step.REVIEW
            draft.editing_field = ""
            draft.edit_mode = Draft.EditMode.NONE
            draft.contact_index = None
            draft.return_step = ""
        elif draft.step == Draft.Step.NAME:
            draft.step = Draft.Step.PAUSED
            draft.return_step = Draft.Step.NAME
        elif draft.step == Draft.Step.CONTACTS:
            draft.step = (
                Draft.Step.NAME
                if draft.edit_mode == Draft.EditMode.CONTACT_CHOICE
                or not draft.values.get("contacts")
                else Draft.Step.CONTACT_CHOICE
            )
            draft.contact_index = None
            draft.edit_mode = Draft.EditMode.NONE
        elif draft.step == Draft.Step.CONTACT_CHOICE:
            draft.step = Draft.Step.CONTACTS
            draft.contact_index = len(draft.values.get("contacts", [])) - 1
            draft.edit_mode = Draft.EditMode.CONTACT_CHOICE
        elif draft.step == Draft.Step.DIRECTION:
            draft.step = Draft.Step.CONTACT_CHOICE
            draft.edit_mode = Draft.EditMode.NONE
            draft.contact_index = None
        elif draft.step == Draft.Step.REQUEST:
            draft.step = Draft.Step.DIRECTION
            draft.edit_mode = Draft.EditMode.NONE
        elif draft.step == Draft.Step.REVIEW:
            draft.step = Draft.Step.REQUEST
        else:
            raise StaleDraft("There is no earlier dialogue step")
        return _save_transition(draft)


def keep_current_value(user_id, submission_id, revision):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        field = draft.editing_field or draft.step
        if field not in draft.values:
            raise StaleDraft("There is no saved value to keep")
        if draft.editing_field:
            draft.step = draft.return_step or Draft.Step.REVIEW
            draft.editing_field = ""
            draft.edit_mode = Draft.EditMode.NONE
            draft.contact_index = None
            draft.return_step = ""
        elif draft.step == Draft.Step.NAME:
            draft.step = (
                Draft.Step.CONTACT_CHOICE if draft.values.get("contacts") else Draft.Step.CONTACTS
            )
        elif draft.step == Draft.Step.CONTACTS:
            draft.step = Draft.Step.CONTACT_CHOICE
            draft.contact_index = None
            draft.edit_mode = Draft.EditMode.NONE
        elif draft.step == Draft.Step.DIRECTION:
            draft.step = Draft.Step.REQUEST
            draft.return_step = ""
        elif draft.step == Draft.Step.REQUEST:
            draft.step = Draft.Step.REVIEW
        else:
            raise StaleDraft("The current value cannot be kept at this step")
        return _save_transition(draft)


def bind_question(user_id, submission_id, revision, message_id, message_date):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        draft.question_id = message_id
        draft.question_date = message_date
        draft.save(update_fields=["question_id", "question_date"])
        return draft


def begin_edit(user_id, submission_id, revision, field, *, contact_index=None, mode="replace"):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != "review" or field not in {"name", "contacts", "direction", "request"}:
            raise StaleDraft("Only the current review can be edited")
        if mode not in {Draft.EditMode.REPLACE, Draft.EditMode.APPEND}:
            raise StaleDraft("The requested edit mode is unavailable")
        if mode == Draft.EditMode.APPEND and field != "request":
            raise StaleDraft("Only a request can be appended")
        if field == "contacts" and contact_index is not None:
            _check_contact_index(draft, contact_index)
        draft.editing_field = field
        draft.edit_mode = mode
        draft.contact_index = contact_index if field == "contacts" else None
        draft.return_step = Draft.Step.REVIEW
        draft.step = field
        return _save_transition(draft)


def remove_contact(user_id, submission_id, revision, contact_index):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != "review":
            raise StaleDraft("Only the current review can be edited")
        _check_contact_index(draft, contact_index)
        del draft.values["contacts"][contact_index]
        if not draft.values["contacts"]:
            draft.step = "contacts"
            draft.editing_field = "contacts"
            draft.edit_mode = Draft.EditMode.REPLACE
            draft.contact_index = None
            draft.return_step = Draft.Step.REVIEW
        return _save_transition(draft)


def _check_contact_index(draft, index):
    if type(index) is not int or not 0 <= index < len(draft.values.get("contacts", [])):
        raise InputError({"contacts": ["Контакт недоступен."]})


def _locked_draft(
    user_id,
    submission_id,
    revision,
    *,
    allow_paused=False,
    allow_pending_action=False,
):
    submission_id = parse_submission_id(submission_id)
    state = BotUser.objects.select_for_update().filter(pk=user_id).first()
    draft = Draft.objects.filter(pk=submission_id).first()
    if draft and draft.user_id != user_id:
        raise SubmissionForbidden("Submission belongs to another user")
    if not state or not draft or type(revision) is not int or draft.revision != revision:
        raise StaleDraft("Draft or revision is no longer current")
    if draft.step == Draft.Step.PAUSED and not allow_paused:
        raise StaleDraft("The draft must be resumed before it can be changed")
    if draft.submission_state != "collecting":
        raise StaleDraft("The confirmed submission is still being checked")
    if draft.pending_action and not allow_pending_action:
        raise StaleDraft("A draft action is awaiting confirmation")
    return draft


def _save_transition(draft):
    draft.revision += 1
    draft.question_id = None
    draft.question_date = None
    draft.save()
    return draft


def _check_event(draft, event):
    if event is None and draft.question_id is None:
        return
    if not event or draft.question_id is None or event.get("chat_id") != draft.user_id:
        raise StaleDraft("Question binding is unavailable")
    if event.get("kind", "text") == "contact":
        if draft.step != "contacts" or event.get("contact_user_id") != draft.user_id:
            raise StaleDraft("Contact does not belong to the expected sender")
    elif event.get("kind", "text") != "text":
        raise StaleDraft("Unexpected message type")
    if event.get("reply_to") is not None:
        if event["reply_to"] != draft.question_id:
            raise StaleDraft("Reply belongs to an earlier question")
    if event.get("reply_to") is None or event.get("kind") == "contact":
        if not event.get("date") or not draft.question_date or event["date"] <= draft.question_date:
            raise StaleDraft("Message cannot be assigned to the current question")

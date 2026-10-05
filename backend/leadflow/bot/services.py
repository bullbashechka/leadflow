"""Persistent dialogue operations. Telegram transport stays outside this module."""

from dataclasses import dataclass

from django.db import transaction

from leadflow.crm.models import SYSTEM_TAGS
from leadflow.crm.models import SubmissionReceipt
from leadflow.crm.services import LeadDeleted
from leadflow.crm.services import StaleDraft
from leadflow.crm.services import SubmissionForbidden
from leadflow.crm.services import SubmissionResult
from leadflow.crm.services import create_lead
from leadflow.crm.services import parse_submission_id
from leadflow.crm.validation import InputError
from leadflow.crm.validation import validate_contacts
from leadflow.crm.validation import validate_text
from leadflow.crm.validation import validate_text_content

from .models import BotUser
from .models import Draft
from .models import DraftInput


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


def choose_username(user_id, submission_id, revision, *, event=None):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        username = draft.last_username_offer
        if not username or draft.step != Draft.Step.CONTACTS:
            raise StaleDraft("No Telegram username is available")
        if event is not None:
            _check_event(draft, event)
        _set_contact_input(draft, f"@{username}", message_id=None)
        if draft.editing_field:
            draft.step = Draft.Step.REVIEW
            _finish_edit(draft)
        else:
            draft.step = Draft.Step.CONTACT_CHOICE
        return _save_transition(draft)


def continue_directions(user_id, submission_id, revision):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != Draft.Step.DIRECTION:
            raise StaleDraft("The direction step is no longer current")
        if not draft.values.get("directions"):
            raise InputError(
                {"directions": ["Выберите хотя бы одно направление перед продолжением."]}
            )
        draft.step = Draft.Step.REVIEW if draft.editing_field == "direction" else Draft.Step.REQUEST
        if draft.step == Draft.Step.REVIEW:
            _finish_edit(draft)
        return _save_transition(draft)


def continue_request(user_id, submission_id, revision):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != Draft.Step.REQUEST or draft.needs_correction:
            raise StaleDraft("The request still needs attention")
        if not draft.values.get("request"):
            raise InputError({"request": ["Добавьте описание заявки."]})
        draft.step = Draft.Step.REVIEW if draft.editing_field == "request" else Draft.Step.NAME
        if draft.step == Draft.Step.REVIEW:
            _finish_edit(draft)
        return _save_transition(draft)


def append_request_part(user_id, submission_id, revision, value, *, event, source_message_id):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != Draft.Step.REQUEST:
            raise StaleDraft("Description is not the current step")
        if draft.needs_correction:
            raise StaleDraft("Correct or remove the pending request part first")
        _check_event(draft, event)
        part = validate_text_content(value, "request")
        current = draft.values.get("request", "")
        combined = f"{current}\n\n{part}" if current else part
        try:
            validate_text(combined, "request", 2000)
        except InputError:
            source = DraftInput.objects.create(
                draft=draft,
                field=DraftInput.Field.REQUEST,
                position=_next_input_position(draft, DraftInput.Field.REQUEST),
                source_message_id=source_message_id,
                accepted_text="",
                pending_text=part,
                active=False,
            )
            draft.needs_correction = True
            _save_request_state(draft, preserve_question=True)
            return draft, False, source
        source = DraftInput.objects.create(
            draft=draft,
            field=DraftInput.Field.REQUEST,
            position=_next_input_position(draft, DraftInput.Field.REQUEST),
            source_message_id=source_message_id,
            accepted_text=part,
        )
        draft.values["request"] = combined
        _save_request_state(draft, preserve_question=True)
        return draft, True, source


def resolve_request_part(user_id, submission_id, revision, position, *, keep=False):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != Draft.Step.REQUEST or not draft.needs_correction:
            raise StaleDraft("No request part needs correction")
        source = (
            DraftInput.objects.select_for_update()
            .filter(draft=draft, field=DraftInput.Field.REQUEST, position=position, active=False)
            .first()
        )
        if not source:
            raise StaleDraft("Request part is no longer current")
        if keep:
            source.active = True
            source.accepted_text = source.pending_text
            source.pending_text = ""
            source.save(update_fields=["active", "accepted_text", "pending_text"])
            combined = _request_from_parts(draft)
            try:
                draft.values["request"] = validate_text(combined, "request", 2000)
            except InputError:
                source.active = False
                source.accepted_text = ""
                source.pending_text = combined
                source.save(update_fields=["active", "accepted_text", "pending_text"])
                raise
        else:
            source.active = False
            source.pending_text = ""
            source.save(update_fields=["active", "pending_text"])
        draft.needs_correction = DraftInput.objects.filter(
            draft=draft, field=DraftInput.Field.REQUEST, pending_text__gt=""
        ).exists()
        draft.values["request"] = _request_from_parts(draft)
        _save_request_state(draft, preserve_question=True)
        return draft


def edit_input_message(user_id, submission_id, revision, message_id, new_text):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        pending_candidate = next(
            (
                candidate
                for candidate in draft.pending_inputs
                if candidate.get("message_id") == message_id
            ),
            None,
        )
        if pending_candidate and draft.step == Draft.Step.REVIEW:
            try:
                text = validate_text(new_text, "request", 2000)
            except InputError:
                return draft, False
            candidate = dict(pending_candidate)
            candidate["text"] = text
            if (
                sum(
                    len(text) if item is pending_candidate else len(item["text"])
                    for item in draft.pending_inputs
                )
                > 4000
            ):
                return draft, False
            draft.pending_inputs = [
                candidate if item.get("message_id") == message_id else item
                for item in draft.pending_inputs
            ]
            _save_transition(draft)
            return draft, True
        source = (
            DraftInput.objects.select_for_update()
            .filter(
                draft=draft,
                source_message_id=message_id,
                active=True,
                editing_enabled=True,
            )
            .first()
        )
        pending_request = False
        if not source and draft.step == Draft.Step.REQUEST:
            source = (
                DraftInput.objects.select_for_update()
                .filter(
                    draft=draft,
                    source_message_id=message_id,
                    field=DraftInput.Field.REQUEST,
                    active=False,
                    pending_text__gt="",
                    editing_enabled=True,
                )
                .first()
            )
            pending_request = source is not None
        if not source:
            raise StaleDraft("This message is not an editable part of the current draft")
        if source.field == DraftInput.Field.REQUEST:
            try:
                text = validate_text(new_text, "request", 2000)
                candidate = _request_from_parts(
                    draft,
                    replace=(source.position, text),
                    include_inactive=source if pending_request else None,
                )
                validate_text(candidate, "request", 2000)
            except InputError:
                return draft, False
            source.accepted_text = text
            source.pending_text = ""
            if pending_request:
                source.active = True
            source.save(update_fields=["accepted_text", "pending_text", "active"])
            draft.values["request"] = _request_from_parts(draft)
            draft.needs_correction = DraftInput.objects.filter(
                draft=draft, pending_text__gt=""
            ).exists()
            _save_request_state(draft, preserve_question=True)
            return draft, True
        if source.field == DraftInput.Field.NAME:
            try:
                text = validate_text(new_text, "name", 100)
            except InputError:
                return draft, False
            source.accepted_text = text
            source.pending_text = ""
            source.save(update_fields=["accepted_text", "pending_text"])
            draft.values["name"] = text
        else:
            try:
                text = validate_text(new_text, "contacts", 254)
            except InputError:
                return draft, False
            old = source.accepted_text
            source.accepted_text = text
            source.pending_text = ""
            source.save(update_fields=["accepted_text", "pending_text"])
            try:
                draft.values["contacts"] = _sync_contact_inputs(draft)
            except InputError:
                source.accepted_text = old
                source.pending_text = ""
                source.save(update_fields=["accepted_text", "pending_text"])
                return draft, False
        _save_transition(draft)
        return draft, True


def append_review_input(user_id, submission_id, revision, value, source_message_id):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != Draft.Step.REVIEW:
            raise StaleDraft("The request is no longer under review")
        text = validate_text(value, "request", 2000)
        if len(draft.pending_inputs) >= 10 or (
            sum(len(item["text"]) for item in draft.pending_inputs) + len(text) > 4000
        ):
            raise InputError(
                {
                    "request": [
                        "Новое сообщение не сохранено: ожидают решения до 10 сообщений "
                        "общим объёмом до 4000 символов. Добавьте или отклоните их, "
                        "затем отправьте новое сообщение ещё раз."
                    ]
                }
            )
        draft.pending_inputs = [
            *draft.pending_inputs,
            {"message_id": source_message_id, "text": text},
        ]
        draft.revision += 1
        draft.save(update_fields=["pending_inputs", "revision"])
        return draft


def decide_review_input(user_id, submission_id, revision, *, add):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != Draft.Step.REVIEW or not draft.pending_inputs:
            raise StaleDraft("There is no additional text to decide")
        if add:
            for candidate in draft.pending_inputs:
                combined = f"{draft.values['request']}\n\n{candidate['text']}"
                text = validate_text(combined, "request", 2000)
                DraftInput.objects.create(
                    draft=draft,
                    field=DraftInput.Field.REQUEST,
                    position=_next_input_position(draft, DraftInput.Field.REQUEST),
                    source_message_id=candidate["message_id"],
                    accepted_text=candidate["text"],
                )
                draft.values["request"] = text
        else:
            DraftInput.objects.filter(
                draft=draft,
                source_message_id__in=[item["message_id"] for item in draft.pending_inputs],
            ).update(editing_enabled=False)
        draft.pending_inputs = []
        _save_transition(draft)
        return draft


def remove_request_part(user_id, submission_id, revision, position):
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if draft.step != Draft.Step.REVIEW:
            raise StaleDraft("Only the current review can be edited")
        source = (
            DraftInput.objects.select_for_update()
            .filter(draft=draft, field=DraftInput.Field.REQUEST, position=position, active=True)
            .first()
        )
        if not source:
            raise StaleDraft("Request part is no longer current")
        source.active = False
        source.editing_enabled = False
        source.save(update_fields=["active", "editing_enabled"])
        draft.values["request"] = _request_from_parts(draft)
        if not draft.values["request"]:
            draft.step = Draft.Step.REQUEST
            draft.editing_field = "request"
            draft.edit_mode = Draft.EditMode.REPLACE
            draft.return_step = Draft.Step.REVIEW
        return _save_transition(draft)


def _next_input_position(draft, field):
    current = DraftInput.objects.filter(draft=draft, field=field).order_by("-position").first()
    return current.position + 1 if current else 0


def _save_contact_input(draft, value, message_id):
    position = _next_input_position(draft, DraftInput.Field.CONTACT)
    DraftInput.objects.create(
        draft=draft,
        field=DraftInput.Field.CONTACT,
        position=position,
        source_message_id=message_id,
        accepted_text=value,
    )


def _set_contact_input(draft, value, *, message_id):
    contact = validate_contacts([value])[0]
    if draft.contact_index is None:
        _save_contact_input(draft, contact.value, message_id)
    else:
        source = _contact_source_for_index(draft, draft.contact_index)
        if not source:
            raise StaleDraft("The selected contact is no longer current")
        source.accepted_text = contact.value
        source.source_message_id = message_id
        source.save(update_fields=["accepted_text", "source_message_id"])
    draft.values["contacts"] = _sync_contact_inputs(draft)


def _normalized_contacts(draft):
    values = list(
        DraftInput.objects.filter(draft=draft, field=DraftInput.Field.CONTACT, active=True)
        .order_by("position")
        .values_list("accepted_text", flat=True)
    )
    return [item.value for item in validate_contacts(values)]


def _sync_contact_inputs(draft):
    rows = list(
        DraftInput.objects.select_for_update()
        .filter(draft=draft, field=DraftInput.Field.CONTACT, active=True)
        .order_by("position")
    )
    seen = set()
    for row in rows:
        contact = validate_contacts([row.accepted_text])[0]
        key = (contact.type, contact.key)
        if key in seen:
            row.active = False
            row.editing_enabled = False
            row.save(update_fields=["active", "editing_enabled"])
        else:
            seen.add(key)
    return _normalized_contacts(draft) if seen else []


def _contact_source_for_index(draft, index):
    _check_contact_index(draft, index)
    value = draft.values["contacts"][index]
    return (
        DraftInput.objects.select_for_update()
        .filter(
            draft=draft,
            field=DraftInput.Field.CONTACT,
            active=True,
            accepted_text=value,
        )
        .order_by("position")
        .first()
    )


def _request_from_parts(draft, *, replace=None, include_inactive=None):
    parts = list(
        DraftInput.objects.filter(draft=draft, field=DraftInput.Field.REQUEST, active=True)
        .order_by("position")
        .values_list("position", "accepted_text")
    )
    if include_inactive:
        parts.append((include_inactive.position, include_inactive.accepted_text))
        parts.sort(key=lambda item: item[0])
    if (
        not parts
        and not DraftInput.objects.filter(draft=draft, field=DraftInput.Field.REQUEST).exists()
        and draft.values.get("request")
    ):
        return draft.values["request"]
    if replace:
        parts = [
            (position, replace[1] if position == replace[0] else current_text)
            for position, current_text in parts
        ]
    return "\n\n".join(text for _, text in parts)


def _save_request_state(draft, *, preserve_question):
    draft.revision += 1
    fields = ["values", "revision", "needs_correction"]
    if not preserve_question:
        draft.question_id = None
        draft.question_date = None
        fields.extend(["question_id", "question_date"])
    draft.save(update_fields=fields)


def _finish_edit(draft):
    draft.editing_field = ""
    draft.edit_mode = Draft.EditMode.NONE
    draft.contact_index = None
    draft.return_step = ""


def set_field(user_id, submission_id, revision, field, value, *, event=None):
    """Advance a current draft; event=None is for trusted internal callers only."""
    with transaction.atomic():
        draft = _locked_draft(user_id, submission_id, revision)
        if field in {"name", "contacts", "request", "append_request"}:
            _check_event(draft, event)
        if draft.step == "name" and field == "name":
            draft.values["name"] = validate_text(value, "name", 100)
            if event and event.get("message_id"):
                DraftInput.objects.update_or_create(
                    draft=draft,
                    field=DraftInput.Field.NAME,
                    position=0,
                    defaults={"source_message_id": event["message_id"], "accepted_text": value},
                )
            draft.step = "review" if draft.editing_field else "contacts"
        elif draft.step == "contacts" and field == "contacts":
            _set_contact_input(
                draft,
                value,
                message_id=event.get("message_id") if event else None,
            )
            draft.step = "review" if draft.editing_field else "contact_choice"
        elif draft.step == "contact_choice" and field in {"add_contact", "continue_contacts"}:
            validate_contacts(draft.values.get("contacts", []))
            draft.contact_index = None
            draft.edit_mode = Draft.EditMode.NONE
            draft.step = "contacts" if field == "add_contact" else "review"
        elif draft.step == "direction" and field == "direction":
            if not isinstance(value, str) or value not in SYSTEM_TAGS:
                raise InputError({"direction": ["Выберите одно из четырёх направлений."]})
            directions = list(draft.values.get("directions", []))
            if value in directions:
                directions.remove(value)
            else:
                directions.append(value)
            draft.values["directions"] = directions
            draft.step = "direction"
        elif draft.step == "request" and field in {"request", "append_request"}:
            request_part = validate_text(value, "request", 2000)
            request = request_part
            if field == "append_request":
                if draft.editing_field != "request" or draft.edit_mode != Draft.EditMode.APPEND:
                    raise StaleDraft("Request addition is no longer current")
                request = validate_text(
                    f"{draft.values['request']}\n\n{request_part}", "request", 2000
                )
            draft.values["request"] = request
            if draft.editing_field and draft.edit_mode != Draft.EditMode.APPEND:
                DraftInput.objects.filter(
                    draft=draft, field=DraftInput.Field.REQUEST, active=True
                ).update(active=False, editing_enabled=False)
            DraftInput.objects.create(
                draft=draft,
                field=DraftInput.Field.REQUEST,
                position=_next_input_position(draft, DraftInput.Field.REQUEST),
                source_message_id=event.get("message_id") if event else None,
                accepted_text=request_part,
            )
            if draft.editing_field:
                if draft.edit_mode == Draft.EditMode.APPEND:
                    draft.values["request"] = _request_from_parts(draft)
                draft.step = "review"
            else:
                draft.values["request"] = _request_from_parts(draft)
                draft.step = "request"
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
            if receipt.lead is None:
                raise LeadDeleted("Submitted lead has been deleted")
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
        if (
            draft.step != Draft.Step.REVIEW
            or draft.pending_action
            or draft.pending_inputs
            or draft.needs_correction
        ):
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
            draft.step = Draft.Step.REQUEST
        elif draft.step == Draft.Step.CONTACTS:
            draft.step = Draft.Step.NAME
            draft.contact_index = None
            draft.edit_mode = Draft.EditMode.NONE
        elif draft.step == Draft.Step.CONTACT_CHOICE:
            draft.step = Draft.Step.CONTACTS
            draft.contact_index = len(draft.values.get("contacts", [])) - 1
            draft.edit_mode = Draft.EditMode.CONTACT_CHOICE
        elif draft.step == Draft.Step.DIRECTION:
            if draft.editing_field == "direction":
                draft.step = Draft.Step.REVIEW
                _finish_edit(draft)
            else:
                raise StaleDraft("There is no earlier dialogue step")
        elif draft.step == Draft.Step.REQUEST:
            if draft.editing_field == "request":
                draft.step = Draft.Step.REVIEW
                _finish_edit(draft)
            else:
                draft.step = Draft.Step.DIRECTION
        elif draft.step == Draft.Step.REVIEW:
            draft.step = Draft.Step.CONTACT_CHOICE
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
            if draft.editing_field == "direction":
                draft.step = Draft.Step.REVIEW
                _finish_edit(draft)
            else:
                if not draft.values.get("directions"):
                    raise StaleDraft("Choose at least one direction before continuing")
                draft.step = Draft.Step.REQUEST
        elif draft.step == Draft.Step.REQUEST:
            if draft.editing_field == "request":
                draft.step = Draft.Step.REVIEW
                _finish_edit(draft)
            else:
                if not draft.values.get("request"):
                    raise StaleDraft("Add a description before continuing")
                draft.step = Draft.Step.NAME
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
        if draft.pending_inputs:
            raise StaleDraft("Decide whether to add the pending text before editing the review")
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
        source = _contact_source_for_index(draft, contact_index)
        if source:
            source.active = False
            source.editing_enabled = False
            source.save(update_fields=["active", "editing_enabled"])
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
    draft.review_ui = {}
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
        current_questions = {draft.question_id}
        if (
            draft.step == Draft.Step.REQUEST
            and not draft.editing_field
            and draft.active_control_ids
        ):
            current_questions.add(draft.active_control_ids[-1])
        if event["reply_to"] not in current_questions:
            raise StaleDraft("Reply belongs to an earlier question")
    if event.get("reply_to") is None or event.get("kind") == "contact":
        if not event.get("date") or not draft.question_date or event["date"] <= draft.question_date:
            raise StaleDraft("Message cannot be assigned to the current question")

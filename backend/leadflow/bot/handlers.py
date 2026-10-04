"""Transactional Telegram updates and persistent response delivery."""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from itertools import batched
from uuid import UUID

from aiogram.types import ForceReply
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import KeyboardButton
from aiogram.types import ReplyKeyboardMarkup
from aiogram.types import ReplyKeyboardRemove
from aiogram.types import Update
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils import timezone

from leadflow.crm.models import SYSTEM_TAGS
from leadflow.crm.models import SubmissionReceipt
from leadflow.crm.services import StaleDraft
from leadflow.crm.services import SubmissionForbidden
from leadflow.crm.validation import InputError

from .models import BotPollingState
from .models import BotUser
from .models import Draft
from .models import DraftInput
from .models import OutboundMessage
from .models import ProcessedUpdate
from .services import append_request_part
from .services import append_review_input
from .services import begin_edit
from .services import choose_username
from .services import confirm_draft_action
from .services import continue_directions
from .services import continue_request
from .services import decide_review_input
from .services import edit_input_message
from .services import get_dialogue
from .services import go_back
from .services import keep_current_value
from .services import keep_draft
from .services import prepare_confirmation
from .services import remove_contact
from .services import remove_request_part
from .services import request_draft_action
from .services import resolve_request_part
from .services import resume_draft
from .services import set_field
from .services import start_draft

logger = logging.getLogger(__name__)

_ACTION_CODES = {
    "new": "N",
    "resume": "R",
    "restart": "S",
    "confirm_restart": "Y",
    "cancel": "C",
    "confirm_cancel": "X",
    "continue": "T",
    "back": "B",
    "keep": "K",
    "edit_name": "EN",
    "edit_contacts": "EC",
    "edit_contact": "E",
    "remove_contact": "D",
    "replace_request": "RR",
    "append_request": "A",
    "add_contact": "AC",
    "continue_contacts": "CC",
    "direction": "G",
    "continue_directions": "GD",
    "continue_request": "GR",
    "username": "U",
    "review_add": "RA",
    "review_discard": "RD",
    "remove_part": "RP",
    "discard_part": "DP",
    "confirm": "OK",
}
_CODE_ACTIONS = {code: action for action, code in _ACTION_CODES.items()}
_FIELD_LABELS = {
    "name": "Имя",
    "contacts": "Контакты",
    "direction": "Направление",
    "request": "Запрос",
}
_MAX_TELEGRAM_TEXT = 3900
_MAX_INLINE_BUTTONS = 300
_MAX_RETRY_ATTEMPTS = 32767


@dataclass(frozen=True)
class UpdateResult:
    callback_query_id: str | None = None
    callback_notice: str | None = None
    duplicate: bool = False


def make_callback(action, submission_id=None, revision=None, value=None):
    code = _ACTION_CODES.get(action)
    if not code:
        raise ValueError("Unknown callback action")
    parts = [code]
    if submission_id is not None:
        parts.extend((str(submission_id).replace("-", ""), str(revision)))
    if value is not None:
        parts.append(str(value))
    data = ":".join(parts)
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Telegram callback data exceeds 64 bytes")
    return data


def process_update(bot_id, incoming):
    """Commit the update, its dialogue transition, replies and next offset together."""
    update = incoming if isinstance(incoming, Update) else Update.model_validate(incoming)
    with transaction.atomic():
        BotPollingState.objects.get_or_create(bot_id=bot_id)
        polling = BotPollingState.objects.select_for_update().get(pk=bot_id)
        event, created = ProcessedUpdate.objects.get_or_create(
            polling_state=polling,
            update_id=update.update_id,
        )
        # One sequential poller follows the current server sequence, including replays.
        # Telegram can choose a lower update ID after a week without new events.
        polling.next_offset = update.update_id + 1
        polling.save(update_fields=["next_offset"])
        callback = update.callback_query
        if not created:
            return UpdateResult(
                callback.id if callback else None,
                "Это действие уже обработано." if callback else None,
                True,
            )
        if callback:
            notice = _handle_callback(event, callback)
        elif update.message:
            _handle_message(event, update.message)
            notice = None
        elif update.edited_message:
            _handle_edited_message(event, update.edited_message)
            notice = None
        else:
            notice = None
        return UpdateResult(callback.id if callback else None, notice)


def get_polling_offset(bot_id):
    state, _ = BotPollingState.objects.get_or_create(bot_id=bot_id)
    return state.next_offset


def get_pending_message():
    earlier_in_chat = OutboundMessage.objects.filter(
        status=OutboundMessage.Status.PENDING,
        chat_id=OuterRef("chat_id"),
        id__lt=OuterRef("id"),
    )
    return (
        OutboundMessage.objects.filter(
            status=OutboundMessage.Status.PENDING,
            next_attempt_at__lte=timezone.now(),
        )
        .filter(~Exists(earlier_in_chat))
        .order_by("id")
        .values(
            "id",
            "chat_id",
            "text",
            "reply_markup",
            "operation",
            "target_message_id",
            "interactive",
        )
        .first()
    )


def mark_message_delivered(message_id, telegram_message_id, telegram_message_date):
    with transaction.atomic():
        message = OutboundMessage.objects.select_for_update().filter(pk=message_id).first()
        if not message or message.status != OutboundMessage.Status.PENDING:
            return
        delivered_id = (
            telegram_message_id
            if message.operation == OutboundMessage.Operation.SEND
            else message.target_message_id
        )
        if (
            message.operation == OutboundMessage.Operation.SEND
            and message.bind_question
            and message.submission_id
            and message.draft_revision is not None
        ):
            draft = (
                Draft.objects.select_for_update()
                .filter(
                    pk=message.submission_id,
                    user_id=message.chat_id,
                    revision=message.draft_revision,
                    submission_state="collecting",
                    pending_action="",
                )
                .first()
            )
            if draft:
                draft.question_id = telegram_message_id
                draft.question_date = telegram_message_date
                draft.save(update_fields=["question_id", "question_date"])
        if (
            message.operation == OutboundMessage.Operation.SEND
            and message.interactive
            and delivered_id
            and message.submission_id
            and message.draft_revision is not None
        ):
            draft = (
                Draft.objects.select_for_update()
                .filter(
                    pk=message.submission_id,
                    user_id=message.chat_id,
                    revision=message.draft_revision,
                    submission_state="collecting",
                    pending_action="",
                )
                .first()
            )
            if draft:
                controls = list(draft.active_control_ids)
                if delivered_id not in controls:
                    controls.append(delivered_id)
                    draft.active_control_ids = controls
                    draft.save(update_fields=["active_control_ids"])
        message.status = OutboundMessage.Status.DELIVERED
        message.delivered_message_id = delivered_id
        message.text = ""
        message.reply_markup = {}
        message.save(update_fields=["status", "text", "reply_markup", "delivered_message_id"])


def mark_message_failed(message_id, error_name, *, retry_after=None, permanent=False):
    with transaction.atomic():
        message = OutboundMessage.objects.select_for_update().filter(pk=message_id).first()
        if not message or message.status != OutboundMessage.Status.PENDING:
            return
        fields = ["status", "text", "reply_markup", "attempts", "next_attempt_at", "last_error"]
        if permanent:
            message.status = OutboundMessage.Status.FAILED
            message.text = ""
            message.reply_markup = {}
        else:
            message.attempts = min(_MAX_RETRY_ATTEMPTS, message.attempts + 1)
            delay = min(30, 2 ** min(message.attempts - 1, 5))
            if retry_after is not None:
                delay = min(3600, max(delay, int(retry_after)))
            message.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        message.last_error = str(error_name)[:60]
        message.save(update_fields=fields)


def complete_pending_submission(submission_id):
    """Commit the shared lead operation and its success reply in one transaction."""
    from .services import confirm_draft

    with transaction.atomic():
        owner_id = (
            Draft.objects.filter(pk=submission_id, submission_state="pending")
            .values_list("user_id", flat=True)
            .first()
        )
        if owner_id is None:
            return None
        BotUser.objects.select_for_update().filter(pk=owner_id).first()
        draft = (
            Draft.objects.select_for_update()
            .filter(pk=submission_id, submission_state="pending")
            .first()
        )
        if not draft:
            return None
        revision = draft.pending_revision
        event = draft.pending_update
        if revision is None or event is None:
            raise StaleDraft("Pending confirmation has no durable update")
        try:
            result = confirm_draft(draft.user_id, draft.pk, revision)
        except InputError as error:
            draft.submission_state = "collecting"
            draft.pending_revision = None
            draft.pending_update = None
            draft.submission_attempts = 0
            draft.submission_retry_at = None
            draft.revision += 1
            draft.save(
                update_fields=[
                    "submission_state",
                    "pending_revision",
                    "pending_update",
                    "submission_attempts",
                    "submission_retry_at",
                    "revision",
                ]
            )
            _queue_notice(event, draft.user_id, _error_text(error))
            _render_current(event, Draft.objects.get(pk=draft.pk))
            return None
        _queue_notice(
            event,
            draft.user_id,
            "Заявка принята. Спасибо!",
            markup=_keyboard([[("Новая заявка", make_callback("new", draft.pk, revision))]]),
        )
        return result


def defer_pending_submission(submission_id, error_name):
    with transaction.atomic():
        draft = (
            Draft.objects.select_for_update()
            .filter(
                pk=submission_id,
                submission_state="pending",
            )
            .first()
        )
        if not draft:
            return
        draft.submission_attempts = min(_MAX_RETRY_ATTEMPTS, draft.submission_attempts + 1)
        delay = min(30, 2 ** min(draft.submission_attempts - 1, 5))
        draft.submission_retry_at = timezone.now() + timedelta(seconds=delay)
        draft.save(update_fields=["submission_attempts", "submission_retry_at"])
        logger.warning("Pending bot submission deferred after %s", str(error_name)[:60])


def get_retryable_submissions(limit=20):
    return list(
        Draft.objects.filter(submission_state="pending")
        .filter(Q(submission_retry_at__isnull=True) | Q(submission_retry_at__lte=timezone.now()))
        .order_by("submission_id")
        .values_list("submission_id", flat=True)[:limit]
    )


def _handle_message(event, message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    if message.chat.type != "private":
        _queue_notice(event, message.chat.id, "Чтобы оставить заявку, откройте бота в личном чате.")
        return
    text = message.text or ""
    username = message.from_user.username or ""
    BotUser.objects.update_or_create(pk=user_id, defaults={"username": username})
    if _is_command(text, "/start"):
        _handle_start(event, user_id)
        return
    if _is_command(text, "/back"):
        _handle_back(event, user_id)
        return
    if _is_command(text, "/cancel"):
        _handle_action_request(event, user_id, Draft.PendingAction.CANCEL)
        return

    draft = get_dialogue(user_id).draft
    if not draft:
        _render_invitation(event, user_id)
        return
    if draft.submission_state == "pending":
        _render_pending(event, user_id, draft)
        return
    if draft.pending_action:
        _render_action_confirmation(event, draft)
        return
    if draft.step == Draft.Step.PAUSED:
        _render_start_menu(event, draft)
        return
    if draft.step == Draft.Step.REVIEW:
        try:
            updated = append_review_input(
                user_id, draft.pk, draft.revision, text, message.message_id
            )
        except InputError as error:
            _queue_notice(event, user_id, _error_text(error))
        else:
            _render_current(event, updated)
        return
    if draft.step == Draft.Step.CONTACT_CHOICE:
        _queue_notice(event, user_id, "Выберите «Добавить контакт» или «Продолжить».")
        _render_current(event, draft)
        return
    if draft.step == Draft.Step.DIRECTION:
        _queue_notice(event, user_id, "Выберите направление кнопкой ниже.")
        _render_current(event, draft)
        return
    if draft.step == Draft.Step.REQUEST and not draft.editing_field:
        if not text:
            _queue_notice(event, user_id, "Отправьте описание текстом или нажмите «Продолжить».")
            _render_current(event, draft)
            return
        try:
            updated, accepted, _ = append_request_part(
                user_id,
                draft.pk,
                draft.revision,
                text,
                event=_message_binding(message),
                source_message_id=message.message_id,
            )
        except InputError as error:
            _queue_notice(event, user_id, _error_text(error))
            _render_current(event, draft)
        except StaleDraft:
            _queue_notice(
                event, user_id, "Ответ не относится к текущему вопросу. Проверьте шаг ниже."
            )
            _render_current(event, Draft.objects.get(pk=draft.pk))
        else:
            if not accepted:
                _queue_notice(
                    event,
                    user_id,
                    "Эта часть превышает лимит описания. Исправьте исходное сообщение "
                    "или удалите эту часть.",
                )
            _render_request_progress(event, updated)
        return
    if not text and not message.contact:
        _queue_notice(event, user_id, "Ответьте текстом или используйте кнопку своего телефона.")
        _render_current(event, draft)
        return

    value = text
    field = draft.step
    event_binding = _message_binding(message)
    if draft.step == Draft.Step.CONTACTS:
        field = "contacts"
        if message.contact:
            if message.contact.user_id != user_id:
                _queue_notice(event, user_id, "Можно отправить только свой номер.")
                _render_current(event, draft)
                return
            value = message.contact.phone_number
            if not value.startswith("+"):
                value = f"+{value}"
            event_binding["kind"] = "contact"
            event_binding["contact_user_id"] = message.contact.user_id
    elif draft.step == Draft.Step.REQUEST and draft.editing_field == "request":
        field = "append_request" if draft.edit_mode == Draft.EditMode.APPEND else "request"

    try:
        updated = set_field(
            user_id,
            draft.pk,
            draft.revision,
            field,
            value,
            event=event_binding,
        )
    except InputError as error:
        _queue_notice(event, user_id, _error_text(error))
        _render_current(event, Draft.objects.get(pk=draft.pk))
    except StaleDraft, SubmissionForbidden:
        _queue_notice(
            event,
            user_id,
            "Не удалось определить, к какому вопросу относится ответ. Ответьте на текущий вопрос.",
        )
        _render_current(event, Draft.objects.get(pk=draft.pk))
    else:
        _render_transition(event, draft, updated)


def _handle_edited_message(event, message):
    if not message.from_user or message.chat.type != "private":
        return
    user_id = message.from_user.id
    state = BotUser.objects.filter(pk=user_id).first()
    if not state:
        return
    if message.message_id in state.last_submission_message_ids:
        _queue_notice(event, user_id, "Заявка уже принята. Это исправление её не изменило.")
        return
    draft = get_dialogue(user_id).draft
    if not draft:
        return
    if draft.submission_state == "pending":
        if DraftInput.objects.filter(draft=draft, source_message_id=message.message_id).exists():
            _queue_notice(event, user_id, "Отправка уже началась. Изменение не применено.")
        return
    pending_review = any(
        candidate.get("message_id") == message.message_id for candidate in draft.pending_inputs
    )
    editable_input = DraftInput.objects.filter(
        draft=draft,
        source_message_id=message.message_id,
        editing_enabled=True,
    ).exists()
    if not pending_review and not editable_input:
        return
    new_text = message.text or message.caption or ""
    try:
        updated, accepted = edit_input_message(
            user_id, draft.pk, draft.revision, message.message_id, new_text
        )
    except InputError as error:
        _queue_notice(event, user_id, _error_text(error))
        return
    except StaleDraft, SubmissionForbidden:
        _queue_notice(event, user_id, "Изменение не применено. Показываю сохранённые данные.")
        _render_current(event, Draft.objects.filter(user_id=user_id).first())
        return
    if not accepted:
        _queue_notice(
            event,
            user_id,
            "Изменение не применено. Проверьте формат и лимит поля; сохранённые данные "
            "остались прежними.",
        )
    else:
        _queue_notice(event, user_id, "Исправление сохранено в текущей заявке.")
    if updated.step == Draft.Step.REQUEST:
        _render_request_progress(event, updated)
    else:
        _render_current(event, updated)


def _handle_start(event, user_id):
    dialogue = get_dialogue(user_id)
    draft = dialogue.draft
    if not draft:
        if dialogue.last_receipt:
            _render_completed(event, user_id, dialogue.last_receipt.pk)
        else:
            _render_current(event, start_draft(user_id))
    elif draft.submission_state == "pending":
        _render_pending(event, user_id, draft)
    else:
        _render_start_menu(event, draft)


def _handle_back(event, user_id):
    draft = get_dialogue(user_id).draft
    if not draft:
        _render_invitation(event, user_id)
        return
    if draft.submission_state == "pending":
        _render_pending(event, user_id, draft)
        return
    try:
        updated = go_back(user_id, draft.pk, draft.revision)
    except StaleDraft:
        _queue_notice(event, user_id, "Сейчас вернуться назад нельзя.")
        _render_current(event, Draft.objects.get(pk=draft.pk))
        return
    _render_transition(event, draft, updated)


def _handle_action_request(event, user_id, action, submission_id=None, revision=None):
    draft = get_dialogue(user_id).draft
    if not draft:
        _render_invitation(event, user_id)
        return
    if draft.submission_state == "pending":
        _render_pending(event, user_id, draft)
        return
    if submission_id and str(draft.pk) != str(submission_id):
        _render_start_menu(event, draft)
        return
    _clear_active_controls(event, draft)
    try:
        result = request_draft_action(
            user_id,
            draft.pk,
            revision if revision is not None else draft.revision,
            action,
        )
    except StaleDraft:
        _render_current(event, Draft.objects.get(pk=draft.pk))
        return
    if result is None:
        _queue_notice(event, user_id, "Черновик удалён. Можно начать новую заявку.")
        _render_invitation(event, user_id)
    elif action == Draft.PendingAction.RESTART and not result.values:
        _render_current(event, result)
    else:
        _render_action_confirmation(event, result)


def _handle_callback(event, callback):
    actor_id = callback.from_user.id
    if callback.message is None:
        return "Кнопка устарела. Напишите /start."
    chat_id = callback.message.chat.id
    if callback.message.chat.type != "private":
        _queue_notice(event, chat_id, "Откройте бота в личном чате.")
        return None
    parsed = _parse_callback(callback.data)
    if not parsed:
        _queue_edit_markup(event, actor_id, callback.message.message_id, None)
        _queue_notice(event, actor_id, "Эта кнопка больше не действует.")
        return "Кнопка устарела."
    action, submission_id, revision, value = parsed
    if action == "new":
        _queue_edit_markup(event, actor_id, callback.message.message_id, None)
        draft = get_dialogue(actor_id).draft
        if draft and draft.submission_state == "pending":
            _render_pending(event, actor_id, draft)
        elif draft:
            _render_start_menu(event, draft)
        else:
            _render_current(event, start_draft(actor_id))
        return None

    if action == "confirm" and submission_id:
        receipt = (
            SubmissionReceipt.objects.filter(
                pk=submission_id,
                channel="telegram_bot",
                owner_id=actor_id,
            )
            .select_related("lead")
            .first()
        )
        if receipt:
            _queue_edit_markup(event, actor_id, callback.message.message_id, None)
            _queue_notice(
                event,
                actor_id,
                "Эта заявка уже принята. Спасибо!",
                markup=_keyboard(
                    [[("Новая заявка", make_callback("new", submission_id, revision))]]
                ),
            )
            return "Заявка уже принята."

    draft = get_dialogue(actor_id).draft
    if not draft or (submission_id and str(draft.pk) != str(submission_id)):
        _queue_edit_markup(event, actor_id, callback.message.message_id, None)
        _render_start_menu(event, draft) if draft else _render_invitation(event, actor_id)
        return "Кнопка устарела."
    if draft.submission_state == "pending":
        _render_pending(event, actor_id, draft)
        return "Проверяем отправку."
    if revision is not None and draft.revision != revision:
        _queue_edit_markup(event, actor_id, callback.message.message_id, None)
        if callback.message.message_id in draft.active_control_ids:
            draft.active_control_ids = [
                message_id
                for message_id in draft.active_control_ids
                if message_id != callback.message.message_id
            ]
            draft.save(update_fields=["active_control_ids"])
        _queue_notice(event, actor_id, "Эта кнопка устарела. Показываю текущий шаг.")
        _render_current(event, draft)
        return "Кнопка устарела."
    try:
        return _apply_callback_action(event, actor_id, draft, action, value)
    except InputError as error:
        _queue_notice(event, actor_id, _error_text(error))
        current = Draft.objects.filter(pk=draft.pk).first()
        if current:
            _render_current(event, current)
        return "Проверьте данные."
    except StaleDraft, SubmissionForbidden:
        _queue_edit_markup(event, actor_id, callback.message.message_id, None)
        current = Draft.objects.filter(user_id=actor_id).first()
        if current:
            _queue_notice(event, actor_id, "Кнопка устарела. Показываю текущий шаг.")
            _render_current(event, current)
        else:
            _queue_notice(event, actor_id, "Черновик уже завершён или отменён.")
        return "Кнопка устарела."


def _apply_callback_action(event, user_id, draft, action, value):
    if action == "resume":
        updated = (
            resume_draft(user_id, draft.pk, draft.revision)
            if draft.step == Draft.Step.PAUSED
            else draft
        )
        _render_current(event, updated)
    elif action in {"restart", "cancel"}:
        intent = Draft.PendingAction.RESTART if action == "restart" else Draft.PendingAction.CANCEL
        _handle_action_request(event, user_id, intent, draft.pk, draft.revision)
    elif action in {"confirm_restart", "confirm_cancel"}:
        _clear_active_controls(event, draft)
        replacement = confirm_draft_action(user_id, draft.pk, draft.revision)
        if action == "confirm_restart":
            _render_current(event, replacement)
        else:
            _queue_notice(event, user_id, "Черновик удалён. Можно начать новую заявку.")
            _render_invitation(event, user_id)
    elif action == "continue":
        _render_current(event, keep_draft(user_id, draft.pk, draft.revision))
    elif action == "back":
        _render_transition(event, draft, go_back(user_id, draft.pk, draft.revision))
    elif action == "keep":
        _render_transition(event, draft, keep_current_value(user_id, draft.pk, draft.revision))
    elif action in {
        "edit_name",
        "edit_contacts",
        "edit_contact",
        "replace_request",
        "append_request",
    }:
        field = {
            "edit_name": "name",
            "edit_contacts": "contacts",
            "edit_contact": "contacts",
            "replace_request": "request",
            "append_request": "request",
        }[action]
        updated = begin_edit(
            user_id,
            draft.pk,
            draft.revision,
            field,
            contact_index=value if action == "edit_contact" else None,
            mode="append" if action == "append_request" else "replace",
        )
        _render_current(event, updated)
    elif action == "remove_contact":
        _render_current(event, remove_contact(user_id, draft.pk, draft.revision, value))
    elif action == "username":
        _render_transition(
            event,
            draft,
            choose_username(user_id, draft.pk, draft.revision),
        )
    elif action == "continue_directions":
        _render_current(event, continue_directions(user_id, draft.pk, draft.revision))
    elif action == "continue_request":
        _render_current(event, continue_request(user_id, draft.pk, draft.revision))
    elif action == "discard_part":
        _render_current(
            event,
            resolve_request_part(user_id, draft.pk, draft.revision, value, keep=False),
        )
    elif action == "remove_part":
        _render_current(event, remove_request_part(user_id, draft.pk, draft.revision, value))
    elif action in {"review_add", "review_discard"}:
        try:
            updated = decide_review_input(
                user_id, draft.pk, draft.revision, add=action == "review_add"
            )
        except InputError as error:
            _queue_notice(event, user_id, _error_text(error))
        else:
            _render_current(event, updated)
    elif action in {"add_contact", "continue_contacts"}:
        if draft.step == Draft.Step.REVIEW and action == "add_contact":
            updated = begin_edit(user_id, draft.pk, draft.revision, "contacts")
        else:
            updated = set_field(user_id, draft.pk, draft.revision, action, None)
        _render_current(event, updated)
    elif action == "direction":
        if value == "edit":
            updated = begin_edit(user_id, draft.pk, draft.revision, "direction")
            _render_current(event, updated)
        else:
            updated = set_field(user_id, draft.pk, draft.revision, "direction", value)
            if draft.active_control_ids:
                _render_direction_progress(event, updated, draft.active_control_ids)
            else:
                _render_current(event, updated)
    elif action == "confirm":
        pending = prepare_confirmation(user_id, draft.pk, draft.revision, event)
        _render_pending(event, user_id, pending)
    else:
        raise StaleDraft("Unknown action")
    return None


def _render_invitation(event, chat_id):
    _queue_text(
        event,
        chat_id,
        (
            "Здравствуйте! Я помогу отправить заявку в агентство. "
            "Укажите имя, контакты и запрос. Перед отправкой проверьте данные."
        ),
        markup=_keyboard([[("Оставить заявку", make_callback("new"))]]),
    )


def _render_completed(event, chat_id, submission_id):
    _queue_text(
        event,
        chat_id,
        "Ваша последняя заявка уже принята. Если нужно, можно отправить ещё одну.",
        markup=_keyboard([[("Новая заявка", make_callback("new", submission_id, 0))]]),
    )


def _render_start_menu(event, draft):
    _clear_active_controls(event, draft)
    if draft.pending_action:
        _render_action_confirmation(event, draft)
        return
    actions = [
        [("Продолжить", make_callback("resume", draft.pk, draft.revision))],
        [("Начать заново", make_callback("restart", draft.pk, draft.revision))],
        [("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))],
    ]
    if draft.step == Draft.Step.CONTACTS:
        _queue_notice(
            event,
            draft.user_id,
            "Клавиатура телефона скрыта. Выберите действие для черновика.",
            markup=ReplyKeyboardRemove(),
        )
    _queue_text(
        event,
        draft.user_id,
        "У вас есть незавершённая заявка. Сохранённые данные:\n\n" + _summary(draft),
        markup=_keyboard(actions),
        draft=draft,
    )


def _render_action_confirmation(event, draft):
    _clear_active_controls(event, draft)
    if draft.pending_action == Draft.PendingAction.CANCEL:
        text = "Удалить незавершённую заявку? Введённые данные будут потеряны."
        action, button = "confirm_cancel", "Удалить заявку"
    else:
        text = "Удалить прежнюю заявку и начать заново? Её данные будут потеряны."
        action, button = "confirm_restart", "Удалить и начать заново"
    actions = [
        [(button, make_callback(action, draft.pk, draft.revision))],
        [("Продолжить заполнение", make_callback("continue", draft.pk, draft.revision))],
    ]
    if draft.step == Draft.Step.CONTACTS:
        _queue_notice(event, draft.user_id, text, markup=ReplyKeyboardRemove(), draft=draft)
        text = "Выберите действие."
    _queue_notice(event, draft.user_id, text, markup=_keyboard(actions), draft=draft)


def _render_pending(event, user_id, draft=None):
    draft = draft or get_dialogue(user_id).draft
    if draft and draft.submission_state == "pending":
        _clear_active_controls(event, draft)
        _queue_notice(
            event, user_id, "Проверяем отправку. Вводить данные заново не нужно.", draft=draft
        )


def _render_transition(event, previous, updated):
    if previous.step == Draft.Step.CONTACTS and updated.step != Draft.Step.CONTACTS:
        _queue_notice(
            event, previous.user_id, "Продолжаем заполнение.", markup=ReplyKeyboardRemove()
        )
    _render_current(event, updated)


def _clear_active_controls(event, draft):
    message_ids = list(draft.active_control_ids)
    if not message_ids:
        return
    draft.active_control_ids = []
    draft.save(update_fields=["active_control_ids"])
    for message_id in message_ids:
        _queue_edit_markup(event, draft.user_id, message_id, None, draft=draft)


def _render_request_progress(event, draft):
    question_id = draft.question_id
    if question_id is None:
        _render_prompt(event, draft)
        return
    part_count = DraftInput.objects.filter(
        draft=draft, field=DraftInput.Field.REQUEST, active=True
    ).count()
    length = len(draft.values.get("request", ""))
    if draft.needs_correction:
        prompt = (
            "Часть не добавлена. Исправьте исходное сообщение или удалите её кнопкой ниже. "
            f"Сохранено фрагментов: {part_count}, {length}/2000 символов."
        )
    else:
        prompt = (
            f"Описание сохранено. Фрагментов: {part_count}; {length}/2000 символов. "
            "Отправьте ещё часть или нажмите «Продолжить»."
        )
    actions = _request_actions(draft)
    actions.append([("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))])
    keyboard = _keyboard(actions)
    _queue_edit_text(event, draft.user_id, question_id, prompt, markup=keyboard, draft=draft)


def _request_actions(draft):
    actions = []
    if draft.needs_correction:
        pending_positions = list(
            DraftInput.objects.filter(
                draft=draft,
                field=DraftInput.Field.REQUEST,
                pending_text__gt="",
                active=False,
            ).values_list("position", flat=True)
        )
        for position in pending_positions:
            actions.append(
                [
                    (
                        "Удалить эту часть",
                        make_callback("discard_part", draft.pk, draft.revision, position),
                    )
                ]
            )
    elif draft.values.get("request"):
        actions.append(
            [("Продолжить", make_callback("continue_request", draft.pk, draft.revision))]
        )
    if draft.editing_field == "request" or draft.values.get("directions"):
        actions.append([("Назад", make_callback("back", draft.pk, draft.revision))])
    return actions


def _direction_keyboard(draft):
    selected = set(draft.values.get("directions", []))
    actions = []
    for code, label in SYSTEM_TAGS.items():
        prefix = "✓ " if code in selected else ""
        actions.append(
            [
                (
                    f"{prefix}{label}",
                    make_callback("direction", draft.pk, draft.revision, code),
                )
            ]
        )
    actions.append([("Продолжить", make_callback("continue_directions", draft.pk, draft.revision))])
    if draft.editing_field == "direction":
        actions.append([("Назад", make_callback("back", draft.pk, draft.revision))])
    actions.append([("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))])
    return _keyboard(actions)


def _render_direction_progress(event, draft, message_ids):
    message_id = message_ids[-1]
    for stale_id in message_ids[:-1]:
        _queue_edit_markup(event, draft.user_id, stale_id, None, draft=draft)
    draft.active_control_ids = [message_id]
    draft.save(update_fields=["active_control_ids"])
    _queue_edit_text(
        event,
        draft.user_id,
        message_id,
        "Выберите одно или несколько направлений. Можно отметить несколько вариантов.",
        markup=_direction_keyboard(draft),
        draft=draft,
    )


def _render_current(event, draft):
    if draft is None:
        return
    _clear_active_controls(event, draft)
    if draft.submission_state == "pending":
        _render_pending(event, draft.user_id, draft)
    elif draft.pending_action:
        _render_action_confirmation(event, draft)
    elif draft.step == Draft.Step.PAUSED:
        _render_start_menu(event, draft)
    elif draft.step == Draft.Step.CONTACT_CHOICE:
        contacts = "\n".join(
            f"{index + 1}. {value}" for index, value in enumerate(draft.values.get("contacts", []))
        )
        actions = [
            [("Добавить контакт", make_callback("add_contact", draft.pk, draft.revision))],
            [("Продолжить", make_callback("continue_contacts", draft.pk, draft.revision))],
            [("Назад", make_callback("back", draft.pk, draft.revision))],
            [("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))],
        ]
        _queue_text(
            event,
            draft.user_id,
            f"Контакты:\n{contacts}\n\nДобавьте контакт или продолжите.",
            markup=_keyboard(actions),
            draft=draft,
        )
    elif draft.step == Draft.Step.DIRECTION:
        _queue_notice(
            event,
            draft.user_id,
            "Выберите одно или несколько направлений. Можно отметить несколько вариантов.",
            markup=_direction_keyboard(draft),
            draft=draft,
        )
    elif draft.step == Draft.Step.REVIEW:
        _render_review(event, draft)
    else:
        _render_prompt(event, draft)


def _render_prompt(event, draft):
    label = _FIELD_LABELS[draft.step]
    prompt = {
        Draft.Step.NAME: "Как к вам обращаться? Напишите имя.",
        Draft.Step.CONTACTS: (
            "Как с вами связаться? Нажмите «Отправить мой номер» или напишите телефон "
            "с + и кодом страны, email, @username или ссылку t.me."
        ),
        Draft.Step.REQUEST: "Опишите, что нужно сделать. Можно отправить несколько сообщений.",
    }[draft.step]
    value = _current_value(draft, draft.step)
    if draft.step == Draft.Step.REQUEST:
        actions = _request_actions(draft)
    else:
        actions = []
        actions.append(
            [
                (
                    "Назад",
                    make_callback("back", draft.pk, draft.revision),
                )
            ]
        )
        if value is not None:
            actions.append([("Оставить как есть", make_callback("keep", draft.pk, draft.revision))])
    if draft.step == Draft.Step.CONTACTS:
        username = (
            BotUser.objects.filter(pk=draft.user_id).values_list("username", flat=True).first()
        )
        draft.last_username_offer = username or ""
        draft.save(update_fields=["last_username_offer"])
        if username and f"@{username}" not in draft.values.get("contacts", []):
            actions.insert(
                0,
                [
                    (
                        f"Использовать @{username}",
                        make_callback("username", draft.pk, draft.revision),
                    )
                ],
            )
    actions.append([("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))])
    if draft.step == Draft.Step.REQUEST:
        if value is not None:
            prompt = f"Текущее описание:\n{value}\n\n{prompt}"
        _queue_notice(
            event,
            draft.user_id,
            prompt,
            markup=_keyboard(actions),
            draft=draft,
            bind_question=True,
        )
        return
    _queue_notice(
        event,
        draft.user_id,
        f"Действия для шага «{label}».",
        markup=_keyboard(actions),
        draft=draft,
    )
    markup = ForceReply(selective=True, input_field_placeholder=label)
    if draft.step == Draft.Step.CONTACTS:
        markup = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
            resize_keyboard=True,
            is_persistent=True,
            one_time_keyboard=True,
            input_field_placeholder=label,
            force_reply=True,
        )
    if value is not None:
        prompt = f"Текущее значение:\n{value}\n\n{prompt}"
    _queue_notice(
        event,
        draft.user_id,
        prompt,
        markup=markup,
        draft=draft,
        bind_question=True,
    )


def _render_review(event, draft):
    values = draft.values
    contacts = values.get("contacts", [])
    directions = values.get("directions", [])
    direction_labels = [SYSTEM_TAGS[code] for code in directions if code in SYSTEM_TAGS]
    request_parts = list(
        DraftInput.objects.filter(
            draft=draft, field=DraftInput.Field.REQUEST, active=True
        ).order_by("position")
    )
    body = (
        f"Имя: {values.get('name', '')}\n"
        "Контакты:\n"
        + (
            "\n".join(f"{index + 1}. {value}" for index, value in enumerate(contacts))
            or "Не указаны"
        )
        + f"\nНаправления: {', '.join(direction_labels) or 'Не выбрано'}"
        + f"\nЗапрос:\n{values.get('request', '')}"
    )
    actions = [
        [("Изменить имя", make_callback("edit_name", draft.pk, draft.revision))],
        [("Изменить контакты", make_callback("edit_contacts", draft.pk, draft.revision))],
    ]
    for index, contact in enumerate(contacts):
        actions.append(
            [
                (
                    f"Изменить {index + 1}: {contact[:28]}",
                    make_callback("edit_contact", draft.pk, draft.revision, index),
                ),
                ("Удалить", make_callback("remove_contact", draft.pk, draft.revision, index)),
            ]
        )
    if not contacts:
        actions.append(
            [("Добавить контакт", make_callback("add_contact", draft.pk, draft.revision))]
        )
        body += "\n\nДобавьте хотя бы один контакт перед отправкой."
    for index, part in enumerate(request_parts):
        actions.append(
            [
                (
                    f"Удалить часть запроса {index + 1}",
                    make_callback("remove_part", draft.pk, draft.revision, part.position),
                )
            ]
        )
    if draft.pending_inputs:
        pending_text = "\n\n".join(item["text"] for item in draft.pending_inputs)
        body += f"\n\nНовое сообщение:\n{pending_text}\n\nДобавить его к описанию заявки?"
        actions.append(
            [
                (
                    "Добавить к описанию",
                    make_callback("review_add", draft.pk, draft.revision),
                ),
                (
                    "Не добавлять",
                    make_callback("review_discard", draft.pk, draft.revision),
                ),
            ]
        )
    actions.extend(
        [
            [
                (
                    "Изменить направление",
                    make_callback("direction", draft.pk, draft.revision, "edit"),
                )
            ],
            [("Заменить запрос", make_callback("replace_request", draft.pk, draft.revision))],
            [("Дополнить запрос", make_callback("append_request", draft.pk, draft.revision))],
            [("Назад", make_callback("back", draft.pk, draft.revision))],
            [("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))],
        ]
    )
    can_submit = bool(contacts and values.get("name") and values.get("request") and directions)
    can_submit = can_submit and not draft.pending_inputs and not draft.needs_correction
    if can_submit:
        actions.append(
            [("Подтвердить отправку", make_callback("confirm", draft.pk, draft.revision))]
        )
    else:
        body += (
            "\n\nЗаполните обязательные данные и решите вопрос с новым сообщением перед отправкой."
        )
    # Each review row has at most two buttons; reserve cancel/confirm for the last keyboard.
    action_batches = [
        list(batch) for batch in batched(actions[:-2], _MAX_INLINE_BUTTONS // 2 - 2, strict=False)
    ]
    action_batches[-1].extend(actions[-2:])
    _queue_text(event, draft.user_id, body, markup=_keyboard(action_batches[0]), draft=draft)
    for number, batch in enumerate(action_batches[1:], start=2):
        _queue_notice(
            event,
            draft.user_id,
            f"Проверка заявки: действия {number} из {len(action_batches)}.",
            markup=_keyboard(batch),
            draft=draft,
        )


def _summary(draft):
    values = draft.values
    contacts = values.get("contacts", [])
    return (
        f"Имя: {values.get('name', 'Не указано')}\n"
        "Контакты:\n"
        + (
            "\n".join(f"{index + 1}. {value}" for index, value in enumerate(contacts))
            or "Не указаны"
        )
        + "\nНаправления: "
        + (
            ", ".join(
                SYSTEM_TAGS[code] for code in values.get("directions", []) if code in SYSTEM_TAGS
            )
            or "Не выбрано"
        )
        + f"\nЗапрос:\n{values.get('request', 'Не указан')}"
    )


def _current_value(draft, field):
    if field == Draft.Step.CONTACTS:
        contacts = draft.values.get("contacts", [])
        if draft.contact_index is not None and 0 <= draft.contact_index < len(contacts):
            return contacts[draft.contact_index]
        return None
    return draft.values.get(field)


def _queue_text(event, chat_id, text, *, markup=None, draft=None, bind_question=False):
    chunks = []
    while text:
        end = min(len(text), _MAX_TELEGRAM_TEXT)
        if len(text) > end:
            newline = text.rfind("\n", 0, end)
            if newline > 0:
                end = newline + 1
        chunks.append(text[:end])
        text = text[end:]
    for index, chunk in enumerate(chunks):
        _queue_notice(
            event,
            chat_id,
            chunk,
            markup=markup if index == len(chunks) - 1 else None,
            draft=draft,
            bind_question=bind_question and index == len(chunks) - 1,
        )


def _queue_notice(event, chat_id, text, *, markup=None, draft=None, bind_question=False):
    markup_data = markup.model_dump(mode="json", exclude_none=True) if markup is not None else {}
    _queue_outbound(
        event,
        chat_id,
        text,
        markup_data,
        draft=draft,
        bind_question=bind_question,
        interactive="inline_keyboard" in markup_data,
    )


def _queue_edit_text(event, chat_id, message_id, text, *, markup=None, draft=None):
    markup_data = markup.model_dump(mode="json", exclude_none=True) if markup is not None else {}
    _queue_outbound(
        event,
        chat_id,
        text,
        markup_data,
        draft=draft,
        operation=OutboundMessage.Operation.EDIT_TEXT,
        target_message_id=message_id,
    )


def _queue_edit_markup(event, chat_id, message_id, markup, *, draft=None):
    markup_data = markup.model_dump(mode="json", exclude_none=True) if markup is not None else {}
    _queue_outbound(
        event,
        chat_id,
        "",
        markup_data,
        draft=draft,
        operation=OutboundMessage.Operation.EDIT_MARKUP,
        target_message_id=message_id,
    )


def _queue_outbound(
    event,
    chat_id,
    text,
    markup_data,
    *,
    draft=None,
    bind_question=False,
    interactive=False,
    operation=OutboundMessage.Operation.SEND,
    target_message_id=None,
):
    ordinal = event.next_ordinal
    event.next_ordinal += 1
    event.save(update_fields=["next_ordinal"])
    OutboundMessage.objects.create(
        processed_update=event,
        ordinal=ordinal,
        chat_id=chat_id,
        text=text,
        reply_markup=markup_data,
        operation=operation,
        target_message_id=target_message_id,
        interactive=interactive,
        submission_id=draft.pk if draft else None,
        draft_revision=draft.revision if draft else None,
        bind_question=bind_question,
        next_attempt_at=timezone.now(),
    )


def _keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


def _message_binding(message):
    return {
        "chat_id": message.chat.id,
        "date": message.date,
        "message_id": message.message_id,
        "reply_to": message.reply_to_message.message_id if message.reply_to_message else None,
        "kind": "text",
    }


def _parse_callback(data):
    if not isinstance(data, str) or len(data.encode("utf-8")) > 64:
        return None
    parts = data.split(":")
    action = _CODE_ACTIONS.get(parts[0]) if parts else None
    if not action:
        return None
    if action == "new" and len(parts) == 1:
        return action, None, None, None
    if len(parts) not in {3, 4}:
        return None
    if not re.fullmatch(r"[a-f0-9]{32}", parts[1]) or not parts[2].isdigit():
        return None
    try:
        submission_id = UUID(hex=parts[1])
        revision = int(parts[2])
        if action in {"edit_contact", "remove_contact", "remove_part", "discard_part"}:
            value = int(parts[3]) if len(parts) == 4 else None
            if value is None:
                return None
        else:
            value = parts[3] if len(parts) == 4 else None
    except ValueError, TypeError:
        return None
    return action, submission_id, revision, value


def _is_command(text, command):
    parts = text.split(maxsplit=1)
    if not parts:
        return False
    first = parts[0].split("@", maxsplit=1)[0]
    return first.casefold() == command


def _error_text(error):
    messages = []
    for field_errors in error.field_errors.values():
        messages.extend(str(message) for message in field_errors)
    detail = " ".join(dict.fromkeys(messages)) or "Проверьте значение."
    return f"Не удалось принять ответ. {detail}"

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
from .models import OutboundMessage
from .models import ProcessedUpdate
from .services import begin_edit
from .services import confirm_draft_action
from .services import get_dialogue
from .services import go_back
from .services import keep_current_value
from .services import keep_draft
from .services import prepare_confirmation
from .services import remove_contact
from .services import request_draft_action
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
        .values("id", "chat_id", "text", "reply_markup")
        .first()
    )


def mark_message_delivered(message_id, telegram_message_id, telegram_message_date):
    with transaction.atomic():
        message = OutboundMessage.objects.select_for_update().filter(pk=message_id).first()
        if not message or message.status != OutboundMessage.Status.PENDING:
            return
        if message.bind_question and message.submission_id and message.draft_revision is not None:
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
        message.status = OutboundMessage.Status.DELIVERED
        message.text = ""
        message.reply_markup = {}
        message.save(update_fields=["status", "text", "reply_markup"])


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
        _queue_notice(event, user_id, "Проверьте данные с помощью кнопок ниже.")
        _render_current(event, draft)
        return
    if draft.step == Draft.Step.CONTACT_CHOICE:
        _queue_notice(event, user_id, "Выберите «Добавить контакт» или «Продолжить».")
        _render_current(event, draft)
        return
    if draft.step == Draft.Step.DIRECTION:
        _queue_notice(event, user_id, "Выберите направление кнопкой ниже.")
        _render_current(event, draft)
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


def _handle_start(event, user_id):
    dialogue = get_dialogue(user_id)
    draft = dialogue.draft
    if not draft:
        if dialogue.last_receipt:
            _render_completed(event, user_id, dialogue.last_receipt.pk)
        else:
            _render_invitation(event, user_id)
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
        _queue_notice(event, actor_id, "Эта кнопка больше не действует.")
        return "Кнопка устарела."
    action, submission_id, revision, value = parsed
    if action == "new":
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
        _render_start_menu(event, draft) if draft else _render_invitation(event, actor_id)
        return "Кнопка устарела."
    if draft.submission_state == "pending":
        _render_pending(event, actor_id, draft)
        return "Проверяем отправку."
    if revision is not None and draft.revision != revision:
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
    elif action in {"add_contact", "continue_contacts"}:
        if draft.step == Draft.Step.REVIEW and action == "add_contact":
            updated = begin_edit(user_id, draft.pk, draft.revision, "contacts")
        else:
            updated = set_field(user_id, draft.pk, draft.revision, action, None)
        _render_current(event, updated)
    elif action == "direction":
        if value == "edit":
            updated = begin_edit(user_id, draft.pk, draft.revision, "direction")
        else:
            updated = set_field(user_id, draft.pk, draft.revision, "direction", value)
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
    if draft.pending_action:
        _render_action_confirmation(event, draft)
        return
    actions = [
        [("Продолжить", make_callback("resume", draft.pk, draft.revision))],
        [("Начать заново", make_callback("restart", draft.pk, draft.revision))],
        [("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))],
    ]
    _queue_text(
        event,
        draft.user_id,
        "У вас есть незавершённая заявка. Сохранённые данные:\n\n" + _summary(draft),
        markup=ReplyKeyboardRemove() if draft.step == Draft.Step.CONTACTS else None,
    )
    _queue_notice(
        event, draft.user_id, "Выберите действие.", markup=_keyboard(actions), draft=draft
    )


def _render_action_confirmation(event, draft):
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
        _queue_notice(
            event, user_id, "Проверяем отправку. Вводить данные заново не нужно.", draft=draft
        )


def _render_transition(event, previous, updated):
    if previous.step == Draft.Step.CONTACTS and updated.step != Draft.Step.CONTACTS:
        _queue_notice(
            event, previous.user_id, "Продолжаем заполнение.", markup=ReplyKeyboardRemove()
        )
    _render_current(event, updated)


def _render_current(event, draft):
    if draft is None:
        return
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
        actions = [
            [(label, make_callback("direction", draft.pk, draft.revision, code))]
            for code, label in SYSTEM_TAGS.items()
        ]
        actions.extend(
            [
                [("Назад", make_callback("back", draft.pk, draft.revision))],
                [("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))],
            ]
        )
        if draft.values.get("direction"):
            actions.insert(
                -1,
                [("Оставить как есть", make_callback("keep", draft.pk, draft.revision))],
            )
        _queue_notice(
            event,
            draft.user_id,
            "Выберите направление услуги. Текущее: "
            + SYSTEM_TAGS.get(draft.values.get("direction"), "не выбрано"),
            markup=_keyboard(actions),
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
        Draft.Step.REQUEST: "Опишите, что нужно сделать.",
    }[draft.step]
    value = _current_value(draft, draft.step)
    actions = [
        [
            (
                "В начало" if draft.step == Draft.Step.NAME else "Назад",
                make_callback("back", draft.pk, draft.revision),
            )
        ]
    ]
    if value is not None:
        actions.append([("Оставить как есть", make_callback("keep", draft.pk, draft.revision))])
    actions.append([("Отменить заявку", make_callback("cancel", draft.pk, draft.revision))])
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
    body = (
        f"Имя: {values.get('name', '')}\n"
        "Контакты:\n"
        + (
            "\n".join(f"{index + 1}. {value}" for index, value in enumerate(contacts))
            or "Не указаны"
        )
        + f"\nНаправление: {SYSTEM_TAGS.get(values.get('direction'), 'Не выбрано')}"
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
            [("Подтвердить отправку", make_callback("confirm", draft.pk, draft.revision))],
        ]
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
        + f"\nНаправление: {SYSTEM_TAGS.get(values.get('direction'), 'Не выбрано')}"
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
    ordinal = event.next_ordinal
    event.next_ordinal += 1
    event.save(update_fields=["next_ordinal"])
    OutboundMessage.objects.create(
        processed_update=event,
        ordinal=ordinal,
        chat_id=chat_id,
        text=text,
        reply_markup=markup_data,
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
        if action in {"edit_contact", "remove_contact"}:
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

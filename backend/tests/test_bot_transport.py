import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.exceptions import TelegramRetryAfter
from aiogram.exceptions import TelegramServerError
from aiogram.types import ReplyKeyboardMarkup
from django.utils import timezone

from leadflow.bot.handlers import complete_pending_submission
from leadflow.bot.handlers import defer_pending_submission
from leadflow.bot.handlers import get_pending_message
from leadflow.bot.handlers import get_polling_offset
from leadflow.bot.handlers import make_callback
from leadflow.bot.handlers import mark_message_delivered
from leadflow.bot.handlers import mark_message_failed
from leadflow.bot.handlers import process_update
from leadflow.bot.models import BotPollingState
from leadflow.bot.models import BotUser
from leadflow.bot.models import Draft
from leadflow.bot.models import DraftInput
from leadflow.bot.models import OutboundMessage
from leadflow.bot.models import ProcessedUpdate
from leadflow.bot.runtime import _deliver_pending_messages
from leadflow.bot.runtime import run_polling
from leadflow.bot.services import bind_question
from leadflow.bot.services import confirm_draft
from leadflow.bot.services import continue_directions
from leadflow.bot.services import continue_request
from leadflow.bot.services import prepare_confirmation
from leadflow.bot.services import set_field
from leadflow.bot.services import start_draft
from leadflow.crm.models import SYSTEM_TAGS
from leadflow.crm.models import Lead
from leadflow.crm.models import SubmissionReceipt
from leadflow.crm.models import Tag

pytestmark = pytest.mark.django_db(transaction=True)

BOT_ID = 90001


def _user(user_id, first_name="Клиент", *, is_bot=False, username=None):
    user = {"id": user_id, "is_bot": is_bot, "first_name": first_name}
    if username is not None:
        user["username"] = username
    return user


def _chat(user_id, kind="private"):
    chat = {"id": user_id, "type": kind}
    if kind == "private":
        chat["first_name"] = "Клиент"
    return chat


def _message_update(update_id, user_id, text, *, date=None, reply_to=None, username=None):
    date = date or int(timezone.now().timestamp())
    message = {
        "message_id": update_id,
        "date": date,
        "chat": _chat(user_id),
        "from": _user(user_id, username=username),
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": reply_to,
            "date": date - 1,
            "chat": _chat(user_id),
            "from": _user(BOT_ID, "Leadflow", is_bot=True),
            "text": "Текущий вопрос",
        }
    return {"update_id": update_id, "message": message}


def _edited_message_update(update_id, user_id, message_id, text):
    return {
        "update_id": update_id,
        "edited_message": {
            "message_id": message_id,
            "date": int(timezone.now().timestamp()),
            "edit_date": int(timezone.now().timestamp()),
            "chat": _chat(user_id),
            "from": _user(user_id),
            "text": text,
        },
    }


def _callback_update(update_id, user_id, data, *, message_id=None):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": _user(user_id),
            "chat_instance": "private-chat",
            "message": {
                "message_id": message_id if message_id is not None else update_id,
                "date": int(timezone.now().timestamp()),
                "chat": _chat(user_id),
                "from": _user(BOT_ID, "Leadflow", is_bot=True),
                "text": "Действие",
            },
            "data": data,
        },
    }


def _contact_update(update_id, user_id, contact_user_id, *, date=None):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": date or int(timezone.now().timestamp()),
            "chat": _chat(user_id),
            "from": _user(user_id),
            "contact": {
                "phone_number": "77011234567",
                "first_name": "Клиент",
                "user_id": contact_user_id,
            },
        },
    }


def _review_draft(user_id):
    for code, name in SYSTEM_TAGS.items():
        Tag.objects.get_or_create(code=code, defaults={"name": name})
    draft = start_draft(user_id)
    draft = set_field(user_id, draft.pk, draft.revision, "direction", "website")
    draft = continue_directions(user_id, draft.pk, draft.revision)
    draft = set_field(user_id, draft.pk, draft.revision, "request", "Нужен сайт")
    draft = continue_request(user_id, draft.pk, draft.revision)
    draft = set_field(user_id, draft.pk, draft.revision, "name", "Тестовый клиент")
    draft = set_field(user_id, draft.pk, draft.revision, "contacts", "+77011234567")
    draft = set_field(user_id, draft.pk, draft.revision, "continue_contacts", None)
    return draft


def _name_step_draft(user_id):
    for code, name in SYSTEM_TAGS.items():
        Tag.objects.get_or_create(code=code, defaults={"name": name})
    draft = start_draft(user_id)
    draft = set_field(user_id, draft.pk, draft.revision, "direction", "website")
    draft = continue_directions(user_id, draft.pk, draft.revision)
    draft = set_field(user_id, draft.pk, draft.revision, "request", "Нужен сайт")
    return continue_request(user_id, draft.pk, draft.revision)


def _contact_draft(user_id):
    draft = _name_step_draft(user_id)
    return set_field(user_id, draft.pk, draft.revision, "name", "Клиент")


def _request_draft(user_id):
    draft = start_draft(user_id)
    draft = set_field(user_id, draft.pk, draft.revision, "direction", "website")
    return continue_directions(user_id, draft.pk, draft.revision)


def _request_prompt(user_id, update_id):
    draft = _request_draft(user_id)
    process_update(
        BOT_ID,
        _callback_update(update_id, user_id, make_callback("resume", draft.pk, draft.revision)),
    )
    _deliver_outbox(update_id)
    draft.refresh_from_db()
    return draft


def _deliver_outbox(update_id):
    messages = OutboundMessage.objects.filter(
        processed_update__polling_state_id=BOT_ID,
        processed_update__update_id=update_id,
        status=OutboundMessage.Status.PENDING,
    ).order_by("ordinal")
    for message in messages:
        mark_message_delivered(
            message.pk,
            telegram_message_id=10000 + message.pk,
            telegram_message_date=timezone.now().replace(microsecond=0),
        )


def _button_data(update_id, label):
    messages = OutboundMessage.objects.filter(
        processed_update__polling_state_id=BOT_ID,
        processed_update__update_id=update_id,
    ).order_by("ordinal")
    for message in messages:
        for row in message.reply_markup.get("inline_keyboard", []):
            for button in row:
                if button["text"] == label:
                    return button["callback_data"]
    raise AssertionError(f"Button {label!r} not found in update {update_id}")


def _has_button(update_id, label):
    return any(
        button["text"] == label
        for message in OutboundMessage.objects.filter(
            processed_update__polling_state_id=BOT_ID,
            processed_update__update_id=update_id,
        )
        for row in message.reply_markup.get("inline_keyboard", [])
        for button in row
    )


def test_update_processing_is_idempotent_and_persists_the_polling_offset():
    update = _message_update(120, 42, "/start")

    first = process_update(BOT_ID, update)
    repeated = process_update(BOT_ID, update)

    assert not first.duplicate and repeated.duplicate
    assert get_polling_offset(BOT_ID) == 121
    assert ProcessedUpdate.objects.filter(polling_state_id=BOT_ID, update_id=120).count() == 1
    assert OutboundMessage.objects.filter(processed_update__update_id=120).count() == 1
    assert OutboundMessage.objects.get(processed_update__update_id=120).text.startswith(
        "Выберите одно или несколько направлений"
    )


@pytest.mark.parametrize("already_processed", [False, True])
def test_polling_offset_follows_a_lower_sequence_without_repeating_dialogue(already_processed):
    update = _message_update(1000, 42, "/start")
    if already_processed:
        process_update(BOT_ID, update)
    BotPollingState.objects.update_or_create(bot_id=BOT_ID, defaults={"next_offset": 2000000001})

    result = process_update(BOT_ID, update)

    assert result.duplicate is already_processed
    assert get_polling_offset(BOT_ID) == 1001
    assert OutboundMessage.objects.filter(processed_update__update_id=1000).count() == 1


def test_lower_sequence_offset_rolls_back_when_processing_fails():
    BotPollingState.objects.create(bot_id=BOT_ID, next_offset=2000000001)
    with patch("leadflow.bot.handlers._handle_message", side_effect=RuntimeError("storage error")):
        with pytest.raises(RuntimeError, match="storage error"):
            process_update(BOT_ID, _message_update(1000, 42, "/start"))

    assert get_polling_offset(BOT_ID) == 2000000001
    assert not ProcessedUpdate.objects.exists()
    assert not OutboundMessage.objects.exists()


def test_polling_drains_more_than_one_batch_after_update_ids_restart_lower():
    process_update(BOT_ID, {"update_id": 2000000000})
    queued = [{"update_id": number} for number in range(1000, 1101)]
    offsets = []

    async def get_updates(*, offset, limit, **kwargs):
        nonlocal queued
        offsets.append(offset)
        if len(offsets) == 3:
            raise asyncio.CancelledError
        # Telegram falls back to the queue head when offset exceeds its tail by over 10.
        if queued and offset <= queued[-1]["update_id"] + 11:
            queued = [update for update in queued if update["update_id"] >= offset]
        return queued[:limit]

    bot = SimpleNamespace(
        session=SimpleNamespace(close=AsyncMock()),
        get_me=AsyncMock(return_value=SimpleNamespace(id=BOT_ID)),
        set_my_commands=AsyncMock(),
        set_chat_menu_button=AsyncMock(),
        get_updates=AsyncMock(side_effect=get_updates),
        send_message=AsyncMock(),
    )
    with patch("leadflow.bot.runtime.Bot", return_value=bot):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_polling("123456:fake-token-for-test-only"))

    assert offsets == [2000000001, 1100, 1101]
    assert get_polling_offset(BOT_ID) == 1101
    assert ProcessedUpdate.objects.count() == 102
    assert ProcessedUpdate.objects.filter(update_id=1100).exists()
    bot.send_message.assert_not_awaited()
    bot.session.close.assert_awaited_once()


def test_failed_update_transaction_does_not_advance_offset_and_can_be_retried():
    update = _message_update(121, 43, "/start")
    with patch("leadflow.bot.handlers._handle_message", side_effect=RuntimeError("storage error")):
        with pytest.raises(RuntimeError, match="storage error"):
            process_update(BOT_ID, update)

    assert not ProcessedUpdate.objects.filter(update_id=121).exists()
    assert OutboundMessage.objects.count() == 0

    result = process_update(BOT_ID, update)

    assert not result.duplicate
    assert get_polling_offset(BOT_ID) == 122
    assert OutboundMessage.objects.filter(processed_update__update_id=121).count() == 1


@pytest.mark.parametrize("text", [" ", "\t\n", "\u00a0", "\u2003"])
def test_whitespace_answer_keeps_saved_values_and_does_not_block_other_users(text):
    draft = _name_step_draft(90)
    asked_at = timezone.now().replace(microsecond=0)
    bind_question(90, draft.pk, draft.revision, 90000, asked_at)

    process_update(
        BOT_ID,
        _message_update(901, 90, text, reply_to=90000, date=int(asked_at.timestamp()) + 1),
    )

    draft.refresh_from_db()
    assert draft.values == {"directions": ["website"], "request": "Нужен сайт"}
    assert draft.step == Draft.Step.NAME
    assert get_polling_offset(BOT_ID) == 902
    assert OutboundMessage.objects.filter(
        processed_update__update_id=901, bind_question=True
    ).exists()
    process_update(BOT_ID, _message_update(902, 91, "/start"))
    assert get_polling_offset(BOT_ID) == 903
    assert OutboundMessage.objects.filter(processed_update__update_id=902, chat_id=91).exists()
    assert Lead.objects.count() == 0


def test_back_navigation_reaches_name_after_accepted_contact_without_losing_values():
    draft = _review_draft(93)
    saved_values = draft.values
    process_update(
        BOT_ID,
        _callback_update(931, 93, make_callback("resume", draft.pk, draft.revision)),
    )
    for update_id, step in zip(
        [932, 933, 934],
        [Draft.Step.CONTACT_CHOICE, Draft.Step.CONTACTS, Draft.Step.NAME],
        strict=True,
    ):
        update = _message_update(update_id, 93, "/back")
        _deliver_outbox(update_id - 1)
        process_update(BOT_ID, update)
        draft.refresh_from_db()
        assert draft.step == step
        assert draft.values == saved_values


def test_back_from_review_reaches_name_and_preserves_all_values():
    draft = _review_draft(97)
    saved_values = draft.values.copy()
    process_update(
        BOT_ID, _callback_update(970, 97, make_callback("resume", draft.pk, draft.revision))
    )
    for update_id, step in zip(
        range(971, 976),
        [
            Draft.Step.CONTACT_CHOICE,
            Draft.Step.CONTACTS,
            Draft.Step.NAME,
            Draft.Step.REQUEST,
            Draft.Step.DIRECTION,
        ],
        strict=True,
    ):
        update = _message_update(update_id, 97, "/back")
        _deliver_outbox(update_id - 1)
        process_update(BOT_ID, update)
        draft.refresh_from_db()
        assert draft.step == step
        assert draft.values == saved_values


def test_long_contact_list_shows_a_short_summary_and_keeps_continue_action():
    draft = _contact_draft(94)
    domain = f"{'b' * 63}.{'c' * 63}.{'d' * 61}"
    contacts = [f"{'a' * 62}{index:02}@{domain}" for index in range(17)]
    for index, contact in enumerate(contacts):
        draft = set_field(94, draft.pk, draft.revision, "contacts", contact)
        if index < len(contacts) - 1:
            draft = set_field(94, draft.pk, draft.revision, "add_contact", None)
    process_update(
        BOT_ID,
        _callback_update(941, 94, make_callback("resume", draft.pk, draft.revision)),
    )
    messages = list(
        OutboundMessage.objects.filter(processed_update__update_id=941).order_by("ordinal")
    )
    assert len(messages) == 1
    assert len(messages[0].text) <= 4096
    assert "Сохранено контактов: 17" in messages[0].text
    assert contacts[-1] in messages[0].text
    next_action = _button_data(941, "К проверке")
    assert messages[-1].reply_markup
    sent = []

    async def send_message(**kwargs):
        assert len(kwargs["text"]) <= 4096
        sent.append(kwargs)
        return SimpleNamespace(message_id=94000 + len(sent), date=timezone.now())

    asyncio.run(_deliver_pending_messages(SimpleNamespace(send_message=send_message)))

    assert len(sent) == len(messages)
    assert get_pending_message() is None
    process_update(BOT_ID, _callback_update(942, 94, next_action))
    draft.refresh_from_db()
    assert draft.step == Draft.Step.REVIEW
    assert draft.values["contacts"] == contacts


def test_outbound_retry_waits_for_flood_control_delay():
    process_update(BOT_ID, _message_update(122, 44, "/start"))
    message = OutboundMessage.objects.get(processed_update__update_id=122)
    retry_started = timezone.now()

    mark_message_failed(message.pk, "TelegramRetryAfter", retry_after=20)

    message.refresh_from_db()
    assert message.status == OutboundMessage.Status.PENDING
    assert message.attempts == 1
    assert message.last_error == "TelegramRetryAfter"
    assert message.next_attempt_at >= retry_started + timedelta(seconds=20)
    assert message.text.startswith("Выберите одно или несколько направлений")
    assert get_pending_message() is None


@pytest.mark.parametrize("attempts", [32766, 32767])
def test_outbound_retry_saturates_counter_and_keeps_scheduling_recovery(attempts):
    process_update(BOT_ID, _message_update(981, 98, "/start"))
    message = OutboundMessage.objects.get(processed_update__update_id=981)
    message.attempts = attempts
    message.save(update_fields=["attempts"])
    for _ in range(2):
        retry_started = timezone.now()
        mark_message_failed(message.pk, "TelegramNetworkError")
        message.refresh_from_db()
        assert message.attempts == 32767
        assert message.next_attempt_at >= retry_started + timedelta(seconds=30)
        assert message.status == OutboundMessage.Status.PENDING

    mark_message_delivered(message.pk, 98000, timezone.now())
    message.refresh_from_db()
    assert message.status == OutboundMessage.Status.DELIVERED


@pytest.mark.parametrize("attempts", [32766, 32767])
def test_submission_retry_saturates_counter_and_can_complete_after_recovery(attempts):
    draft = _review_draft(99)
    polling, _ = BotPollingState.objects.get_or_create(bot_id=BOT_ID)
    event = ProcessedUpdate.objects.create(polling_state=polling, update_id=991)
    pending = prepare_confirmation(99, draft.pk, draft.revision, event)
    pending.submission_attempts = attempts
    pending.save(update_fields=["submission_attempts"])
    for _ in range(2):
        retry_started = timezone.now()
        defer_pending_submission(draft.pk, "OperationalError")
        pending.refresh_from_db()
        assert pending.submission_attempts == 32767
        assert pending.submission_retry_at >= retry_started + timedelta(seconds=30)
        assert pending.submission_state == "pending"
        assert pending.values == draft.values

    complete_pending_submission(draft.pk)
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1
    assert not Draft.objects.filter(pk=draft.pk).exists()


@pytest.mark.parametrize("contact_count", [146, 147, 300])
def test_large_contact_lists_are_paginated_and_can_still_be_submitted(contact_count):
    draft = _review_draft(96)
    contacts = [f"@client{index:04}" for index in range(contact_count)]
    draft.values["contacts"] = contacts
    draft.save(update_fields=["values"])
    process_update(
        BOT_ID,
        _callback_update(961, 96, make_callback("resume", draft.pk, draft.revision)),
    )

    review = OutboundMessage.objects.get(processed_update__update_id=961)
    primary_labels = [
        button["text"] for row in review.reply_markup["inline_keyboard"] for button in row
    ]
    assert primary_labels == ["Отправить заявку", "Исправить", "Отменить заявку"]
    edit = _button_data(961, "Исправить")
    _deliver_outbox(961)
    menu_message_id = 10000 + review.pk
    update_id = 962
    process_update(BOT_ID, _callback_update(update_id, 96, edit, message_id=menu_message_id))
    contacts_menu = _button_data(update_id, "Контакты")
    _deliver_outbox(update_id)

    update_id += 1
    process_update(
        BOT_ID,
        _callback_update(update_id, 96, contacts_menu, message_id=menu_message_id),
    )
    while True:
        page_message = OutboundMessage.objects.get(processed_update__update_id=update_id)
        keyboard = page_message.reply_markup["inline_keyboard"]
        assert sum(len(row) for row in keyboard) <= 300
        labels = [button["text"] for row in keyboard for button in row]
        if "Следующие" not in labels:
            assert f"{contact_count}. {contacts[-1]}" in labels
            parent = _button_data(update_id, "К исправлениям")
            break
        next_page = _button_data(update_id, "Следующие")
        _deliver_outbox(update_id)
        update_id += 1
        process_update(
            BOT_ID,
            _callback_update(update_id, 96, next_page, message_id=menu_message_id),
        )

    _deliver_outbox(update_id)
    update_id += 1
    process_update(
        BOT_ID,
        _callback_update(update_id, 96, parent, message_id=menu_message_id),
    )
    review_home = _button_data(update_id, "К проверке")
    _deliver_outbox(update_id)
    update_id += 1
    process_update(
        BOT_ID,
        _callback_update(update_id, 96, review_home, message_id=menu_message_id),
    )
    confirm = _button_data(update_id, "Отправить заявку")
    update_id += 1
    process_update(
        BOT_ID,
        _callback_update(update_id, 96, confirm, message_id=menu_message_id),
    )
    complete_pending_submission(draft.pk)

    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1
    assert list(Lead.objects.get().contacts.values_list("value", flat=True)) == contacts


def test_outbound_retry_preserves_chat_order_without_blocking_other_chats():
    process_update(BOT_ID, _message_update(801, 81, "/start"))
    process_update(BOT_ID, _message_update(802, 81, "/start"))
    process_update(BOT_ID, _message_update(803, 82, "/start"))
    first = OutboundMessage.objects.get(processed_update__update_id=801)
    second = OutboundMessage.objects.get(processed_update__update_id=802)
    other_chat = OutboundMessage.objects.get(processed_update__update_id=803)

    mark_message_failed(first.pk, "TelegramNetworkError", retry_after=20)

    assert get_pending_message()["id"] == other_chat.pk
    mark_message_delivered(other_chat.pk, 80003, timezone.now())
    assert get_pending_message() is None
    OutboundMessage.objects.filter(pk=first.pk).update(next_attempt_at=timezone.now())
    assert get_pending_message()["id"] == first.pk
    mark_message_delivered(first.pk, 80001, timezone.now())
    assert get_pending_message()["id"] == second.pk


@pytest.mark.parametrize("error_type", [TelegramNetworkError, TelegramServerError])
def test_delivery_continues_in_other_chats_after_a_local_temporary_error(error_type):
    process_update(BOT_ID, _message_update(851, 85, "/start"))
    process_update(BOT_ID, _message_update(852, 85, "/start"))
    process_update(BOT_ID, _message_update(853, 86, "/start"))
    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs["chat_id"])
        if kwargs["chat_id"] == 85:
            raise error_type(method=None, message="temporary test failure")
        return SimpleNamespace(message_id=86000, date=timezone.now())

    asyncio.run(_deliver_pending_messages(SimpleNamespace(send_message=send_message)))

    assert sent == [85, 86]
    assert OutboundMessage.objects.get(processed_update__update_id=851).attempts == 1
    assert OutboundMessage.objects.get(processed_update__update_id=852).attempts == 0
    assert OutboundMessage.objects.get(processed_update__update_id=853).status == "delivered"


def test_delivery_stops_when_telegram_requests_a_rate_limit_pause():
    process_update(BOT_ID, _message_update(861, 87, "/start"))
    process_update(BOT_ID, _message_update(862, 88, "/start"))
    bot = SimpleNamespace(
        send_message=AsyncMock(
            side_effect=TelegramRetryAfter(method=None, message="test rate limit", retry_after=20)
        )
    )

    asyncio.run(_deliver_pending_messages(bot))

    bot.send_message.assert_awaited_once()
    assert OutboundMessage.objects.get(processed_update__update_id=861).attempts == 1
    assert OutboundMessage.objects.get(processed_update__update_id=862).attempts == 0


def test_permanent_delivery_failure_unblocks_the_next_message_in_the_chat():
    process_update(BOT_ID, _message_update(811, 83, "/start"))
    process_update(BOT_ID, _message_update(812, 83, "/start"))
    first = OutboundMessage.objects.get(processed_update__update_id=811)
    second = OutboundMessage.objects.get(processed_update__update_id=812)
    mark_message_failed(first.pk, "TelegramNetworkError", retry_after=20)
    assert get_pending_message() is None

    mark_message_failed(first.pk, "TelegramForbiddenError", permanent=True)

    assert get_pending_message()["id"] == second.pk


def test_delivered_outbound_message_clears_saved_copy_and_markup():
    process_update(BOT_ID, _message_update(123, 44, "/start"))
    message = OutboundMessage.objects.get(processed_update__update_id=123)
    assert message.text.startswith("Выберите одно или несколько направлений")
    assert message.reply_markup

    mark_message_delivered(
        message.pk,
        telegram_message_id=12300,
        telegram_message_date=timezone.now(),
    )

    message.refresh_from_db()
    assert message.status == OutboundMessage.Status.DELIVERED
    assert message.text == ""
    assert message.reply_markup == {}


def test_reply_to_delivered_request_prompt_advances_the_current_draft():
    draft = start_draft(42)
    draft = set_field(42, draft.pk, draft.revision, "direction", "website")
    draft = continue_directions(42, draft.pk, draft.revision)
    process_update(
        BOT_ID,
        _callback_update(202, 42, make_callback("resume", draft.pk, draft.revision)),
    )

    messages = list(
        OutboundMessage.objects.filter(processed_update__update_id=202).order_by("ordinal")
    )
    assert len(messages) == 1
    prompt = messages[-1]
    assert not prompt.reply_markup
    sent_at = timezone.now().replace(microsecond=0)
    mark_message_delivered(prompt.pk, telegram_message_id=800, telegram_message_date=sent_at)

    update = _message_update(
        203,
        42,
        "Нужен сайт",
        date=int((sent_at + timedelta(seconds=1)).timestamp()),
        reply_to=800,
    )
    process_update(BOT_ID, update)

    draft = Draft.objects.get(user_id=42)
    assert draft.values["request"] == "Нужен сайт"
    assert draft.step == Draft.Step.REQUEST
    assert draft.question_id == 800


def test_continue_request_sends_a_new_name_question():
    draft = _request_prompt(160, 1600)
    request_question_id = draft.question_id
    process_update(
        BOT_ID,
        _message_update(1601, 160, "Нужен сайт", reply_to=request_question_id),
    )
    continue_data = _button_data(1601, "Продолжить")
    _deliver_outbox(1601)
    draft.refresh_from_db()
    progress_id = draft.active_control_ids[-1]

    process_update(BOT_ID, _callback_update(1602, 160, continue_data, message_id=progress_id))

    messages = list(OutboundMessage.objects.filter(processed_update__update_id=1602))
    name_question = next(message for message in messages if message.text)
    assert name_question.operation == OutboundMessage.Operation.SEND
    assert name_question.bind_question
    assert not any(message.operation == OutboundMessage.Operation.EDIT_TEXT for message in messages)
    assert any(
        message.operation == OutboundMessage.Operation.EDIT_MARKUP
        and message.target_message_id == progress_id
        and not message.reply_markup
        for message in messages
    )
    _deliver_outbox(1602)
    draft.refresh_from_db()
    assert draft.step == Draft.Step.NAME
    assert draft.question_id not in {request_question_id, progress_id}


@pytest.mark.parametrize("reply_to_progress", [False, True])
def test_request_progress_sends_below_answers_and_keeps_the_original_question(reply_to_progress):
    draft = _request_prompt(161, 1610)
    question_id, question_date = draft.question_id, draft.question_date
    process_update(
        BOT_ID,
        _message_update(1611, 161, "Первая часть", reply_to=question_id),
    )
    progress = OutboundMessage.objects.get(processed_update__update_id=1611)
    assert progress.operation == OutboundMessage.Operation.SEND
    assert not progress.bind_question
    _deliver_outbox(1611)
    draft.refresh_from_db()
    first_progress_id = draft.active_control_ids[-1]
    assert draft.question_id == question_id and draft.question_date == question_date

    process_update(
        BOT_ID,
        _message_update(
            1612,
            161,
            "Вторая часть",
            reply_to=first_progress_id if reply_to_progress else question_id,
        ),
    )
    messages = list(OutboundMessage.objects.filter(processed_update__update_id=1612))
    assert any(
        message.operation == OutboundMessage.Operation.EDIT_MARKUP
        and message.target_message_id == first_progress_id
        and not message.reply_markup
        for message in messages
    )
    assert sum(message.operation == OutboundMessage.Operation.SEND for message in messages) == 1
    assert not any(message.operation == OutboundMessage.Operation.EDIT_TEXT for message in messages)
    _deliver_outbox(1612)
    draft.refresh_from_db()
    assert draft.values["request"] == "Первая часть\n\nВторая часть"
    assert draft.question_id == question_id and draft.question_date == question_date
    assert len(draft.active_control_ids) == 1
    assert draft.active_control_ids[0] not in {question_id, first_progress_id}


def test_request_parts_are_preserved_when_previous_progress_delivery_is_delayed():
    draft = _request_prompt(162, 1620)
    question_id = draft.question_id
    date = int(draft.question_date.timestamp()) + 1
    process_update(BOT_ID, _message_update(1621, 162, "Первая часть", date=date))
    first_progress = OutboundMessage.objects.get(processed_update__update_id=1621)
    mark_message_failed(first_progress.pk, "TelegramNetworkError")

    process_update(BOT_ID, _message_update(1622, 162, "Вторая часть", date=date))

    first_progress.refresh_from_db()
    assert not first_progress.reply_markup and not first_progress.interactive
    latest_progress = OutboundMessage.objects.get(processed_update__update_id=1622)
    assert latest_progress.operation == OutboundMessage.Operation.SEND
    assert _has_button(1622, "Продолжить")
    _deliver_outbox(1621)
    _deliver_outbox(1622)
    draft.refresh_from_db()
    assert draft.values["request"] == "Первая часть\n\nВторая часть"
    assert draft.question_id == question_id
    assert draft.active_control_ids == [10000 + latest_progress.pk]


def test_name_answer_is_followed_by_a_new_contact_question():
    draft = _name_step_draft(163)
    process_update(
        BOT_ID, _callback_update(1630, 163, make_callback("resume", draft.pk, draft.revision))
    )
    _deliver_outbox(1630)
    draft.refresh_from_db()
    name_question_id = draft.question_id

    process_update(BOT_ID, _message_update(1631, 163, "Кирилл", reply_to=name_question_id))

    contact_question = OutboundMessage.objects.get(processed_update__update_id=1631)
    assert contact_question.operation == OutboundMessage.Operation.SEND
    assert contact_question.bind_question
    assert contact_question.reply_markup["keyboard"][0][0]["request_contact"]
    _deliver_outbox(1631)
    draft.refresh_from_db()
    assert draft.values["name"] == "Кирилл"
    assert draft.step == Draft.Step.CONTACTS
    assert draft.question_id != name_question_id


@pytest.mark.parametrize(
    "field,path,answer",
    [
        ("name", ["Исправить", "Имя"], "Новое имя"),
        ("request", ["Исправить", "Описание", "Заменить описание"], "Другой запрос"),
        ("direction", ["Исправить", "Направления"], None),
        ("contacts", ["Исправить", "Контакты", "Добавить контакт"], "client@example.com"),
    ],
)
def test_field_correction_and_return_to_review_send_new_messages(field, path, answer):
    draft = _review_draft(164)
    update_id = 1640
    process_update(
        BOT_ID, _callback_update(update_id, 164, make_callback("resume", draft.pk, draft.revision))
    )
    data = _button_data(update_id, path[0])
    _deliver_outbox(update_id)
    draft.refresh_from_db()
    review_message_id = draft.active_control_ids[-1]
    for index, label in enumerate(path):
        update_id += 1
        process_update(BOT_ID, _callback_update(update_id, 164, data, message_id=review_message_id))
        if label != path[-1]:
            menu = OutboundMessage.objects.get(processed_update__update_id=update_id)
            assert menu.operation == OutboundMessage.Operation.EDIT_TEXT
            assert menu.target_message_id == review_message_id
            data = _button_data(update_id, path[index + 1])
            _deliver_outbox(update_id)

    messages = list(OutboundMessage.objects.filter(processed_update__update_id=update_id))
    question = next(message for message in messages if message.text)
    assert question.operation == OutboundMessage.Operation.SEND
    assert not any(message.operation == OutboundMessage.Operation.EDIT_TEXT for message in messages)
    continue_data = _button_data(update_id, "Продолжить") if field == "direction" else None
    _deliver_outbox(update_id)
    draft.refresh_from_db()
    field_message_id = draft.active_control_ids[-1] if field == "direction" else draft.question_id
    assert field_message_id != review_message_id

    update_id += 1
    if field == "direction":
        process_update(
            BOT_ID, _callback_update(update_id, 164, continue_data, message_id=field_message_id)
        )
    else:
        process_update(BOT_ID, _message_update(update_id, 164, answer, reply_to=field_message_id))
    messages = list(OutboundMessage.objects.filter(processed_update__update_id=update_id))
    review = next(message for message in messages if "inline_keyboard" in message.reply_markup)
    assert review.operation == OutboundMessage.Operation.SEND
    assert not any(message.operation == OutboundMessage.Operation.EDIT_TEXT for message in messages)
    _deliver_outbox(update_id)
    draft.refresh_from_db()
    assert draft.step == Draft.Step.REVIEW
    assert len(draft.active_control_ids) == 1
    assert draft.active_control_ids[0] not in {review_message_id, field_message_id}


def test_back_to_request_sends_a_new_question_and_rejects_an_old_reply():
    draft = _request_prompt(165, 1650)
    original_question_id = draft.question_id
    process_update(BOT_ID, _message_update(1651, 165, "Нужен сайт", reply_to=original_question_id))
    continue_data = _button_data(1651, "Продолжить")
    _deliver_outbox(1651)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _callback_update(1652, 165, continue_data, message_id=draft.active_control_ids[-1]),
    )
    _deliver_outbox(1652)
    draft.refresh_from_db()
    name_question_id = draft.question_id

    process_update(BOT_ID, _message_update(1653, 165, "/back"))

    request_question = OutboundMessage.objects.get(processed_update__update_id=1653)
    assert request_question.operation == OutboundMessage.Operation.SEND
    _deliver_outbox(1653)
    draft.refresh_from_db()
    assert draft.question_id not in {original_question_id, name_question_id}
    assert draft.step == Draft.Step.REQUEST
    assert draft.values["request"] == "Нужен сайт"
    process_update(
        BOT_ID, _message_update(1654, 165, "Старый ответ", reply_to=original_question_id)
    )
    draft.refresh_from_db()
    assert draft.values["request"] == "Нужен сайт"


def test_request_progress_delivers_a_new_message_without_rewriting_the_question():
    draft = start_draft(43)
    draft = set_field(43, draft.pk, draft.revision, "direction", "website")
    draft = continue_directions(43, draft.pk, draft.revision)
    process_update(
        BOT_ID,
        _callback_update(210, 43, make_callback("resume", draft.pk, draft.revision)),
    )
    sent_at = timezone.now().replace(microsecond=0)
    next_message_id = 8000

    async def send_message(**kwargs):
        nonlocal next_message_id
        next_message_id += 1
        return SimpleNamespace(message_id=next_message_id, date=sent_at)

    bot = SimpleNamespace(
        send_message=send_message,
        edit_message_text=AsyncMock(
            side_effect=lambda **kwargs: SimpleNamespace(
                message_id=kwargs["message_id"], date=sent_at
            )
        ),
        edit_message_reply_markup=AsyncMock(return_value=True),
    )
    asyncio.run(_deliver_pending_messages(bot))
    draft.refresh_from_db()
    question_id = draft.question_id
    process_update(
        BOT_ID,
        _message_update(
            211,
            43,
            "Нужен сайт",
            date=int((sent_at + timedelta(seconds=1)).timestamp()),
            reply_to=question_id,
        ),
    )

    asyncio.run(_deliver_pending_messages(bot))

    bot.edit_message_text.assert_not_awaited()
    draft.refresh_from_db()
    assert draft.question_id == question_id
    assert draft.active_control_ids == [next_message_id]
    assert next_message_id != question_id
    bot.edit_message_reply_markup.assert_not_awaited()


def test_contact_button_rejects_someone_elses_number_and_accepts_the_owners():
    draft = _contact_draft(45)
    asked_at = timezone.now().replace(microsecond=0)
    bind_question(45, draft.pk, draft.revision, 700, asked_at)

    process_update(BOT_ID, _contact_update(451, 45, 46))

    draft.refresh_from_db()
    assert draft.values.get("contacts", []) == []
    _deliver_outbox(451)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _contact_update(
            452,
            45,
            45,
            date=int(draft.question_date.timestamp()) + 1,
        ),
    )

    draft.refresh_from_db()
    assert draft.values["contacts"] == ["+77011234567"]
    assert draft.step == Draft.Step.CONTACT_CHOICE


def _ask_for_contact(user_id, update_id):
    draft = _contact_draft(user_id)
    process_update(
        BOT_ID,
        _callback_update(update_id, user_id, make_callback("resume", draft.pk, draft.revision)),
    )
    return draft


def _contact_question(update_id):
    messages = list(
        OutboundMessage.objects.filter(processed_update__update_id=update_id).order_by("ordinal")
    )
    questions = [message for message in messages if message.bind_question]
    assert len(questions) == 1
    prompt = questions[0]
    assert prompt.reply_markup["keyboard"] == [
        [{"text": "Отправить мой номер", "request_contact": True}]
    ]
    assert prompt.reply_markup["one_time_keyboard"] is True
    assert sum("keyboard" in message.reply_markup for message in messages) == 1
    return prompt


def test_contact_question_delivery_retains_keyboard_and_binds_accepted_phone():
    draft = _ask_for_contact(77, 770)
    sent_at = timezone.now().replace(microsecond=0)
    sent = []

    async def send_message(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=80000 + len(sent), date=sent_at)

    bot = SimpleNamespace(send_message=send_message)
    asyncio.run(_deliver_pending_messages(bot))

    assert len(sent) == 1
    markup = sent[0]["reply_markup"]
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.keyboard[0][0].request_contact is True
    draft.refresh_from_db()
    assert draft.question_id == 80001
    process_update(
        BOT_ID,
        _contact_update(771, 77, 77, date=int(sent_at.timestamp()) + 1),
    )
    draft.refresh_from_db()
    assert draft.values["contacts"] == ["+77011234567"]


def test_telegram_username_is_offered_as_an_explicit_contact_choice():
    update = _message_update(781, 78, "/start", username="client_name")
    process_update(BOT_ID, update)
    assert BotUser.objects.get(pk=78).username == "client_name"

    draft = _contact_draft(79)
    BotUser.objects.filter(pk=79).update(username="client_name")
    process_update(
        BOT_ID,
        _callback_update(791, 79, make_callback("resume", draft.pk, draft.revision)),
    )
    prompt = OutboundMessage.objects.get(processed_update__update_id=791)
    assert prompt.reply_markup["keyboard"][1][0]["text"] == "Использовать @client_name"
    _deliver_outbox(791)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _message_update(
            792,
            79,
            "Использовать @client_name",
            date=int(draft.question_date.timestamp()) + 1,
        ),
    )

    draft.refresh_from_db()
    assert draft.values["contacts"] == ["@client_name"]
    assert draft.step == Draft.Step.CONTACT_CHOICE


def test_username_choice_replaces_the_contact_being_corrected_and_hides_phone_keyboard():
    draft = _review_draft(79)
    BotUser.objects.filter(pk=79).update(username="client_name")
    old_contact = DraftInput.objects.get(draft=draft, field=DraftInput.Field.CONTACT)
    old_contact.source_message_id = 7900
    old_contact.save(update_fields=["source_message_id"])
    process_update(
        BOT_ID,
        _callback_update(7901, 79, make_callback("resume", draft.pk, draft.revision)),
    )
    menu_message = OutboundMessage.objects.get(processed_update__update_id=7901)
    control_id = 10000 + menu_message.pk
    edit = _button_data(7901, "Исправить")
    _deliver_outbox(7901)
    process_update(BOT_ID, _callback_update(7902, 79, edit, message_id=control_id))
    contacts = _button_data(7902, "Контакты")
    _deliver_outbox(7902)
    process_update(BOT_ID, _callback_update(7903, 79, contacts, message_id=control_id))
    pick = _button_data(7903, "1. +77011234567")
    _deliver_outbox(7903)
    process_update(BOT_ID, _callback_update(7904, 79, pick, message_id=control_id))
    edit_contact = _button_data(7904, "Изменить контакт")
    _deliver_outbox(7904)
    process_update(BOT_ID, _callback_update(7905, 79, edit_contact, message_id=control_id))
    prompt = OutboundMessage.objects.get(processed_update__update_id=7905, bind_question=True)
    assert prompt.reply_markup["keyboard"][1][0]["text"] == "Использовать @client_name"
    _deliver_outbox(7905)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _message_update(
            7906,
            79,
            "Использовать @client_name",
            date=int(draft.question_date.timestamp()) + 1,
        ),
    )
    draft.refresh_from_db()
    old_contact.refresh_from_db()

    assert draft.values["contacts"] == ["@client_name"]
    assert draft.step == Draft.Step.REVIEW
    assert old_contact.active and old_contact.editing_enabled
    assert old_contact.accepted_text == "@client_name"
    assert old_contact.source_message_id is None
    assert OutboundMessage.objects.filter(
        processed_update__update_id=7906,
        reply_markup__remove_keyboard=True,
    ).exists()
    process_update(BOT_ID, _edited_message_update(7907, 79, 7900, "+77019999999"))
    draft.refresh_from_db()
    assert draft.values["contacts"] == ["@client_name"]


@pytest.mark.parametrize(
    "contact", ["+77011234567", "client@example.com", "@client_name", "t.me/client_name"]
)
def test_contact_question_shows_phone_button_and_accepts_manual_input(contact):
    draft = _ask_for_contact(72, 720)
    _contact_question(720)
    _deliver_outbox(720)
    draft.refresh_from_db()

    process_update(
        BOT_ID,
        _message_update(721, 72, contact, date=int(draft.question_date.timestamp()) + 1),
    )

    draft.refresh_from_db()
    assert draft.values["contacts"] == [contact]
    assert draft.step == Draft.Step.CONTACT_CHOICE
    assert OutboundMessage.objects.filter(
        processed_update__update_id=721, reply_markup__remove_keyboard=True
    ).exists()


def test_phone_button_returns_for_another_contact_and_rejects_an_old_reply():
    draft = _ask_for_contact(73, 730)
    _deliver_outbox(730)
    draft.refresh_from_db()
    old_question_id = draft.question_id
    process_update(
        BOT_ID,
        _contact_update(731, 73, 73, date=int(draft.question_date.timestamp()) + 1),
    )
    draft.refresh_from_db()
    assert draft.values["contacts"] == ["+77011234567"]
    assert OutboundMessage.objects.filter(
        processed_update__update_id=731, reply_markup__remove_keyboard=True
    ).exists()

    process_update(
        BOT_ID,
        _callback_update(732, 73, make_callback("add_contact", draft.pk, draft.revision)),
    )
    _contact_question(732)
    _deliver_outbox(732)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _message_update(
            733,
            73,
            "client@example.com",
            reply_to=old_question_id,
            date=int(draft.question_date.timestamp()) + 1,
        ),
    )
    draft.refresh_from_db()
    assert draft.values["contacts"] == ["+77011234567"]
    assert draft.step == Draft.Step.CONTACTS
    _contact_question(733)


def test_phone_button_works_when_correcting_a_contact_and_is_removed_at_review():
    draft = _review_draft(74)
    process_update(
        BOT_ID, _callback_update(740, 74, make_callback("resume", draft.pk, draft.revision))
    )
    menu = OutboundMessage.objects.get(processed_update__update_id=740)
    control_id = 10000 + menu.pk
    edit = _button_data(740, "Исправить")
    _deliver_outbox(740)
    process_update(BOT_ID, _callback_update(741, 74, edit, message_id=control_id))
    contacts = _button_data(741, "Контакты")
    _deliver_outbox(741)
    process_update(BOT_ID, _callback_update(742, 74, contacts, message_id=control_id))
    pick = _button_data(742, "1. +77011234567")
    _deliver_outbox(742)
    process_update(BOT_ID, _callback_update(743, 74, pick, message_id=control_id))
    edit_contact = _button_data(743, "Изменить контакт")
    _deliver_outbox(743)
    process_update(BOT_ID, _callback_update(744, 74, edit_contact, message_id=control_id))
    _contact_question(744)
    _deliver_outbox(744)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _message_update(
            745,
            74,
            "client@example.com",
            reply_to=draft.question_id,
            date=int(draft.question_date.timestamp()) + 1,
        ),
    )
    draft.refresh_from_db()
    assert draft.values["contacts"] == ["client@example.com"]
    assert draft.step == Draft.Step.REVIEW
    assert OutboundMessage.objects.filter(
        processed_update__update_id=745, reply_markup__remove_keyboard=True
    ).exists()


@pytest.mark.parametrize("action", ["/back", "back", "/cancel", "/start"])
def test_leaving_contact_input_hides_phone_button_and_keeps_saved_values(action):
    draft = _ask_for_contact(75, 750)
    _deliver_outbox(750)
    if action.startswith("/"):
        update = _message_update(751, 75, action)
    else:
        update = _callback_update(751, 75, make_callback(action, draft.pk, draft.revision))

    process_update(BOT_ID, update)

    draft.refresh_from_db()
    assert draft.values["name"] == "Клиент"
    assert OutboundMessage.objects.filter(
        processed_update__update_id=751, reply_markup__remove_keyboard=True
    ).exists()
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0
    if action == "/cancel":
        assert draft.pending_action == Draft.PendingAction.CANCEL
        process_update(
            BOT_ID,
            _callback_update(752, 75, make_callback("confirm_cancel", draft.pk, draft.revision)),
        )
        assert not Draft.objects.filter(pk=draft.pk).exists()


def test_back_to_contact_input_restores_the_phone_button():
    draft = _ask_for_contact(76, 760)
    _deliver_outbox(760)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _contact_update(761, 76, 76, date=int(draft.question_date.timestamp()) + 1),
    )
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _callback_update(762, 76, make_callback("back", draft.pk, draft.revision)),
    )

    _contact_question(762)
    draft.refresh_from_db()
    assert draft.values["contacts"] == ["+77011234567"]


def test_pending_confirmation_and_reply_are_recovered_before_new_updates():
    draft = _review_draft(53)
    polling, _ = BotPollingState.objects.get_or_create(bot_id=BOT_ID)
    event = ProcessedUpdate.objects.create(polling_state=polling, update_id=530)
    prepare_confirmation(53, draft.pk, draft.revision, event)
    sent_at = timezone.now()

    poll_count = 0

    async def get_updates(**kwargs):
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            return []
        raise asyncio.CancelledError

    bot = SimpleNamespace(
        session=SimpleNamespace(close=AsyncMock()),
        get_me=AsyncMock(return_value=SimpleNamespace(id=BOT_ID)),
        set_my_commands=AsyncMock(),
        set_chat_menu_button=AsyncMock(),
        get_updates=AsyncMock(side_effect=get_updates),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=900, date=sent_at)),
    )
    with (
        patch("leadflow.bot.runtime.Bot", return_value=bot),
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_polling("123456:fake-token-for-test-only"))

    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1
    assert not Draft.objects.filter(pk=draft.pk).exists()
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "Заявка принята. Спасибо!"
    bot.session.close.assert_awaited_once()


def test_confirmation_is_saved_then_creates_one_lead_even_when_update_replays():
    draft = _review_draft(52)
    update = _callback_update(
        303,
        52,
        make_callback("confirm", draft.pk, draft.revision),
    )

    process_update(BOT_ID, update)

    pending = Draft.objects.get(pk=draft.pk)
    assert pending.submission_state == "pending"
    assert pending.pending_update.update_id == 303
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0

    result = complete_pending_submission(draft.pk)
    replay = process_update(BOT_ID, update)

    assert result.lead.source == "telegram_bot"
    assert replay.duplicate
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1
    assert not Draft.objects.filter(pk=draft.pk).exists()
    assert OutboundMessage.objects.filter(
        processed_update__update_id=303,
        text="Заявка принята. Спасибо!",
    ).exists()


def test_complete_telegram_intake_creates_one_searchable_bot_lead():
    for code, name in SYSTEM_TAGS.items():
        Tag.objects.get_or_create(code=code, defaults={"name": name})
    process_update(BOT_ID, _message_update(600, 66, "/start"))
    direction = _button_data(600, "Сайт")
    _deliver_outbox(600)
    process_update(BOT_ID, _callback_update(601, 66, direction))
    continue_directions_action = _button_data(601, "Продолжить")
    _deliver_outbox(601)
    draft = Draft.objects.get(user_id=66)
    process_update(BOT_ID, _callback_update(602, 66, continue_directions_action))
    _deliver_outbox(602)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _message_update(
            603,
            66,
            "Нужен сайт для записи клиентов",
            date=int((draft.question_date + timedelta(seconds=1)).timestamp()),
            reply_to=draft.question_id,
        ),
    )
    request_continue = _button_data(603, "Продолжить")
    _deliver_outbox(603)
    process_update(BOT_ID, _callback_update(604, 66, request_continue))
    _deliver_outbox(604)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _message_update(
            605,
            66,
            "Мария",
            date=int((draft.question_date + timedelta(seconds=1)).timestamp()),
            reply_to=draft.question_id,
        ),
    )
    _deliver_outbox(605)
    draft.refresh_from_db()
    process_update(
        BOT_ID,
        _message_update(
            606,
            66,
            "maria@example.com",
            date=int((draft.question_date + timedelta(seconds=1)).timestamp()),
            reply_to=draft.question_id,
        ),
    )
    continue_contacts = _button_data(606, "К проверке")
    _deliver_outbox(606)
    process_update(BOT_ID, _callback_update(607, 66, continue_contacts))
    confirm = _button_data(607, "Отправить заявку")
    _deliver_outbox(607)
    draft.refresh_from_db()
    update = _callback_update(608, 66, confirm)
    process_update(BOT_ID, update)

    submission = complete_pending_submission(draft.pk)
    replay = process_update(BOT_ID, update)

    assert submission.lead.source == "telegram_bot"
    assert submission.lead.status == "new"
    assert submission.lead.request == "Нужен сайт для записи клиентов"
    assert list(submission.lead.contacts.values_list("value", flat=True)) == ["maria@example.com"]
    assert list(submission.lead.tags.values_list("code", flat=True)) == ["website"]
    assert replay.duplicate


def test_editing_request_source_updates_the_draft_but_not_a_submitted_lead():
    for code, name in SYSTEM_TAGS.items():
        Tag.objects.get_or_create(code=code, defaults={"name": name})
    draft = start_draft(68)
    draft = set_field(68, draft.pk, draft.revision, "direction", "website")
    draft = continue_directions(68, draft.pk, draft.revision)
    question_date = timezone.now().replace(microsecond=0)
    bind_question(68, draft.pk, draft.revision, 6800, question_date)
    process_update(
        BOT_ID,
        _message_update(
            6801,
            68,
            "Нужен сайт",
            date=int((question_date + timedelta(seconds=1)).timestamp()),
            reply_to=6800,
        ),
    )
    draft.refresh_from_db()
    assert draft.values["request"] == "Нужен сайт"

    process_update(BOT_ID, _edited_message_update(6802, 68, 6801, "Нужен сайт для бронирования"))
    draft.refresh_from_db()
    assert draft.values["request"] == "Нужен сайт для бронирования"
    assert OutboundMessage.objects.filter(
        processed_update__update_id=6802,
        text__contains="Исправление сохранено",
    ).exists()

    draft = continue_request(68, draft.pk, draft.revision)
    draft = set_field(68, draft.pk, draft.revision, "name", "Клиент")
    draft = set_field(68, draft.pk, draft.revision, "contacts", "client@example.com")
    draft = set_field(68, draft.pk, draft.revision, "continue_contacts", None)
    result = confirm_draft(68, draft.pk, draft.revision)
    current = start_draft(68)

    process_update(BOT_ID, _edited_message_update(6803, 68, 6801, "После отправки"))

    current.refresh_from_db()
    assert current.values == {}
    assert result.lead.request == "Нужен сайт для бронирования"
    assert OutboundMessage.objects.get(processed_update__update_id=6803).text.startswith(
        "Заявка уже принята"
    )
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1


def test_replacing_request_from_review_discards_the_old_text():
    draft = _review_draft(80)
    old_request = DraftInput.objects.get(draft=draft, field=DraftInput.Field.REQUEST)
    old_request.source_message_id = 8000
    old_request.save(update_fields=["source_message_id"])
    process_update(
        BOT_ID, _callback_update(8001, 80, make_callback("resume", draft.pk, draft.revision))
    )
    review = OutboundMessage.objects.get(processed_update__update_id=8001)
    control_id = 10000 + review.pk
    edit = _button_data(8001, "Исправить")
    _deliver_outbox(8001)
    process_update(BOT_ID, _callback_update(8002, 80, edit, message_id=control_id))
    request_menu = _button_data(8002, "Описание")
    _deliver_outbox(8002)
    process_update(BOT_ID, _callback_update(8003, 80, request_menu, message_id=control_id))
    replace_request = _button_data(8003, "Заменить описание")
    _deliver_outbox(8003)
    process_update(BOT_ID, _callback_update(8004, 80, replace_request, message_id=control_id))
    _deliver_outbox(8004)
    draft.refresh_from_db()

    process_update(
        BOT_ID,
        _message_update(
            8005,
            80,
            "Другой запрос",
            date=int((draft.question_date + timedelta(seconds=1)).timestamp()),
            reply_to=draft.question_id,
        ),
    )
    draft.refresh_from_db()
    old_request.refresh_from_db()

    assert draft.values["request"] == "Другой запрос"
    assert draft.step == Draft.Step.REVIEW
    assert not old_request.active and not old_request.editing_enabled
    process_update(BOT_ID, _edited_message_update(8006, 80, 8000, "Старый запрос"))
    draft.refresh_from_db()
    assert draft.values["request"] == "Другой запрос"


def test_overlimit_request_part_stays_removable_after_start_and_resume():
    draft = _request_prompt(81, 8100)
    question_id = draft.question_id
    asked_at = draft.question_date
    process_update(
        BOT_ID,
        _message_update(
            8101,
            81,
            "a" * 1500,
            date=int((asked_at + timedelta(seconds=1)).timestamp()),
            reply_to=question_id,
        ),
    )
    _deliver_outbox(8101)
    draft.refresh_from_db()
    next_question_date = draft.question_date
    process_update(
        BOT_ID,
        _message_update(
            8102,
            81,
            "b" * 600,
            date=int((next_question_date + timedelta(seconds=1)).timestamp()),
            reply_to=question_id,
        ),
    )
    assert _has_button(8102, "Удалить эту часть")
    _deliver_outbox(8102)

    process_update(BOT_ID, _message_update(8103, 81, "/start"))
    resume = _button_data(8103, "Продолжить")
    _deliver_outbox(8103)
    process_update(BOT_ID, _callback_update(8104, 81, resume))

    draft.refresh_from_db()
    assert draft.step == Draft.Step.REQUEST and draft.needs_correction
    assert _has_button(8104, "Удалить эту часть")


def test_single_overlimit_request_message_can_be_fixed_by_editing_its_source():
    draft = _request_prompt(82, 8200)
    question_id = draft.question_id
    asked_at = draft.question_date
    process_update(
        BOT_ID,
        _message_update(
            8201,
            82,
            "x" * 2100,
            date=int((asked_at + timedelta(seconds=1)).timestamp()),
            reply_to=question_id,
        ),
    )
    draft.refresh_from_db()
    assert draft.needs_correction

    process_update(BOT_ID, _edited_message_update(8202, 82, 8201, "Исправленное описание"))
    draft.refresh_from_db()

    assert draft.values["request"] == "Исправленное описание"
    assert not draft.needs_correction


def test_continue_directions_without_selection_keeps_the_direction_step():
    process_update(BOT_ID, _message_update(8301, 83, "/start"))
    draft = Draft.objects.get(user_id=83)
    continue_action = _button_data(8301, "Продолжить")

    process_update(BOT_ID, _callback_update(8302, 83, continue_action))
    draft.refresh_from_db()

    assert draft.step == Draft.Step.DIRECTION
    assert _has_button(8302, "Сайт")
    assert any(
        "выберите хотя бы одно направление" in message.text.casefold()
        for message in OutboundMessage.objects.filter(processed_update__update_id=8302)
    )


def test_start_acknowledges_the_last_completed_submission():
    draft = _review_draft(64)
    result = confirm_draft(64, draft.pk, draft.revision)

    process_update(BOT_ID, _message_update(404, 64, "/start"))

    message = OutboundMessage.objects.get(processed_update__update_id=404)
    assert "последняя заявка уже принята" in message.text
    assert result.lead.source == "telegram_bot"
    assert message.reply_markup["inline_keyboard"][0][0]["text"] == "Новая заявка"


def test_first_start_immediately_shows_multi_select_services():
    process_update(BOT_ID, _message_update(1400, 140, "/start"))

    draft = Draft.objects.get(user_id=140)
    messages = list(
        OutboundMessage.objects.filter(processed_update__update_id=1400).order_by("ordinal")
    )
    buttons = [
        button["text"]
        for message in messages
        for row in message.reply_markup.get("inline_keyboard", [])
        for button in row
    ]
    assert draft.step == Draft.Step.DIRECTION
    assert buttons == [
        "Сайт",
        "Реклама",
        "Автоматизация",
        "Другое",
        "Продолжить",
    ]
    assert any("Можно отметить несколько" in message.text for message in messages)


def test_direction_refresh_keeps_the_current_message_and_ignores_old_callbacks():
    process_update(BOT_ID, _message_update(1401, 141, "/start"))
    old_callback = _button_data(1401, "Сайт")
    old_message = OutboundMessage.objects.get(processed_update__update_id=1401)
    _deliver_outbox(1401)

    control_message_id = 10000 + old_message.pk
    process_update(
        BOT_ID,
        _callback_update(1402, 141, old_callback, message_id=control_message_id),
    )
    refresh = OutboundMessage.objects.filter(
        processed_update__update_id=1402,
        operation=OutboundMessage.Operation.EDIT_TEXT,
    ).get()
    assert refresh.target_message_id == control_message_id
    assert refresh.reply_markup["inline_keyboard"][0][0]["text"] == "✓ Сайт"

    process_update(
        BOT_ID,
        _callback_update(1403, 141, old_callback, message_id=control_message_id),
    )

    disable = OutboundMessage.objects.filter(
        processed_update__update_id=1403,
        operation=OutboundMessage.Operation.EDIT_MARKUP,
    )
    assert not disable.exists()
    draft = Draft.objects.get(user_id=141)
    assert control_message_id in draft.active_control_ids
    assert draft.values["directions"] == ["website"]
    assert draft.step == Draft.Step.DIRECTION


def test_direction_keyboard_is_removed_when_the_bot_asks_for_the_request():
    process_update(BOT_ID, _message_update(1410, 141, "/start"))
    direction_message = OutboundMessage.objects.get(processed_update__update_id=1410)
    message_id = 10000 + direction_message.pk
    website = _button_data(1410, "Сайт")
    _deliver_outbox(1410)
    process_update(BOT_ID, _callback_update(1411, 141, website, message_id=message_id))
    continue_data = _button_data(1411, "Продолжить")
    _deliver_outbox(1411)

    process_update(
        BOT_ID,
        _callback_update(1412, 141, continue_data, message_id=message_id),
    )
    messages = list(
        OutboundMessage.objects.filter(processed_update__update_id=1412).order_by("ordinal")
    )
    assert [message.operation for message in messages] == [
        OutboundMessage.Operation.EDIT_MARKUP,
        OutboundMessage.Operation.SEND,
    ]
    assert messages[0].reply_markup == {}
    assert messages[1].reply_markup == {}
    assert "Опишите, что нужно сделать." in messages[1].text


def test_start_shows_saved_values_and_resume_actions_for_an_active_draft():
    draft = _review_draft(69)

    process_update(BOT_ID, _message_update(690, 69, "/start"))

    messages = list(
        OutboundMessage.objects.filter(processed_update__update_id=690).order_by("ordinal")
    )
    assert any("Тестовый клиент" in message.text for message in messages)
    assert any("Нужен сайт" in message.text for message in messages)
    buttons = [
        button["text"]
        for message in messages
        for row in message.reply_markup.get("inline_keyboard", [])
        for button in row
    ]
    assert "Продолжить" in buttons
    assert "Начать заново" in buttons
    assert "Отменить заявку" in buttons
    assert Draft.objects.get(pk=draft.pk).values == draft.values


@pytest.mark.parametrize(
    ("action", "expected_request"),
    [("review_add", "Нужен сайт\n\nУточнение"), ("review_discard", "Нужен сайт")],
)
def test_unsolicited_review_text_requires_an_explicit_add_or_discard(action, expected_request):
    draft = _review_draft(69)
    process_update(BOT_ID, _message_update(691, 69, "Уточнение"))
    draft.refresh_from_db()

    assert draft.pending_inputs == [{"message_id": 691, "text": "Уточнение"}]
    assert not _has_button(691, "Отправить заявку")
    label = "Добавить к описанию" if action == "review_add" else "Не добавлять"
    decision = _button_data(691, label)
    process_update(BOT_ID, _callback_update(692, 69, decision))

    draft.refresh_from_db()
    assert draft.values["request"] == expected_request
    assert draft.pending_inputs == []
    assert _has_button(692, "Отправить заявку")


def test_pending_review_text_can_be_corrected_before_the_user_decides():
    draft = _review_draft(70)
    process_update(BOT_ID, _message_update(701, 70, "Уточнение"))
    process_update(BOT_ID, _edited_message_update(702, 70, 701, "Уточнение с деталями"))
    draft.refresh_from_db()

    assert draft.pending_inputs == [{"message_id": 701, "text": "Уточнение с деталями"}]
    add_data = _button_data(702, "Добавить к описанию")
    process_update(BOT_ID, _callback_update(703, 70, add_data))
    draft.refresh_from_db()

    assert draft.values["request"] == "Нужен сайт\n\nУточнение с деталями"
    assert draft.pending_inputs == []


def test_old_confirmation_reports_success_without_touching_a_new_draft():
    old_draft = _review_draft(67)
    result = confirm_draft(67, old_draft.pk, old_draft.revision)
    current = _name_step_draft(67)
    current = set_field(67, current.pk, current.revision, "name", "Новая заявка")

    process_update(
        BOT_ID,
        _callback_update(
            670,
            67,
            make_callback("confirm", old_draft.pk, old_draft.revision),
        ),
    )

    restored = Draft.objects.get(user_id=67)
    response = OutboundMessage.objects.get(
        processed_update__update_id=670,
        text="Эта заявка уже принята. Спасибо!",
    )
    assert restored.pk == current.pk
    assert restored.values["name"] == "Новая заявка"
    assert response.text == "Эта заявка уже принята. Спасибо!"
    assert result.lead.pk == SubmissionReceipt.objects.get(pk=old_draft.pk).lead_id
    assert Lead.objects.count() == 1


def test_old_new_request_button_does_not_offer_changes_while_confirmation_is_pending():
    draft = _review_draft(68)
    process_update(
        BOT_ID,
        _callback_update(
            680,
            68,
            make_callback("confirm", draft.pk, draft.revision),
        ),
    )

    process_update(BOT_ID, _callback_update(681, 68, make_callback("new")))

    pending = Draft.objects.get(pk=draft.pk)
    reply = OutboundMessage.objects.get(
        processed_update__update_id=681,
        text="Проверяем отправку. Вводить данные заново не нужно.",
    )
    assert pending.submission_state == "pending"
    assert "Проверяем отправку" in reply.text
    assert "Начать заново" not in reply.text
    assert Lead.objects.count() == 0


def test_cancel_of_a_populated_draft_requires_and_accepts_confirmation():
    draft = _review_draft(65)
    process_update(
        BOT_ID,
        _callback_update(505, 65, make_callback("cancel", draft.pk, draft.revision)),
    )

    waiting = Draft.objects.get(pk=draft.pk)
    assert waiting.pending_action == Draft.PendingAction.CANCEL
    assert Lead.objects.count() == 0

    process_update(
        BOT_ID,
        _callback_update(
            506,
            65,
            make_callback("confirm_cancel", draft.pk, waiting.revision),
        ),
    )

    assert not Draft.objects.filter(pk=draft.pk).exists()
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("action", ["cancel", "restart"])
def test_paused_menu_cancel_and_restart_buttons_complete_the_selected_action(populated, action):
    draft = _review_draft(84) if populated else start_draft(84)
    draft.step = Draft.Step.PAUSED
    draft.return_step = Draft.Step.REVIEW if populated else Draft.Step.DIRECTION
    draft.save(update_fields=["step", "return_step"])
    process_update(BOT_ID, _message_update(841, 84, "/start"))
    draft.refresh_from_db()
    assert draft.step == Draft.Step.PAUSED
    label = "Отменить заявку" if action == "cancel" else "Начать заново"

    process_update(BOT_ID, _callback_update(842, 84, _button_data(841, label)))
    if populated:
        draft.refresh_from_db()
        assert draft.pending_action == action
        label = "Удалить заявку" if action == "cancel" else "Удалить и начать заново"
        process_update(BOT_ID, _callback_update(843, 84, _button_data(842, label)))

    assert not Draft.objects.filter(pk=draft.pk).exists()
    if action == "restart":
        replacement = Draft.objects.get(user_id=84)
        assert replacement.values == {}
        assert replacement.step == Draft.Step.DIRECTION
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 0


def test_callback_data_stays_within_telegram_limit():
    value = make_callback("confirm", "12345678-1234-5678-1234-567812345678", 123456)
    assert len(value.encode("utf-8")) <= 64


def test_help_is_a_short_standalone_answer_and_unknown_commands_are_not_saved_as_input():
    draft = _name_step_draft(145)
    asked_at = timezone.now().replace(microsecond=0)
    bind_question(145, draft.pk, draft.revision, 14500, asked_at)

    process_update(BOT_ID, _message_update(1451, 145, "/help"))
    process_update(
        BOT_ID,
        _message_update(1452, 145, "/unknown", date=int(asked_at.timestamp()) + 2),
    )

    draft.refresh_from_db()
    assert "name" not in draft.values
    assert draft.step == Draft.Step.NAME
    help_message = OutboundMessage.objects.get(processed_update__update_id=1451)
    unknown_message = OutboundMessage.objects.get(processed_update__update_id=1452)
    assert "/back" in help_message.text and "/cancel" in help_message.text
    assert "/help" in unknown_message.text


def test_name_step_uses_one_clear_question_without_a_separate_actions_message():
    draft = _name_step_draft(146)

    process_update(
        BOT_ID,
        _callback_update(1461, 146, make_callback("resume", draft.pk, draft.revision)),
    )

    messages = list(
        OutboundMessage.objects.filter(processed_update__update_id=1461).order_by("ordinal")
    )
    assert len(messages) == 1
    assert "Как к вам обращаться?" in messages[0].text
    assert "Действия для шага" not in messages[0].text


def test_review_starts_with_three_primary_actions_then_opens_paginated_edit_menus():
    draft = _review_draft(147)
    draft.values["contacts"] = [f"client{index}@example.com" for index in range(7)]
    draft.save(update_fields=["values"])
    process_update(
        BOT_ID,
        _callback_update(1471, 147, make_callback("resume", draft.pk, draft.revision)),
    )

    primary = OutboundMessage.objects.get(processed_update__update_id=1471)
    assert [
        button["text"] for row in primary.reply_markup["inline_keyboard"] for button in row
    ] == ["Отправить заявку", "Исправить", "Отменить заявку"]
    edit_data = _button_data(1471, "Исправить")
    _deliver_outbox(1471)
    menu_message_id = 10000 + primary.pk

    process_update(
        BOT_ID,
        _callback_update(
            1472,
            147,
            edit_data,
            message_id=menu_message_id,
        ),
    )
    edit_menu = OutboundMessage.objects.get(processed_update__update_id=1472)
    assert edit_menu.operation == OutboundMessage.Operation.EDIT_TEXT
    assert edit_menu.target_message_id == menu_message_id
    assert {
        button["text"] for row in edit_menu.reply_markup["inline_keyboard"] for button in row
    } >= {
        "Имя",
        "Контакты",
        "Направления",
        "Описание",
        "К проверке",
    }
    contacts_data = _button_data(1472, "Контакты")
    _deliver_outbox(1472)

    process_update(
        BOT_ID,
        _callback_update(
            1473,
            147,
            contacts_data,
            message_id=menu_message_id,
        ),
    )
    first_page = OutboundMessage.objects.get(processed_update__update_id=1473)
    labels = [
        button["text"] for row in first_page.reply_markup["inline_keyboard"] for button in row
    ]
    assert first_page.operation == OutboundMessage.Operation.EDIT_TEXT
    assert "client0@example.com" in labels[0]
    assert "Следующие" in labels
    assert not any("client5@example.com" in label for label in labels)
    next_data = _button_data(1473, "Следующие")
    _deliver_outbox(1473)

    process_update(
        BOT_ID,
        _callback_update(
            1474,
            147,
            next_data,
            message_id=menu_message_id,
        ),
    )
    second_page = OutboundMessage.objects.get(processed_update__update_id=1474)
    second_labels = [
        button["text"] for row in second_page.reply_markup["inline_keyboard"] for button in row
    ]
    assert "client5@example.com" in second_labels[0]
    assert "Предыдущие" in second_labels


def test_back_command_returns_from_review_submenus_to_the_parent_menu():
    draft = _review_draft(150)
    process_update(
        BOT_ID,
        _callback_update(1501, 150, make_callback("resume", draft.pk, draft.revision)),
    )
    review = OutboundMessage.objects.get(processed_update__update_id=1501)
    control_id = 10000 + review.pk
    edit = _button_data(1501, "Исправить")
    _deliver_outbox(1501)

    process_update(BOT_ID, _callback_update(1502, 150, edit, message_id=control_id))
    contacts = _button_data(1502, "Контакты")
    _deliver_outbox(1502)
    process_update(BOT_ID, _callback_update(1503, 150, contacts, message_id=control_id))
    _deliver_outbox(1503)

    process_update(BOT_ID, _message_update(1504, 150, "/back"))
    draft.refresh_from_db()
    assert draft.review_ui["view"] == "edit"
    parent = OutboundMessage.objects.get(processed_update__update_id=1504)
    assert parent.operation == OutboundMessage.Operation.EDIT_TEXT
    assert parent.target_message_id == control_id
    _deliver_outbox(1504)

    process_update(BOT_ID, _message_update(1505, 150, "/back"))
    draft.refresh_from_db()
    assert draft.review_ui["view"] == "home"
    home = OutboundMessage.objects.get(processed_update__update_id=1505)
    assert home.operation == OutboundMessage.Operation.EDIT_TEXT
    assert home.target_message_id == control_id


def test_request_parts_are_paginated_inside_the_request_edit_menu():
    draft = _review_draft(151)
    existing_part = DraftInput.objects.get(draft=draft, field=DraftInput.Field.REQUEST)
    existing_part.accepted_text = "Фрагмент 1"
    existing_part.save(update_fields=["accepted_text"])
    parts = [existing_part]
    for position in range(1, 7):
        parts.append(
            DraftInput.objects.create(
                draft=draft,
                field=DraftInput.Field.REQUEST,
                position=position,
                accepted_text=f"Фрагмент {position + 1}",
            )
        )
    draft.values["request"] = "\n\n".join(part.accepted_text for part in parts)
    draft.save(update_fields=["values"])

    process_update(
        BOT_ID,
        _callback_update(1511, 151, make_callback("resume", draft.pk, draft.revision)),
    )
    review = OutboundMessage.objects.get(processed_update__update_id=1511)
    control_id = 10000 + review.pk
    edit = _button_data(1511, "Исправить")
    _deliver_outbox(1511)
    process_update(BOT_ID, _callback_update(1512, 151, edit, message_id=control_id))
    request_menu = _button_data(1512, "Описание")
    _deliver_outbox(1512)
    process_update(BOT_ID, _callback_update(1513, 151, request_menu, message_id=control_id))

    first_page = OutboundMessage.objects.get(processed_update__update_id=1513)
    first_labels = [
        button["text"] for row in first_page.reply_markup["inline_keyboard"] for button in row
    ]
    assert any(label.startswith("Удалить фрагмент 1:") for label in first_labels)
    assert any(label.startswith("Удалить фрагмент 5:") for label in first_labels)
    assert not any(label.startswith("Удалить фрагмент 6:") for label in first_labels)
    next_page = _button_data(1513, "Следующие")
    _deliver_outbox(1513)

    process_update(BOT_ID, _callback_update(1514, 151, next_page, message_id=control_id))
    second_labels = [
        button["text"]
        for row in OutboundMessage.objects.get(processed_update__update_id=1514).reply_markup[
            "inline_keyboard"
        ]
        for button in row
    ]
    assert any(label.startswith("Удалить фрагмент 6:") for label in second_labels)
    assert any(label.startswith("Удалить фрагмент 7:") for label in second_labels)


def test_old_review_button_does_not_remove_the_current_menu_keyboard():
    draft = _review_draft(148)
    process_update(
        BOT_ID,
        _callback_update(1481, 148, make_callback("resume", draft.pk, draft.revision)),
    )
    old_confirm = _button_data(1481, "Отправить заявку")
    edit_data = _button_data(1481, "Исправить")
    menu_message = OutboundMessage.objects.get(processed_update__update_id=1481)
    _deliver_outbox(1481)
    menu_message_id = 10000 + menu_message.pk

    process_update(
        BOT_ID,
        _callback_update(1482, 148, edit_data, message_id=menu_message_id),
    )
    _deliver_outbox(1482)
    process_update(
        BOT_ID,
        _callback_update(1483, 148, old_confirm, message_id=menu_message_id),
    )

    assert not OutboundMessage.objects.filter(
        processed_update__update_id=1483,
        operation=OutboundMessage.Operation.EDIT_MARKUP,
    ).exists()
    assert Draft.objects.get(pk=draft.pk).step == Draft.Step.REVIEW


def test_contact_question_places_phone_and_username_choices_on_one_keyboard():
    draft = _contact_draft(149)
    BotUser.objects.filter(pk=149).update(username="client_name")
    process_update(
        BOT_ID,
        _callback_update(1491, 149, make_callback("resume", draft.pk, draft.revision)),
    )

    messages = list(
        OutboundMessage.objects.filter(processed_update__update_id=1491).order_by("ordinal")
    )
    assert len(messages) == 1
    keyboard = messages[0].reply_markup["keyboard"]
    labels = [button["text"] for row in keyboard for button in row]
    assert "Отправить мой номер" in labels
    assert "Использовать @client_name" in labels
    assert "Действия для шага" not in messages[0].text
    _deliver_outbox(1491)
    draft.refresh_from_db()

    process_update(
        BOT_ID,
        _message_update(
            1492,
            149,
            "Использовать @client_name",
            date=int(draft.question_date.timestamp()) + 1,
        ),
    )

    draft.refresh_from_db()
    assert draft.values["contacts"] == ["@client_name"]
    assert draft.step == Draft.Step.CONTACT_CHOICE

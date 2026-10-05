import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.utils import timezone

from leadflow.bot.handlers import get_pending_message
from leadflow.bot.handlers import mark_message_failed
from leadflow.bot.handlers import process_update
from leadflow.bot.models import Draft
from leadflow.bot.models import OutboundMessage
from leadflow.bot.services import append_review_input
from leadflow.crm.validation import InputError
from tests.test_bot_transport import BOT_ID
from tests.test_bot_transport import _message_update

pytestmark = pytest.mark.django_db(transaction=True)


def test_retry_after_blocks_other_chats_and_preserves_full_delay():
    process_update(BOT_ID, _message_update(1, 42, "/start"))
    process_update(BOT_ID, _message_update(2, 43, "/start"))
    message = OutboundMessage.objects.first()
    now = timezone.now()
    mark_message_failed(message.pk, "TelegramRetryAfter", retry_after=7200)
    assert get_pending_message() is None
    message.refresh_from_db()
    assert message.next_attempt_at >= now + timedelta(seconds=7200)


def test_review_pending_input_has_count_and_total_size_bounds():
    process_update(BOT_ID, _message_update(1, 42, "/start"))
    draft = Draft.objects.get(user_id=42)
    draft.step = "review"
    draft.save()
    for number in range(10):
        draft = append_review_input(42, draft.pk, draft.revision, "x", number + 100)
    before = list(draft.pending_inputs)
    with pytest.raises(InputError):
        append_review_input(42, draft.pk, draft.revision, "extra", 200)
    draft.refresh_from_db()
    assert draft.pending_inputs == before
    draft.pending_inputs = []
    draft.save()
    for number in range(2):
        draft = append_review_input(42, draft.pk, draft.revision, "x" * 2000, number + 300)
    with pytest.raises(InputError):
        append_review_input(42, draft.pk, draft.revision, "x", 400)


def test_intake_rate_guard_bounds_notices_and_preserves_accepted_text(settings):
    settings.BOT_UPDATE_RATE_LIMIT = 3
    process_update(BOT_ID, _message_update(1, 42, "/start"))
    draft = Draft.objects.get(user_id=42)
    draft.step = "review"
    draft.save()
    process_update(BOT_ID, _message_update(2, 42, "first"))
    process_update(BOT_ID, _message_update(3, 42, "second"))
    for number in range(4, 20):
        process_update(BOT_ID, _message_update(number, 42, "excess"))
    draft.refresh_from_db()
    assert [item["text"] for item in draft.pending_inputs] == ["first", "second"]
    assert OutboundMessage.objects.filter(text__contains="слишком часто").count() == 1
    process_update(BOT_ID, _message_update(20, 43, "/start"))
    assert Draft.objects.filter(user_id=43).exists()


def test_polling_lease_exclusive_release_takeover_and_connection_loss():
    from leadflow.bot.lease import PollingLease
    from leadflow.bot.lease import PollingLeaseError

    first = PollingLease(BOT_ID)
    second = PollingLease(BOT_ID)
    try:
        first.acquire()
        first.check()
        with pytest.raises(PollingLeaseError):
            second.acquire()
        first.close()
        second.acquire()
        second.check()
        second.connection.close()
        with pytest.raises(PollingLeaseError):
            second.check()
    finally:
        first.close()
        second.close()


def test_lost_lease_prevents_any_telegram_call():
    from leadflow.bot.lease import PollingLeaseError
    from leadflow.bot.runtime import ProtectedTransport

    class LostLease:
        def check(self):
            raise PollingLeaseError("lost")

    telegram = SimpleNamespace(send_message=AsyncMock())
    transport = ProtectedTransport(telegram, BOT_ID, LostLease())
    with pytest.raises(PollingLeaseError):
        asyncio.run(transport.send_message(chat_id=42, text="test"))
    telegram.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "method",
    [
        "get_me",
        "set_my_commands",
        "set_chat_menu_button",
        "answer_callback_query",
        "send_message",
        "edit_message_text",
        "edit_message_reply_markup",
        "get_updates",
    ],
)
def test_every_transport_method_obeys_persisted_cooldown(method):
    from aiogram.exceptions import TelegramRetryAfter

    from leadflow.bot.lease import PollingLease
    from leadflow.bot.runtime import ProtectedTransport
    from leadflow.bot.runtime import TransportCoolingDown

    lease = PollingLease(BOT_ID)
    lease.acquire()
    call = AsyncMock(
        side_effect=TelegramRetryAfter(method=None, message="limited", retry_after=7200)
    )
    bot = SimpleNamespace(**{method: call})
    transport = ProtectedTransport(bot, BOT_ID, lease)
    try:
        with pytest.raises(TelegramRetryAfter):
            asyncio.run(getattr(transport, method)())
        # A new wrapper/process reads the same committed deadline.
        with pytest.raises(TransportCoolingDown):
            asyncio.run(getattr(ProtectedTransport(bot, BOT_ID, lease), method)())
        call.assert_awaited_once()
    finally:
        lease.close()


def test_lease_check_rejects_a_live_connection_that_lost_the_lock():
    from leadflow.bot.lease import PollingLease
    from leadflow.bot.lease import PollingLeaseError

    lease = PollingLease(BOT_ID)
    try:
        lease.acquire()
        lease.connection.execute("SELECT pg_advisory_unlock_all()")
        with pytest.raises(PollingLeaseError):
            lease.check()
    finally:
        lease.close()


def test_rate_guard_replay_does_not_spend_quota_and_window_recovers(settings):
    from leadflow.bot.models import BotUser

    settings.BOT_UPDATE_RATE_LIMIT = 2
    process_update(BOT_ID, _message_update(1, 42, "/start"))
    process_update(BOT_ID, _message_update(1, 42, "/start"))
    process_update(BOT_ID, _message_update(2, 42, "/help"))
    user = BotUser.objects.get(pk=42)
    assert user.intake_count == 2
    user.intake_window_started = timezone.now() - timedelta(seconds=61)
    user.save()
    process_update(BOT_ID, _message_update(3, 42, "/help"))
    user.refresh_from_db()
    assert user.intake_count == 1
    assert not user.intake_notice_sent


def test_transport_rejects_results_if_lease_is_lost_during_long_poll():
    from asgiref.sync import sync_to_async

    from leadflow.bot.lease import PollingLease
    from leadflow.bot.lease import PollingLeaseError
    from leadflow.bot.runtime import ProtectedTransport

    lease = PollingLease(BOT_ID)
    lease.acquire()

    async def long_poll():
        await sync_to_async(lease.connection.execute)("SELECT pg_advisory_unlock_all()")
        return [_message_update(1, 42, "/start")]

    transport = ProtectedTransport(SimpleNamespace(get_updates=long_poll), BOT_ID, lease)
    try:
        with pytest.raises(PollingLeaseError):
            asyncio.run(transport.get_updates())
        assert not Draft.objects.exists()
    finally:
        lease.close()


def test_pending_source_edit_cannot_exceed_total_limit():
    from leadflow.bot.services import edit_input_message

    process_update(BOT_ID, _message_update(1, 42, "/start"))
    draft = Draft.objects.get(user_id=42)
    draft.step = "review"
    draft.save()
    for index, text in enumerate(["x" * 2000, "y" * 1999, "z"]):
        draft = append_review_input(42, draft.pk, draft.revision, text, index + 100)
    saved = draft.pending_inputs
    _, accepted = edit_input_message(42, draft.pk, draft.revision, 102, "zz")
    assert not accepted
    draft.refresh_from_db()
    assert draft.pending_inputs == saved


def test_group_flood_never_sends_private_data_or_rate_notices_to_group(settings):
    settings.BOT_UPDATE_RATE_LIMIT = 1
    for update_id in range(1, 5):
        update = _message_update(update_id, 42, "/start")
        update["message"]["chat"] = {"id": -10042, "type": "supergroup", "title": "Test"}
        process_update(BOT_ID, update)
    assert not Draft.objects.exists()
    assert OutboundMessage.objects.count() == 1
    message = OutboundMessage.objects.get()
    assert message.chat_id == -10042
    assert "личном чате" in message.text

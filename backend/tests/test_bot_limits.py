import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.utils import timezone

from leadflow.bot.handlers import process_update
from leadflow.bot.models import BotUser
from leadflow.bot.models import Draft
from leadflow.bot.models import OutboundMessage
from leadflow.bot.models import ProcessedUpdate
from leadflow.bot.runtime import _deliver_pending_messages
from leadflow.bot.services import set_field
from leadflow.bot.services import start_draft
from leadflow.crm.validation import InputError
from tests.test_bot_transport import BOT_ID
from tests.test_bot_transport import _message_update

pytestmark = pytest.mark.django_db(transaction=True)


def test_group_updates_have_no_outbox_or_user_history():
    update = _message_update(1, 42, "/start")
    update["message"]["chat"] = {"id": -10042, "type": "supergroup", "title": "Synthetic"}
    process_update(BOT_ID, update)
    assert not OutboundMessage.objects.exists()
    assert not ProcessedUpdate.objects.exists()
    assert not BotUser.objects.exists()


def test_queue_full_does_not_accept_input_or_exceed_capacity(settings):
    settings.BOT_CHAT_QUEUE_LIMIT = 2
    process_update(BOT_ID, _message_update(1, 42, "/start"))
    draft = Draft.objects.get(user_id=42)
    draft.step = "review"
    draft.save()
    process_update(BOT_ID, _message_update(2, 42, "/help"))
    before = dict(draft.values)
    process_update(BOT_ID, _message_update(3, 42, "must not be accepted"))
    draft.refresh_from_db()
    assert draft.values == before
    assert draft.pending_inputs == []
    assert OutboundMessage.objects.filter(status="pending").count() <= 2


def test_delivery_reserves_pacing_before_a_provider_call():
    process_update(BOT_ID, _message_update(1, 42, "/start"))
    process_update(BOT_ID, _message_update(2, 42, "/help"))
    provider = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=700, date=timezone.now()))
    )
    asyncio.run(_deliver_pending_messages(provider))
    assert provider.send_message.await_count == 1
    assert OutboundMessage.objects.filter(status="pending").count() == 1


def test_bot_contact_cap_preserves_existing_values_on_rejection():
    draft = start_draft(42)
    draft.step = "contacts"
    draft.save()
    for index in range(20):
        draft.step = "contacts"
        draft.save()
        draft = set_field(42, draft.pk, draft.revision, "contacts", f"user{index}@example.test")
    saved = list(draft.values["contacts"])
    draft.step = "contacts"
    draft.save()
    with pytest.raises(InputError):
        set_field(42, draft.pk, draft.revision, "contacts", "extra@example.test")
    draft.refresh_from_db()
    assert draft.values["contacts"] == saved
    assert draft.inputs.filter(active=True).count() == 20


def test_capacity_notices_skip_full_chats_before_selecting_the_batch(settings):
    from leadflow.bot.handlers import flush_capacity_notices

    settings.BOT_CHAT_QUEUE_LIMIT = 1
    for chat in range(1, 22):
        process_update(BOT_ID, _message_update(chat * 10, chat, "/start"))
        process_update(BOT_ID, _message_update(chat * 10 + 1, chat, "/help"))
    OutboundMessage.objects.filter(chat_id=21).update(status="delivered", text="", reply_markup={})
    flush_capacity_notices(BOT_ID)
    assert BotUser.objects.get(pk=21).capacity_notice_update_id is None
    assert OutboundMessage.objects.filter(chat_id=21, status="pending").count() == 1
    assert BotUser.objects.exclude(pk=21).filter(capacity_notice_update__isnull=False).count() == 20


def test_cleanup_preserves_pending_work_and_durable_receipts():
    from datetime import timedelta
    from uuid import uuid4

    from leadflow.bot.maintenance import cleanup_history_batch
    from leadflow.bot.models import BotPollingState
    from leadflow.crm.models import SubmissionReceipt
    from leadflow.crm.services import create_lead

    for index in range(1, 6):
        process_update(BOT_ID, _message_update(index, index, "/start"))
    old = timezone.now() - timedelta(days=8)
    ProcessedUpdate.objects.exclude(update_id=5).update(created_at=old)
    OutboundMessage.objects.exclude(processed_update__update_id=1).update(
        status="delivered", text="", reply_markup={}
    )
    event2 = ProcessedUpdate.objects.get(update_id=2)
    Draft.objects.filter(user_id=2).update(submission_state="pending", pending_update=event2)
    BotUser.objects.filter(pk=3).update(
        capacity_notice_update=ProcessedUpdate.objects.get(update_id=3)
    )
    create_lead(uuid4(), {"name": "Synthetic", "request": "Test", "contacts": ["a@example.test"]})
    offset = BotPollingState.objects.get(pk=BOT_ID).next_offset
    assert cleanup_history_batch(BOT_ID, dry_run=True) == 1
    assert ProcessedUpdate.objects.count() == 5
    assert cleanup_history_batch(BOT_ID) == 1
    assert list(
        ProcessedUpdate.objects.order_by("update_id").values_list("update_id", flat=True)
    ) == [1, 2, 3, 5]
    assert SubmissionReceipt.objects.count() == 1
    assert BotPollingState.objects.get(pk=BOT_ID).next_offset == offset


def test_closed_input_history_is_bounded_without_removing_live_or_pending_inputs():
    from leadflow.bot.limits import prune_closed_inputs
    from leadflow.bot.models import DraftInput

    draft = start_draft(42)
    for position in range(80):
        DraftInput.objects.create(
            draft=draft,
            field="contact",
            position=position,
            active=False,
            editing_enabled=False,
            accepted_text="synthetic@example.test",
        )
    active = DraftInput.objects.create(
        draft=draft, field="contact", position=80, accepted_text="active@example.test"
    )
    pending = DraftInput.objects.create(
        draft=draft,
        field="contact",
        position=81,
        active=False,
        editing_enabled=False,
        accepted_text="old",
        pending_text="needs correction",
    )
    prune_closed_inputs(draft)
    assert draft.inputs.count() == 34
    assert draft.inputs.filter(active=False, pending_text="", accepted_text="").count() == 32
    active.refresh_from_db()
    pending.refresh_from_db()
    assert active.accepted_text == "active@example.test"
    assert pending.pending_text == "needs correction"
    assert draft.inputs.filter(position=79).exists()


def test_legacy_group_outbox_does_not_call_provider_or_create_invalid_bot_user():
    process_update(BOT_ID, _message_update(1, 42, "/start"))
    OutboundMessage.objects.update(chat_id=-10042)
    bot = SimpleNamespace(send_message=AsyncMock())
    asyncio.run(_deliver_pending_messages(bot))
    bot.send_message.assert_not_awaited()
    assert OutboundMessage.objects.get().status == "failed"


def test_bootstrap_retries_transient_database_errors():
    from unittest.mock import patch

    from django.db import OperationalError

    from leadflow.bot.runtime import _bootstrap_delivery

    database = AsyncMock(side_effect=[OperationalError("synthetic outage"), None])
    with (
        patch("leadflow.bot.runtime._database", database),
        patch("leadflow.bot.runtime._complete_due_submissions", AsyncMock()),
        patch("leadflow.bot.runtime._deliver_pending_messages", AsyncMock()),
        patch("leadflow.bot.runtime.asyncio.sleep", AsyncMock()) as sleep,
    ):
        asyncio.run(_bootstrap_delivery(SimpleNamespace(), BOT_ID))
    assert database.await_count == 2
    sleep.assert_awaited_once_with(1)

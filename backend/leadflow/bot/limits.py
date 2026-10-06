"""Durable queue capacity, delivery pacing and bounded closed input history."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import BotPollingState
from .models import BotUser
from .models import DraftInput
from .models import OutboundMessage


class QueueFull(Exception):
    pass


def check_queue_capacity(event, chat_id):
    # Every writer locks this row before user/draft rows; delivery only reduces capacity.
    BotPollingState.objects.select_for_update().get(pk=event.polling_state_id)
    pending = OutboundMessage.objects.filter(
        processed_update__polling_state_id=event.polling_state_id, status="pending"
    )
    if pending.count() >= getattr(settings, "BOT_QUEUE_LIMIT", 1024) or pending.filter(
        chat_id=chat_id
    ).count() >= getattr(settings, "BOT_CHAT_QUEUE_LIMIT", 64):
        raise QueueFull


def reserve_delivery(message_id):
    with transaction.atomic():
        message = OutboundMessage.objects.select_related("processed_update").get(pk=message_id)
        state = BotPollingState.objects.select_for_update().get(
            pk=message.processed_update.polling_state_id
        )
        user, _ = BotUser.objects.get_or_create(pk=message.chat_id)
        user = BotUser.objects.select_for_update().get(pk=user.pk)
        now = timezone.now()
        deadlines = [state.outbound_retry_at, state.outbound_next_at, user.outbound_next_at]
        if any(deadline and deadline > now for deadline in deadlines):
            return False
        state.outbound_next_at = now + timedelta(milliseconds=100)
        state.save(update_fields=["outbound_next_at"])
        user.outbound_next_at = now + timedelta(seconds=1)
        user.outbound_last_at = now
        user.save(update_fields=["outbound_next_at", "outbound_last_at"])
        return True


def prune_closed_inputs(draft):
    for field in DraftInput.Field.values:
        closed = DraftInput.objects.filter(
            draft=draft, field=field, active=False, editing_enabled=False, pending_text=""
        )
        keep = list(closed.order_by("-position").values_list("pk", flat=True)[:32])
        closed.exclude(pk__in=keep).delete()
        closed.filter(pk__in=keep).exclude(accepted_text="").update(accepted_text="")

"""Delete only terminal transport history; preserve durable business receipts."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import BotPollingState
from .models import ProcessedUpdate


def cleanup_history_batch(bot_id, *, dry_run=False, batch_size=1000):
    with transaction.atomic():
        BotPollingState.objects.select_for_update().get(pk=bot_id)
        candidates = (
            ProcessedUpdate.objects.filter(
                polling_state_id=bot_id, created_at__lt=timezone.now() - timedelta(days=7)
            )
            .exclude(outboundmessage__status="pending")
            .filter(pending_drafts__isnull=True, capacity_notices__isnull=True)
            .order_by("pk")
        )
        ids = list(candidates.values_list("pk", flat=True)[:batch_size])
        if not dry_run:
            ProcessedUpdate.objects.filter(pk__in=ids).delete()
        return len(ids)

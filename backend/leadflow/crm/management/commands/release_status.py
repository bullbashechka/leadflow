"""Read aggregate release health without returning customer or credential data."""

import json

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.db.models import Count
from django.db.models import Max
from django.db.models import Min
from django.db.models import Q
from django.utils import timezone

from leadflow.bot.models import BotPollingState
from leadflow.bot.models import Draft
from leadflow.bot.models import OutboundMessage
from leadflow.bot.models import ProcessedUpdate


class Command(BaseCommand):
    help = "Read privacy-safe aggregate database and bot delivery status."

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-pending-age",
            type=int,
            help="Exit unsuccessfully if the oldest pending delivery exceeds this many seconds.",
        )

    def handle(self, *args, **options):
        max_age = options["max_pending_age"]
        if max_age is not None and max_age < 0:
            raise CommandError("--max-pending-age must be nonnegative.")
        now = timezone.now()
        try:
            queue = OutboundMessage.objects.aggregate(
                pending=Count("pk", filter=Q(status=OutboundMessage.Status.PENDING)),
                due=Count(
                    "pk",
                    filter=Q(status=OutboundMessage.Status.PENDING, next_attempt_at__lte=now),
                ),
                failed=Count("pk", filter=Q(status=OutboundMessage.Status.FAILED)),
                oldest=Min("created_at", filter=Q(status=OutboundMessage.Status.PENDING)),
            )
            latest_update = ProcessedUpdate.objects.aggregate(latest=Max("created_at"))["latest"]
            cooldown = BotPollingState.objects.aggregate(latest=Max("outbound_retry_at"))["latest"]
            pending_submissions = Draft.objects.filter(submission_state="pending").count()
        except DatabaseError:
            raise CommandError("Operational status unavailable: database read failed.") from None
        oldest = queue.pop("oldest")
        age = max(0, int((now - oldest).total_seconds())) if oldest else 0
        result = {
            "database": "ok",
            "bot": {
                **queue,
                "oldest_pending_age_seconds": age,
                "last_processed_update_age_seconds": (
                    max(0, int((now - latest_update).total_seconds())) if latest_update else None
                ),
                "cooldown_seconds": (
                    max(0, int((cooldown - now).total_seconds())) if cooldown else 0
                ),
                "pending_submissions": pending_submissions,
            },
        }
        self.stdout.write(json.dumps(result, sort_keys=True))
        if max_age is not None and age > max_age:
            raise CommandError("Pending delivery age exceeds the configured limit.")

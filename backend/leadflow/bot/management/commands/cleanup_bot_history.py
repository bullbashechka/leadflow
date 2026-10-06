from django.core.management.base import BaseCommand

from leadflow.bot.maintenance import cleanup_history_batch
from leadflow.bot.models import BotPollingState


class Command(BaseCommand):
    help = "Remove terminal bot transport history older than seven days in bounded batches."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        total = 0
        for bot_id in BotPollingState.objects.values_list("pk", flat=True).iterator():
            while True:
                count = cleanup_history_batch(bot_id, dry_run=options["dry_run"])
                total += count
                if options["dry_run"] or count < 1000:
                    break
        self.stdout.write(f"Eligible transport events: {total}")

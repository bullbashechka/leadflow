from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from leadflow.crm.models import LoginAttempt


class Command(BaseCommand):
    help = "Delete expired sessions and login counters unused for more than one day."

    def handle(self, *args, **options):
        call_command("clearsessions")
        LoginAttempt.objects.filter(started_at__lt=timezone.now() - timedelta(days=1)).delete()
        self.stdout.write("Expired CRM auth records removed.")

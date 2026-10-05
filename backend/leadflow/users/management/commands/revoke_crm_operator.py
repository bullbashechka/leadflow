from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from leadflow.crm.api.access import DEMO_USERNAME


class Command(BaseCommand):
    help = "Revoke an individual CRM account while preserving its records and authorship."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        username = get_user_model().normalize_username(options["username"])
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError("The CRM account does not exist.")
        if user.is_staff or user.is_superuser or user.username == DEMO_USERNAME:
            raise CommandError("This command only revokes individual CRM accounts.")
        user.is_active = False
        user.save(update_fields=["is_active"])
        self.stdout.write(self.style.SUCCESS("Individual CRM access revoked."))

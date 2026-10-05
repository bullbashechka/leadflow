from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.db import transaction

from leadflow.crm.api.access import DEMO_USERNAME


class Command(BaseCommand):
    help = "Create an individual CRM account using a password prompt without granting admin access."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        username = options["username"]
        if username == DEMO_USERNAME:
            raise CommandError("The internal demo principal cannot be an individual CRM account.")
        user = get_user_model()(
            username=username, is_active=True, is_staff=False, is_superuser=False
        )
        try:
            user.full_clean(exclude=["password"])
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        try:
            password = getpass("Password: ")
            repeated = getpass("Password again: ")
        except EOFError, KeyboardInterrupt:
            raise CommandError("Account creation cancelled; no changes saved.") from None
        if password != repeated:
            raise CommandError("Passwords do not match; no changes saved.")
        if len(password) > 1024:
            raise CommandError("The password must contain at most 1024 characters.")
        try:
            validate_password(password, user)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        user.set_password(password)
        try:
            with transaction.atomic():
                user.save(force_insert=True)
        except IntegrityError:
            raise CommandError("The account could not be created; no changes saved.") from None
        self.stdout.write(self.style.SUCCESS("Individual CRM account created."))

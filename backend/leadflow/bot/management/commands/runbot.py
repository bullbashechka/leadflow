import asyncio

from aiogram.exceptions import TelegramNetworkError
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError
from aiogram.utils.token import validate_token
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.db import connections

from leadflow.bot.runtime import run_polling
from leadflow.database import check_database


class Command(BaseCommand):
    help = "Run the Telegram bot as a separate long-polling process."

    def add_arguments(self, parser):
        parser.add_argument("--check-db", action="store_true")

    def handle(self, *args, **options):
        if not options["check_db"]:
            if not settings.BOT_TOKEN:
                raise CommandError("Set BOT_TOKEN in the local .env file to start the bot.")
            try:
                validate_token(settings.BOT_TOKEN)
            except TokenValidationError:
                raise CommandError(
                    "BOT_TOKEN has an invalid format. Check the local .env file."
                ) from None
        try:
            check_database()
        except DatabaseError:
            raise CommandError(
                "Database unavailable. Start PostgreSQL and apply migrations."
            ) from None
        finally:
            connections.close_all()
        self.stdout.write("Database connection OK")
        if options["check_db"]:
            return
        self.stdout.write("Starting Telegram long polling; one process per token.")
        try:
            asyncio.run(run_polling(settings.BOT_TOKEN))
        except TelegramUnauthorizedError:
            raise CommandError("Telegram rejected BOT_TOKEN. Check the local .env file.") from None
        except TelegramNetworkError:
            raise CommandError("Telegram is unreachable. Check the connection and retry.") from None

import asyncio
from io import StringIO
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from django.core.management import call_command
from django.core.management.base import CommandError

from leadflow.bot.handlers import start
from leadflow.bot.runtime import run_polling


def test_bot_requires_token(settings):
    settings.BOT_TOKEN = ""
    with pytest.raises(CommandError, match="BOT_TOKEN"):
        call_command("runbot")


def test_bot_rejects_invalid_token_without_exposing_it(settings):
    settings.BOT_TOKEN = "private-invalid-token"
    with pytest.raises(CommandError) as error:
        call_command("runbot")
    assert settings.BOT_TOKEN not in str(error.value)


def test_start_does_not_claim_to_accept_applications():
    message = AsyncMock()
    asyncio.run(start(message))
    message.answer.assert_awaited_once()
    assert "ещё не доступен" in message.answer.await_args.args[0]


@pytest.mark.django_db(transaction=True)
def test_bot_database_check_uses_the_same_database_without_token(settings):
    settings.BOT_TOKEN = ""
    output = StringIO()
    call_command("runbot", check_db=True, stdout=output)
    assert "Database connection OK" in output.getvalue()


def test_polling_closes_session_after_failure():
    with (
        patch("leadflow.bot.runtime.Bot") as bot_class,
        patch(
            "leadflow.bot.runtime.Dispatcher",
        ) as dispatcher_class,
    ):
        bot = bot_class.return_value
        bot.session.close = AsyncMock()
        bot.get_me = AsyncMock(side_effect=TelegramUnauthorizedError(method=None, message="denied"))
        dispatcher = dispatcher_class.return_value
        dispatcher.start_polling = AsyncMock()
        with pytest.raises(TelegramUnauthorizedError):
            asyncio.run(run_polling("123456:fake-token-for-test-only"))
        bot.session.close.assert_awaited_once()
        dispatcher.start_polling.assert_not_awaited()


def test_polling_handles_shutdown_and_closes_session():
    with (
        patch("leadflow.bot.runtime.Bot") as bot_class,
        patch(
            "leadflow.bot.runtime.Dispatcher",
        ) as dispatcher_class,
    ):
        bot = bot_class.return_value
        bot.session.close = AsyncMock()
        bot.get_me = AsyncMock()
        dispatcher = dispatcher_class.return_value
        dispatcher.start_polling = AsyncMock()
        asyncio.run(run_polling("123456:fake-token-for-test-only"))
        dispatcher.start_polling.assert_awaited_once()
        assert dispatcher.start_polling.await_args.kwargs["handle_signals"] is True
        bot.session.close.assert_awaited_once()

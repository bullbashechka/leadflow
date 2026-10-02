import asyncio
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import ReplyKeyboardMarkup
from django.core.management import call_command
from django.core.management.base import CommandError

from leadflow.bot.runtime import _decode_markup
from leadflow.bot.runtime import run_polling


@pytest.mark.parametrize(
    ("payload", "markup_type"),
    [
        (
            {
                "keyboard": [[{"text": "Отправить мой номер", "request_contact": True}]],
                "force_reply": True,
            },
            ReplyKeyboardMarkup,
        ),
        (
            {
                "inline_keyboard": [[{"text": "Назад", "callback_data": "back"}]],
                "force_reply": True,
            },
            InlineKeyboardMarkup,
        ),
    ],
)
def test_keyboard_with_force_reply_keeps_its_buttons_during_delivery(payload, markup_type):
    markup = _decode_markup(payload)

    assert isinstance(markup, markup_type)
    assert markup.model_dump(exclude_none=True) == payload


def test_bot_requires_token(settings):
    settings.BOT_TOKEN = ""
    with pytest.raises(CommandError, match="BOT_TOKEN"):
        call_command("runbot")


def test_bot_rejects_invalid_token_without_exposing_it(settings):
    settings.BOT_TOKEN = "private-invalid-token"
    with pytest.raises(CommandError) as error:
        call_command("runbot")
    assert settings.BOT_TOKEN not in str(error.value)


@pytest.mark.django_db(transaction=True)
def test_bot_database_check_uses_the_same_database_without_token(settings):
    settings.BOT_TOKEN = ""
    output = StringIO()
    call_command("runbot", check_db=True, stdout=output)
    assert "Database connection OK" in output.getvalue()


def test_polling_closes_session_when_telegram_rejects_token():
    with patch("leadflow.bot.runtime.Bot") as bot_class:
        bot = bot_class.return_value
        bot.session.close = AsyncMock()
        bot.get_me = AsyncMock(side_effect=TelegramUnauthorizedError(method=None, message="denied"))

        with pytest.raises(TelegramUnauthorizedError):
            asyncio.run(run_polling("123456:fake-token-for-test-only"))

        bot.session.close.assert_awaited_once()
        bot.get_updates.assert_not_called()


def test_polling_uses_the_durable_offset_and_closes_on_shutdown():
    with (
        patch("leadflow.bot.runtime.Bot") as bot_class,
        patch("leadflow.bot.runtime._database", new_callable=AsyncMock) as database,
        patch("leadflow.bot.runtime._complete_due_submissions", new_callable=AsyncMock) as complete,
        patch("leadflow.bot.runtime._deliver_pending_messages", new_callable=AsyncMock) as deliver,
    ):
        bot = bot_class.return_value
        bot.session.close = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(id=77))
        bot.get_updates = AsyncMock(side_effect=asyncio.CancelledError)
        database.return_value = 54

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_polling("123456:fake-token-for-test-only"))

        database.assert_awaited_once()
        assert database.await_args.args[0].__name__ == "get_polling_offset"
        assert database.await_args.args[1] == 77
        assert bot.get_updates.await_args.kwargs["offset"] == 54
        complete.assert_awaited_once_with(bot)
        deliver.assert_awaited_once_with(bot)
        bot.session.close.assert_awaited_once()

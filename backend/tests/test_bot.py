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


@pytest.mark.django_db(transaction=True)
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
        patch("leadflow.bot.runtime.PollingLease") as lease_class,
        patch("leadflow.bot.runtime.Bot") as bot_class,
        patch("leadflow.bot.runtime._database", new_callable=AsyncMock) as database,
        patch("leadflow.bot.runtime._complete_due_submissions", new_callable=AsyncMock) as complete,
        patch("leadflow.bot.runtime._deliver_pending_messages", new_callable=AsyncMock) as deliver,
    ):
        bot = bot_class.return_value
        bot.session.close = AsyncMock()
        bot.get_me = AsyncMock(return_value=SimpleNamespace(id=77))
        bot.set_my_commands = AsyncMock()
        bot.set_chat_menu_button = AsyncMock()
        bot.get_updates = AsyncMock(side_effect=asyncio.CancelledError)
        database.side_effect = lambda function, *args: (
            54 if function.__name__ == "get_polling_offset" else 0
        )

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_polling("123456:fake-token-for-test-only"))

        offset_calls = [
            call
            for call in database.await_args_list
            if call.args[0].__name__ == "get_polling_offset"
        ]
        assert len(offset_calls) == 1
        assert offset_calls[0].args[1] == 77
        bot.set_my_commands.assert_awaited_once()
        commands = bot.set_my_commands.await_args.args[0]
        assert [command.command for command in commands] == ["start", "back", "cancel", "help"]
        bot.set_chat_menu_button.assert_awaited_once()
        assert bot.get_updates.await_args.kwargs["offset"] == 54
        complete.assert_awaited_once()
        deliver.assert_awaited_once()
        assert complete.await_args.args[0].bot is bot
        assert deliver.await_args.args[0].bot is bot
        lease_class.return_value.acquire.assert_called_once()
        lease_class.return_value.close.assert_called_once()
        bot.session.close.assert_awaited_once()


@pytest.mark.django_db(transaction=True)
def test_startup_network_failure_retries_before_polling():
    from aiogram.exceptions import TelegramNetworkError

    bot = SimpleNamespace(
        session=SimpleNamespace(close=AsyncMock()),
        get_me=AsyncMock(
            side_effect=[
                TelegramNetworkError(method=None, message="unavailable"),
                SimpleNamespace(id=123456),
            ]
        ),
        set_my_commands=AsyncMock(),
        set_chat_menu_button=AsyncMock(),
        get_updates=AsyncMock(side_effect=asyncio.CancelledError),
    )
    with (
        patch("leadflow.bot.runtime.Bot", return_value=bot),
        patch("leadflow.bot.runtime.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_polling("123456:fake-token-for-test-only"))
    assert bot.get_me.await_count == 2
    sleep.assert_awaited_once_with(1)
    bot.get_updates.assert_awaited_once()
    bot.session.close.assert_awaited_once()


def test_sigterm_cancels_polling_and_waits_for_cleanup():
    from leadflow.bot.management.commands.runbot import _serve

    cleaned_up = []

    async def fake_polling(token):
        try:
            await asyncio.Future()
        finally:
            cleaned_up.append(True)

    async def exercise():
        loop = asyncio.get_running_loop()
        callbacks = []
        with (
            patch.object(
                loop, "add_signal_handler", side_effect=lambda sig, cb: callbacks.append(cb)
            ),
            patch.object(loop, "remove_signal_handler") as remove,
            patch("leadflow.bot.management.commands.runbot.run_polling", fake_polling),
        ):
            task = asyncio.create_task(_serve("unused"))
            await asyncio.sleep(0)
            callbacks[0]()
            await task
            remove.assert_called_once()

    asyncio.run(exercise())
    assert cleaned_up == [True]

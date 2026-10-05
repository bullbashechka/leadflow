"""Sequential polling with database-backed update offsets and message delivery."""

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.exceptions import TelegramNetworkError
from aiogram.exceptions import TelegramRetryAfter
from aiogram.exceptions import TelegramServerError
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand
from aiogram.types import BotCommandScopeAllPrivateChats
from aiogram.types import ForceReply
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import MenuButtonCommands
from aiogram.types import ReplyKeyboardMarkup
from aiogram.types import ReplyKeyboardRemove
from asgiref.sync import sync_to_async
from django.db import DatabaseError
from django.db import close_old_connections
from django.db import connections

from .handlers import complete_pending_submission
from .handlers import defer_pending_submission
from .handlers import get_pending_message
from .handlers import get_polling_offset
from .handlers import get_retryable_submissions
from .handlers import mark_message_delivered
from .handlers import mark_message_failed
from .handlers import process_update

logger = logging.getLogger(__name__)
_ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]


async def run_polling(token):
    bot = Bot(token)
    try:
        me = await bot.get_me()
        failures = 0
        commands_configured = False
        while True:
            try:
                if not commands_configured:
                    try:
                        await bot.set_my_commands(
                            [
                                BotCommand(command="start", description="Начать или продолжить"),
                                BotCommand(command="back", description="Назад"),
                                BotCommand(command="cancel", description="Отменить заявку"),
                                BotCommand(command="help", description="Помощь"),
                            ],
                            scope=BotCommandScopeAllPrivateChats(),
                        )
                    except (TelegramBadRequest, TelegramForbiddenError) as error:
                        logger.warning(
                            "Telegram command menu was rejected: %s", type(error).__name__
                        )
                    try:
                        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
                    except (TelegramBadRequest, TelegramForbiddenError) as error:
                        logger.warning(
                            "Telegram menu button was rejected: %s", type(error).__name__
                        )
                    commands_configured = True
                offset = await _database(get_polling_offset, me.id)
                await _complete_due_submissions(bot)
                await _deliver_pending_messages(bot)
                updates = await bot.get_updates(
                    offset=offset,
                    limit=100,
                    timeout=10,
                    allowed_updates=_ALLOWED_UPDATES,
                )
                for update in updates:
                    result = await _database(process_update, me.id, update)
                    if result.callback_query_id:
                        await _answer_callback(bot, result)
                    await _complete_due_submissions(bot)
                    await _deliver_pending_messages(bot)
                failures = 0
            except TelegramUnauthorizedError:
                raise
            except (TelegramNetworkError, TelegramServerError, DatabaseError) as error:
                failures = min(failures + 1, 6)
                delay = min(30, 2 ** (failures - 1))
                logger.warning("Telegram polling paused after %s", type(error).__name__)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures = min(failures + 1, 6)
                delay = min(30, 2 ** (failures - 1))
                logger.error("Telegram polling iteration failed: %s", type(error).__name__)
                await asyncio.sleep(delay)
    finally:
        await bot.session.close()


async def _database(function, *args, **kwargs):
    return await sync_to_async(_call_with_connection_cleanup, thread_sensitive=True)(
        function, *args, **kwargs
    )


def _call_with_connection_cleanup(function, *args, **kwargs):
    close_old_connections()
    try:
        return function(*args, **kwargs)
    finally:
        connections.close_all()


async def _answer_callback(bot, result):
    try:
        await bot.answer_callback_query(
            callback_query_id=result.callback_query_id,
            text=(result.callback_notice or "")[:200],
        )
    except TelegramBadRequest:
        # The user may have left Telegram's short callback-answer window.
        return
    except TelegramNetworkError:
        return


async def _complete_due_submissions(bot):
    submission_ids = await _database(get_retryable_submissions)
    for submission_id in submission_ids:
        try:
            await _database(complete_pending_submission, submission_id)
        except (DatabaseError, TelegramNetworkError) as error:
            try:
                await _database(defer_pending_submission, submission_id, type(error).__name__)
            except DatabaseError:
                logger.warning(
                    "Unable to record a pending submission retry after %s", type(error).__name__
                )
            return


async def _deliver_pending_messages(bot, limit=25):
    for _ in range(limit):
        message = await _database(get_pending_message)
        if not message:
            return
        try:
            if message["operation"] == "send":
                result = await bot.send_message(
                    chat_id=message["chat_id"],
                    text=message["text"],
                    reply_markup=_decode_markup(message["reply_markup"]),
                )
                telegram_message_id = result.message_id
                telegram_message_date = result.date
            elif message["operation"] == "edit_text":
                result = await bot.edit_message_text(
                    chat_id=message["chat_id"],
                    message_id=message["target_message_id"],
                    text=message["text"],
                    reply_markup=_decode_markup(message["reply_markup"]),
                )
                telegram_message_id = getattr(result, "message_id", message["target_message_id"])
                telegram_message_date = getattr(result, "date", None)
            elif message["operation"] == "edit_markup":
                result = await bot.edit_message_reply_markup(
                    chat_id=message["chat_id"],
                    message_id=message["target_message_id"],
                    reply_markup=_decode_markup(message["reply_markup"]),
                )
                telegram_message_id = message["target_message_id"]
                telegram_message_date = getattr(result, "date", None)
            else:
                raise ValueError("Unknown Telegram outbox operation")
        except TelegramRetryAfter as error:
            await _database(
                mark_message_failed,
                message["id"],
                type(error).__name__,
                retry_after=error.retry_after,
            )
            return
        except (TelegramNetworkError, TelegramServerError) as error:
            await _database(mark_message_failed, message["id"], type(error).__name__)
            continue
        except (TelegramBadRequest, TelegramForbiddenError) as error:
            await _database(
                mark_message_failed,
                message["id"],
                type(error).__name__,
                permanent=True,
            )
            logger.warning("A queued Telegram message was rejected: %s", type(error).__name__)
            continue
        await _database(
            mark_message_delivered,
            message["id"],
            telegram_message_id,
            telegram_message_date,
        )


def _decode_markup(data):
    if not data:
        return None
    if "inline_keyboard" in data:
        return InlineKeyboardMarkup.model_validate(data)
    if "keyboard" in data:
        return ReplyKeyboardMarkup.model_validate(data)
    if data.get("remove_keyboard"):
        return ReplyKeyboardRemove.model_validate(data)
    if "force_reply" in data:
        return ForceReply.model_validate(data)
    raise ValueError("Unknown queued Telegram reply markup")

from aiogram import Bot
from aiogram import Dispatcher

from .handlers import build_router


async def run_polling(token):
    bot = Bot(token=token)
    try:
        # Fail at startup for an invalid remote token, without logging the token.
        await bot.get_me()
        dispatcher = Dispatcher()
        dispatcher.include_router(build_router())
        await dispatcher.start_polling(
            bot,
            handle_signals=True,
            close_bot_session=False,
            handle_as_tasks=False,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()

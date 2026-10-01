from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message


async def start(message: Message):
    await message.answer(
        "Это бот Leadflow для заявок агентства. "
        "Сбор заявок ещё не доступен: сейчас проверяем основу проекта.",
    )


def build_router():
    router = Router()
    router.message.register(start, CommandStart())
    return router

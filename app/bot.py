from __future__ import annotations

from aiogram import Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message


def create_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    router = Router(name="core")

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        await message.answer(
            "Прогнозист готов к работе.\n\n"
            "Добавьте меня в групповой чат, а затем администратор создаст конкурс."
        )

    @router.message(Command("help"))
    async def handle_help(message: Message) -> None:
        await message.answer(
            "Здесь будут прогнозы на футбольные матчи, турнирная таблица "
            "и история ваших прогнозов."
        )

    dispatcher.include_router(router)
    return dispatcher

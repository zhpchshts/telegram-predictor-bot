from __future__ import annotations

from urllib.parse import quote

from aiogram import Dispatcher, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Settings
from app.tma_launch import create_tma_launch_token


def create_dispatcher(settings: Settings) -> Dispatcher:
    dispatcher = Dispatcher()
    router = Router(name="core")

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        await message.answer(
            "Прогнозист готов к работе.\n\n"
            "Добавьте меня в групповой чат и отправьте /app, "
            "чтобы открыть конкурс."
        )

    @router.message(Command("help"))
    async def handle_help(message: Message) -> None:
        await message.answer(
            "Команды:\n"
            "/app — открыть прогнозы этого чата.\n\n"
            "Позже здесь появятся создание конкурса, матчи, "
            "прогнозы и таблица."
        )

    @router.message(Command("app"))
    async def handle_app(message: Message) -> None:
        if message.chat.type not in {
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        }:
            await message.answer(
                "Открой Прогнозист из нужного группового чата: "
                "добавь туда бота и отправь /app."
            )
            return

        launch_token = create_tma_launch_token(
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            chat_title=message.chat.title,
            secret=settings.bot_token,
        )
        launch_url = (
            f"https://t.me/{settings.bot_username}"
            f"?startapp={quote(launch_token, safe='')}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Открыть прогнозы",
                        url=launch_url,
                    )
                ]
            ]
        )

        await message.answer(
            "Открой прогнозы этого чата по кнопке ниже.",
            reply_markup=keyboard,
        )

    dispatcher.include_router(router)
    return dispatcher

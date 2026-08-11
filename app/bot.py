from __future__ import annotations

from urllib.parse import quote

from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.chat_migration_service import migrate_telegram_chat
from app.chat_settings_service import get_chat_settings
from app.config import Settings
from app.tma_launch import create_tma_launch_token


def create_dispatcher(settings: Settings) -> Dispatcher:
    dispatcher = Dispatcher()
    router = Router(name="core")

    @router.message(F.migrate_to_chat_id)
    async def handle_migrate_to_chat(message: Message) -> None:
        migrate_telegram_chat(
            database_path=settings.database_path,
            old_telegram_chat_id=message.chat.id,
            new_telegram_chat_id=message.migrate_to_chat_id,
            new_chat_title=message.chat.title,
        )

    @router.message(F.migrate_from_chat_id)
    async def handle_migrate_from_chat(message: Message) -> None:
        migrate_telegram_chat(
            database_path=settings.database_path,
            old_telegram_chat_id=message.migrate_from_chat_id,
            new_telegram_chat_id=message.chat.id,
            new_chat_title=message.chat.title,
        )

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        await message.answer(
            "Клевер — конкурсы прогнозов на спорт и киберспорт "
            "для Telegram-чатов.\n\n"
            "Добавьте меня в групповой чат и отправьте /app, "
            "чтобы открыть приложение."
        )

    @router.message(Command("help"))
    async def handle_help(message: Message) -> None:
        await message.answer(
            "Клевер открывается из нужного группового чата через /app.\n\n"
            "Добавьте меня в чат и отправьте /app — бот пришлёт "
            "кнопку для открытия приложения."
        )

    @router.message(Command("app"))
    async def handle_app(message: Message) -> None:
        if message.chat.type not in {
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        }:
            await message.answer(
                "Открой Клевер из нужного группового чата: "
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
                        text=get_chat_settings(
                            database_path=settings.database_path,
                            telegram_chat_id=message.chat.id,
                        ).app_button_text,
                        url=launch_url,
                    )
                ]
            ]
        )

        await message.answer(
            "Открой Клевер для этого чата по кнопке ниже.",
            reply_markup=keyboard,
        )

    dispatcher.include_router(router)
    return dispatcher

from __future__ import annotations

from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.chat_migration_service import migrate_telegram_chat
from app.config import Settings
from app.tma_entrypoint import create_tma_launch_keyboard


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

        keyboard = create_tma_launch_keyboard(
            database_path=settings.database_path,
            telegram_chat_id=message.chat.id,
            chat_type=message.chat.type,
            chat_title=message.chat.title,
            bot_username=settings.bot_username,
            bot_token=settings.bot_token,
        )

        await message.answer(
            "Открой Клевер для этого чата по кнопке ниже.",
            reply_markup=keyboard,
        )

    dispatcher.include_router(router)
    return dispatcher

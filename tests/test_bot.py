from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatType

from app.bot import create_dispatcher
from app.config import Settings
from app.database import create_connection, initialize_database


def _settings(database_path: Path) -> Settings:
    return Settings(
        bot_token="123456789:test-token",
        bot_username="ZhpchshtsPredictorBot",
        database_path=database_path,
        telegram_admin_check_timeout_seconds=3.0,
        telegram_bot_api_fallback_ips=(),
        telegram_api_id=None,
        telegram_api_hash=None,
        telegram_mtproto_session_path=database_path.parent / "telegram-mtproto",
        healthcheck_chat_id=None,
        shared_tournament_admin_ids=frozenset(),
    )


def test_app_command_uses_chat_specific_button_text(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    with create_connection(database_path) as connection:
        chat_id = int(
            connection.execute(
                "INSERT INTO chats (telegram_chat_id, title) VALUES (?, ?)",
                (-1001234567890, "Test chat"),
            ).lastrowid
        )
        connection.execute(
            "INSERT INTO chat_settings (chat_id, app_button_text) VALUES (?, ?)",
            (chat_id, "Сделать прогноз"),
        )

    dispatcher = create_dispatcher(_settings(database_path))
    router = dispatcher.sub_routers[0]
    handler = next(
        item.callback
        for item in router.observers["message"].handlers
        if item.callback.__name__ == "handle_app"
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(
            id=-1001234567890,
            type=ChatType.SUPERGROUP,
            title="Test chat",
        ),
        answer=AsyncMock(),
    )

    asyncio.run(handler(message))

    reply_markup = message.answer.await_args.kwargs["reply_markup"]
    assert reply_markup.inline_keyboard[0][0].text == "Сделать прогноз"


def test_app_command_uses_default_button_text_for_new_chat(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    dispatcher = create_dispatcher(_settings(database_path))
    router = dispatcher.sub_routers[0]
    handler = next(
        item.callback
        for item in router.observers["message"].handlers
        if item.callback.__name__ == "handle_app"
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(
            id=-1001234567890,
            type=ChatType.SUPERGROUP,
            title="Test chat",
        ),
        answer=AsyncMock(),
    )

    asyncio.run(handler(message))

    reply_markup = message.answer.await_args.kwargs["reply_markup"]
    assert reply_markup.inline_keyboard[0][0].text == "Открыть Клевер"

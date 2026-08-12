from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.chat_settings_service import get_chat_settings
from app.tma_launch import create_tma_launch_token


def create_tma_launch_keyboard(
    *,
    database_path: Path,
    telegram_chat_id: int,
    chat_type: str,
    chat_title: str | None,
    bot_username: str,
    bot_token: str,
) -> InlineKeyboardMarkup:
    launch_token = create_tma_launch_token(
        chat_id=telegram_chat_id,
        chat_type=chat_type,
        chat_title=chat_title,
        secret=bot_token,
    )
    launch_url = f"https://t.me/{bot_username}?startapp={quote(launch_token, safe='')}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=get_chat_settings(
                        database_path=database_path,
                        telegram_chat_id=telegram_chat_id,
                    ).app_button_text,
                    url=launch_url,
                )
            ]
        ]
    )

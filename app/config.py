from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "predictor.db"

load_dotenv(PROJECT_ROOT / ".env")


def _parse_admin_user_ids(raw_value: str | None) -> frozenset[int]:
    if not raw_value:
        return frozenset()

    try:
        return frozenset(
            int(user_id.strip()) for user_id in raw_value.split(",") if user_id.strip()
        )
    except ValueError as error:
        raise ValueError(
            "ADMIN_USER_IDS must contain comma-separated integer Telegram user IDs."
        ) from error


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    bot_username: str
    database_path: Path
    public_base_url: str | None
    admin_user_ids: frozenset[int]


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required.")
    bot_username = os.getenv("BOT_USERNAME", "").strip().removeprefix("@")
    if not bot_username:
        raise RuntimeError("BOT_USERNAME is required.")
    database_path_value = os.getenv("DATABASE_PATH", "").strip()
    database_path = (
        Path(database_path_value).expanduser()
        if database_path_value
        else DEFAULT_DATABASE_PATH
    )

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/") or None

    return Settings(
        bot_token=bot_token,
        bot_username=bot_username,
        database_path=database_path,
        public_base_url=public_base_url,
        admin_user_ids=_parse_admin_user_ids(os.getenv("ADMIN_USER_IDS")),
    )

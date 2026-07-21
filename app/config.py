from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "predictor.db"
DEFAULT_TELEGRAM_MTPROTO_SESSION_PATH = PROJECT_ROOT / "data" / "telegram-mtproto"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    bot_username: str
    database_path: Path
    public_base_url: str | None
    role_enforcement_enabled: bool
    telegram_api_id: int | None
    telegram_api_hash: str | None
    telegram_mtproto_session_path: Path


def _parse_boolean_environment(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    normalized_value = value.strip().lower()
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be one of: true, false, 1, 0, yes, no, on, off.")


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
    role_enforcement_enabled = _parse_boolean_environment(
        "ROLE_ENFORCEMENT_ENABLED",
        default=False,
    )
    telegram_api_id = _parse_optional_positive_integer_environment("TELEGRAM_API_ID")
    telegram_api_hash = os.getenv("TELEGRAM_API_HASH", "").strip() or None
    telegram_mtproto_session_path_value = os.getenv(
        "TELEGRAM_MTPROTO_SESSION_PATH", ""
    ).strip()
    telegram_mtproto_session_path = (
        Path(telegram_mtproto_session_path_value).expanduser()
        if telegram_mtproto_session_path_value
        else DEFAULT_TELEGRAM_MTPROTO_SESSION_PATH
    )

    return Settings(
        bot_token=bot_token,
        bot_username=bot_username,
        database_path=database_path,
        public_base_url=public_base_url,
        role_enforcement_enabled=role_enforcement_enabled,
        telegram_api_id=telegram_api_id,
        telegram_api_hash=telegram_api_hash,
        telegram_mtproto_session_path=telegram_mtproto_session_path,
    )


def _parse_optional_positive_integer_environment(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        parsed_value = int(value)
    except ValueError:
        logger.warning("%s is invalid; Telegram MTProto is disabled.", name)
        return None
    if parsed_value <= 0:
        logger.warning("%s is invalid; Telegram MTProto is disabled.", name)
        return None
    return parsed_value

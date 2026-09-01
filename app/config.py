from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from ipaddress import ip_address
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
    telegram_admin_check_timeout_seconds: float
    telegram_bot_api_fallback_ips: tuple[str, ...]
    telegram_api_id: int | None
    telegram_api_hash: str | None
    telegram_mtproto_session_path: Path
    healthcheck_chat_id: int | None
    shared_tournament_admin_ids: frozenset[int]
    football_data_api_token: str | None = None
    champions_league_sync_season: int = 2026
    champions_league_sync_interval_minutes: int = 10


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

    telegram_admin_check_timeout_seconds = _parse_positive_float_environment(
        "TELEGRAM_ADMIN_CHECK_TIMEOUT_SECONDS",
        default=3.0,
    )
    telegram_bot_api_fallback_ips = _parse_ip_address_list_environment(
        "TELEGRAM_BOT_API_FALLBACK_IPS"
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
    healthcheck_chat_id = _parse_optional_nonzero_integer_environment(
        "HEALTHCHECK_CHAT_ID"
    )
    shared_tournament_admin_ids = _parse_positive_integer_list_environment(
        "SHARED_TOURNAMENT_ADMIN_IDS"
    )
    football_data_api_token = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip() or None
    champions_league_sync_season = _parse_integer_environment(
        "CHAMPIONS_LEAGUE_SYNC_SEASON",
        default=2026,
        minimum=2026,
        maximum=2026,
    )
    champions_league_sync_interval_minutes = _parse_integer_environment(
        "CHAMPIONS_LEAGUE_SYNC_INTERVAL_MINUTES",
        default=10,
        minimum=1,
        maximum=24 * 60,
    )

    return Settings(
        bot_token=bot_token,
        bot_username=bot_username,
        database_path=database_path,
        telegram_admin_check_timeout_seconds=(telegram_admin_check_timeout_seconds),
        telegram_bot_api_fallback_ips=telegram_bot_api_fallback_ips,
        telegram_api_id=telegram_api_id,
        telegram_api_hash=telegram_api_hash,
        telegram_mtproto_session_path=telegram_mtproto_session_path,
        healthcheck_chat_id=healthcheck_chat_id,
        shared_tournament_admin_ids=shared_tournament_admin_ids,
        football_data_api_token=football_data_api_token,
        champions_league_sync_season=champions_league_sync_season,
        champions_league_sync_interval_minutes=(champions_league_sync_interval_minutes),
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


def _parse_optional_nonzero_integer_environment(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        parsed_value = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a non-zero integer.") from error
    if parsed_value == 0:
        raise RuntimeError(f"{name} must be a non-zero integer.")
    return parsed_value


def _parse_positive_float_environment(name: str, *, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed_value = float(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive number.") from error
    if parsed_value <= 0:
        raise RuntimeError(f"{name} must be a positive number.")
    return parsed_value


def _parse_ip_address_list_environment(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "").strip()
    if not value:
        return ()

    parsed_values: list[str] = []
    seen_values: set[str] = set()
    for item in value.split(","):
        normalized_item = item.strip()
        try:
            parsed_item = ip_address(normalized_item)
        except ValueError as error:
            raise RuntimeError(
                f"{name} must be a comma-separated list of public IP addresses."
            ) from error
        if not parsed_item.is_global:
            raise RuntimeError(
                f"{name} must be a comma-separated list of public IP addresses."
            )
        normalized_address = str(parsed_item)
        if normalized_address not in seen_values:
            parsed_values.append(normalized_address)
            seen_values.add(normalized_address)
    return tuple(parsed_values)


def _parse_positive_integer_list_environment(name: str) -> frozenset[int]:
    value = os.getenv(name, "").strip()
    if not value:
        return frozenset()

    parsed_values: set[int] = set()
    for item in value.split(","):
        normalized_item = item.strip()
        if not normalized_item:
            raise RuntimeError(
                f"{name} must be a comma-separated list of positive integers."
            )
        try:
            parsed_item = int(normalized_item)
        except ValueError as error:
            raise RuntimeError(
                f"{name} must be a comma-separated list of positive integers."
            ) from error
        if parsed_item <= 0:
            raise RuntimeError(
                f"{name} must be a comma-separated list of positive integers."
            )
        parsed_values.add(parsed_item)
    return frozenset(parsed_values)


def _parse_integer_environment(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed_value = int(value)
    except ValueError as error:
        raise RuntimeError(
            f"{name} must be an integer from {minimum} to {maximum}."
        ) from error
    if parsed_value < minimum or parsed_value > maximum:
        raise RuntimeError(f"{name} must be an integer from {minimum} to {maximum}.")
    return parsed_value

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.tma_auth import TelegramInitDataError, validate_telegram_init_data
from app.tma_launch import TmaLaunchTokenError, validate_tma_launch_token


class TmaContextError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TmaUserContext:
    telegram_user_id: int
    first_name: str
    last_name: str | None
    username: str | None


@dataclass(frozen=True, slots=True)
class TmaChatContext:
    telegram_chat_id: int
    chat_type: str
    title: str | None


@dataclass(frozen=True, slots=True)
class TmaContext:
    user: TmaUserContext
    chat: TmaChatContext


def build_tma_context(*, init_data: str, bot_token: str) -> TmaContext:
    try:
        telegram_init_data = validate_telegram_init_data(
            init_data,
            bot_token=bot_token,
        )
    except TelegramInitDataError as error:
        raise TmaContextError(str(error)) from error

    start_param = telegram_init_data.start_param

    if not start_param:
        raise TmaContextError("Telegram init data start_param is required.")

    try:
        launch_context = validate_tma_launch_token(
            start_param,
            secret=bot_token,
        )
    except TmaLaunchTokenError as error:
        raise TmaContextError(str(error)) from error

    return TmaContext(
        user=_parse_user(telegram_init_data.user),
        chat=TmaChatContext(
            telegram_chat_id=launch_context.chat_id,
            chat_type=launch_context.chat_type,
            title=launch_context.chat_title,
        ),
    )


def _parse_user(payload: dict[str, Any] | None) -> TmaUserContext:
    if payload is None:
        raise TmaContextError("Telegram init data user is required.")

    telegram_user_id = _parse_required_integer(
        payload,
        field_name="user.id",
    )
    first_name = _parse_required_string(
        payload,
        field_name="user.first_name",
    )

    return TmaUserContext(
        telegram_user_id=telegram_user_id,
        first_name=first_name,
        last_name=_parse_optional_string(payload, field_name="user.last_name"),
        username=_parse_optional_string(payload, field_name="user.username"),
    )


def _parse_required_integer(
    payload: dict[str, Any],
    *,
    field_name: str,
) -> int:
    value = payload.get(field_name.rsplit(".", maxsplit=1)[1])

    if isinstance(value, bool) or not isinstance(value, int):
        raise TmaContextError(f"Telegram init data {field_name} is invalid.")

    return value


def _parse_required_string(
    payload: dict[str, Any],
    *,
    field_name: str,
) -> str:
    value = _parse_optional_string(
        payload,
        field_name=field_name,
    )

    if not value:
        raise TmaContextError(f"Telegram init data {field_name} is required.")

    return value


def _parse_optional_string(
    payload: dict[str, Any],
    *,
    field_name: str,
) -> str | None:
    value = payload.get(field_name.rsplit(".", maxsplit=1)[1])

    if value is None:
        return None

    if not isinstance(value, str):
        raise TmaContextError(f"Telegram init data {field_name} is invalid.")

    return value

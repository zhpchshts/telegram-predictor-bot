from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl


DEFAULT_INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60


class TelegramInitDataError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramInitData:
    fields: dict[str, str]
    auth_date: int
    user: dict[str, Any] | None = None
    chat: dict[str, Any] | None = None
    chat_type: str | None = None
    start_param: str | None = None


def parse_init_data(init_data: str) -> dict[str, str]:
    if not init_data.strip():
        raise TelegramInitDataError("init_data is required.")

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as error:
        raise TelegramInitDataError("init_data is malformed.") from error

    fields: dict[str, str] = {}

    for key, value in pairs:
        if key in fields:
            raise TelegramInitDataError(f"Duplicate init_data field: {key}.")

        fields[key] = value

    return fields


def build_data_check_string(fields: dict[str, str]) -> str:
    return "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key != "hash"
    )


def calculate_init_data_hash(
    fields: dict[str, str],
    *,
    bot_token: str,
) -> str:
    data_check_string = build_data_check_string(fields)

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).digest()

    return hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def parse_auth_date(fields: dict[str, str]) -> int:
    auth_date = fields.get("auth_date")

    if auth_date is None:
        raise TelegramInitDataError("auth_date is required.")

    try:
        return int(auth_date)
    except ValueError as error:
        raise TelegramInitDataError("auth_date must be an integer.") from error


def is_auth_date_expired(
    *,
    auth_date: int,
    now: int,
    max_age_seconds: int,
) -> bool:
    return auth_date + max_age_seconds < now


def parse_optional_json_object(
    fields: dict[str, str],
    field_name: str,
) -> dict[str, Any] | None:
    raw_value = fields.get(field_name)

    if raw_value is None:
        return None

    try:
        parsed_value = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise TelegramInitDataError(
            f"{field_name} must be a valid JSON object."
        ) from error

    if not isinstance(parsed_value, dict):
        raise TelegramInitDataError(f"{field_name} must be a JSON object.")

    return parsed_value


def validate_telegram_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int = DEFAULT_INIT_DATA_MAX_AGE_SECONDS,
    now: int | None = None,
) -> TelegramInitData:
    if not bot_token:
        raise TelegramInitDataError("bot_token is required.")

    fields = parse_init_data(init_data)

    received_hash = fields.get("hash")

    if received_hash is None:
        raise TelegramInitDataError("hash is required.")

    expected_hash = calculate_init_data_hash(
        fields,
        bot_token=bot_token,
    )

    if not hmac.compare_digest(expected_hash, received_hash):
        raise TelegramInitDataError("init_data hash is invalid.")

    auth_date = parse_auth_date(fields)
    current_time = int(time.time()) if now is None else now

    if max_age_seconds > 0 and is_auth_date_expired(
        auth_date=auth_date,
        now=current_time,
        max_age_seconds=max_age_seconds,
    ):
        raise TelegramInitDataError("init_data is expired.")

    return TelegramInitData(
        fields=fields,
        auth_date=auth_date,
        user=parse_optional_json_object(fields, "user"),
        chat=parse_optional_json_object(fields, "chat"),
        chat_type=fields.get("chat_type"),
        start_param=fields.get("start_param"),
    )

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from app.tma_auth import (
    TelegramInitDataError,
    build_data_check_string,
    calculate_init_data_hash,
    parse_init_data,
    validate_telegram_init_data,
)


BOT_TOKEN = "123456789:test-token"


def build_signed_init_data(fields: dict[str, str]) -> str:
    signed_fields = fields.copy()
    signed_fields["hash"] = calculate_init_data_hash(
        signed_fields,
        bot_token=BOT_TOKEN,
    )
    return urlencode(signed_fields)


def test_parse_init_data_rejects_empty_value() -> None:
    with pytest.raises(TelegramInitDataError, match="init_data is required."):
        parse_init_data("")


def test_parse_init_data_rejects_duplicate_fields() -> None:
    with pytest.raises(
        TelegramInitDataError,
        match="Duplicate init_data field: auth_date.",
    ):
        parse_init_data("auth_date=100&auth_date=200&hash=abc")


def test_build_data_check_string_sorts_fields_and_excludes_hash() -> None:
    fields = {
        "user": '{"id":123}',
        "hash": "should-be-excluded",
        "auth_date": "1700000000",
        "query_id": "AAEAAAE",
        "signature": "test-signature",
    }

    assert build_data_check_string(fields) == (
        "auth_date=1700000000\n"
        "query_id=AAEAAAE\n"
        "signature=test-signature\n"
        'user={"id":123}'
    )


def test_validate_telegram_init_data_returns_parsed_payload() -> None:
    auth_date = 1_700_000_000
    init_data = build_signed_init_data(
        {
            "auth_date": str(auth_date),
            "query_id": "AAEAAAE",
            "user": '{"id":123,"first_name":"Eugene"}',
            "chat": '{"id":-100,"type":"supergroup","title":"Home"}',
            "chat_type": "supergroup",
            "start_param": "chat_-100",
            "signature": "test-signature",
        }
    )

    validated_data = validate_telegram_init_data(
        init_data,
        bot_token=BOT_TOKEN,
        now=auth_date + 60,
    )

    assert validated_data.auth_date == auth_date
    assert validated_data.user == {
        "id": 123,
        "first_name": "Eugene",
    }
    assert validated_data.chat == {
        "id": -100,
        "type": "supergroup",
        "title": "Home",
    }
    assert validated_data.chat_type == "supergroup"
    assert validated_data.start_param == "chat_-100"


def test_validate_telegram_init_data_rejects_missing_hash() -> None:
    init_data = urlencode(
        {
            "auth_date": "1700000000",
            "user": '{"id":123}',
        }
    )

    with pytest.raises(TelegramInitDataError, match="hash is required."):
        validate_telegram_init_data(
            init_data,
            bot_token=BOT_TOKEN,
        )


def test_validate_telegram_init_data_rejects_invalid_hash() -> None:
    init_data = urlencode(
        {
            "auth_date": "1700000000",
            "user": '{"id":123}',
            "hash": "invalid",
        }
    )

    with pytest.raises(
        TelegramInitDataError,
        match="init_data hash is invalid.",
    ):
        validate_telegram_init_data(
            init_data,
            bot_token=BOT_TOKEN,
        )


def test_validate_telegram_init_data_rejects_expired_auth_date() -> None:
    auth_date = 1_700_000_000
    init_data = build_signed_init_data(
        {
            "auth_date": str(auth_date),
            "user": '{"id":123}',
        }
    )

    with pytest.raises(TelegramInitDataError, match="init_data is expired."):
        validate_telegram_init_data(
            init_data,
            bot_token=BOT_TOKEN,
            max_age_seconds=60,
            now=auth_date + 61,
        )


def test_validate_telegram_init_data_rejects_invalid_user_json() -> None:
    auth_date = 1_700_000_000
    init_data = build_signed_init_data(
        {
            "auth_date": str(auth_date),
            "user": "not-json",
        }
    )

    with pytest.raises(
        TelegramInitDataError,
        match="user must be a valid JSON object.",
    ):
        validate_telegram_init_data(
            init_data,
            bot_token=BOT_TOKEN,
            now=auth_date + 60,
        )

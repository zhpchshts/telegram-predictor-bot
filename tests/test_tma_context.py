from __future__ import annotations

from urllib.parse import urlencode

import pytest

import app.tma_auth as tma_auth
import app.tma_launch as tma_launch
from app.chat_migration_service import migrate_telegram_chat
from app.database import create_connection, initialize_database
from app.tma_auth import calculate_init_data_hash
from app.tma_context import TmaContextError, build_tma_context
from app.tma_launch import create_tma_launch_token


BOT_TOKEN = "123456789:test-token"
NOW = 1_800_000_000


def build_signed_init_data(fields: dict[str, str]) -> str:
    signed_fields = fields.copy()
    signed_fields["hash"] = calculate_init_data_hash(
        signed_fields,
        bot_token=BOT_TOKEN,
    )
    return urlencode(signed_fields)


def freeze_time(monkeypatch: pytest.MonkeyPatch, now: int) -> None:
    monkeypatch.setattr(tma_auth.time, "time", lambda: now)
    monkeypatch.setattr(tma_launch.time, "time", lambda: now)


def test_build_tma_context_uses_chat_from_launch_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_time(monkeypatch, NOW)

    launch_token = create_tma_launch_token(
        chat_id=-1001234567890,
        chat_type="supergroup",
        chat_title="Футбольные прогнозы",
        secret=BOT_TOKEN,
        now=NOW,
    )
    init_data = build_signed_init_data(
        {
            "auth_date": str(NOW),
            "query_id": "AAEAAAE",
            "user": '{"id":123,"first_name":"Eugene","username":"evsab"}',
            "chat_type": "supergroup",
            "start_param": launch_token,
        }
    )

    context = build_tma_context(
        init_data=init_data,
        bot_token=BOT_TOKEN,
    )

    assert context.user.telegram_user_id == 123
    assert context.user.first_name == "Eugene"
    assert context.user.username == "evsab"
    assert context.chat.telegram_chat_id == -1001234567890
    assert context.chat.chat_type == "supergroup"
    assert context.chat.title == "Футбольные прогнозы"


def test_build_tma_context_rejects_missing_start_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_time(monkeypatch, NOW)

    init_data = build_signed_init_data(
        {
            "auth_date": str(NOW),
            "user": '{"id":123,"first_name":"Eugene"}',
        }
    )

    with pytest.raises(
        TmaContextError,
        match="start_param is required",
    ):
        build_tma_context(
            init_data=init_data,
            bot_token=BOT_TOKEN,
        )


def test_build_tma_context_rejects_expired_launch_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_token = create_tma_launch_token(
        chat_id=-1001234567890,
        chat_type="supergroup",
        secret=BOT_TOKEN,
        now=NOW,
        max_age_seconds=60,
    )
    freeze_time(monkeypatch, NOW + 61)

    init_data = build_signed_init_data(
        {
            "auth_date": str(NOW + 60),
            "user": '{"id":123,"first_name":"Eugene"}',
            "start_param": launch_token,
        }
    )

    with pytest.raises(
        TmaContextError,
        match="TMA launch token is expired",
    ):
        build_tma_context(
            init_data=init_data,
            bot_token=BOT_TOKEN,
        )


def test_build_tma_context_rejects_migrated_chat_launch_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    old_chat_id = -5456940219
    with create_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO chats (telegram_chat_id, title)
            VALUES (?, ?)
            """,
            (old_chat_id, "Old group"),
        )
    migrate_telegram_chat(
        database_path=database_path,
        old_telegram_chat_id=old_chat_id,
        new_telegram_chat_id=-1003944707033,
        new_chat_title="New supergroup",
    )
    freeze_time(monkeypatch, NOW)
    launch_token = create_tma_launch_token(
        chat_id=old_chat_id,
        chat_type="group",
        secret=BOT_TOKEN,
        now=NOW,
    )
    init_data = build_signed_init_data(
        {
            "auth_date": str(NOW),
            "user": '{"id":123,"first_name":"Eugene"}',
            "start_param": launch_token,
        }
    )

    with pytest.raises(
        TmaContextError,
        match="TMA launch link is no longer active",
    ):
        build_tma_context(
            init_data=init_data,
            bot_token=BOT_TOKEN,
            database_path=database_path,
        )

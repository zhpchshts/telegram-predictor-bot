from __future__ import annotations

import time
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from app.main import create_app
from app.tma_auth import calculate_init_data_hash
from app.tma_launch import create_tma_launch_token


BOT_TOKEN = "123456789:test-token"


def build_signed_init_data(fields: dict[str, str]) -> str:
    signed_fields = fields.copy()
    signed_fields["hash"] = calculate_init_data_hash(
        signed_fields,
        bot_token=BOT_TOKEN,
    )
    return urlencode(signed_fields)


def test_bootstrap_rejects_missing_init_data(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv(
        "BOT_USERNAME",
        "ZhpchshtsPredictorBot",
    )

    client = TestClient(create_app())

    response = client.get("/api/tma/bootstrap")

    assert response.status_code == 401
    assert response.json() == {"detail": "Telegram init data is required."}


def test_bootstrap_returns_verified_tma_context(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv(
        "BOT_USERNAME",
        "ZhpchshtsPredictorBot",
    )

    now = int(time.time())
    launch_token = create_tma_launch_token(
        chat_id=-1001234567890,
        chat_type="supergroup",
        chat_title="Футбольные прогнозы",
        secret=BOT_TOKEN,
        now=now,
    )
    init_data = build_signed_init_data(
        {
            "auth_date": str(now),
            "query_id": "AAEAAAE",
            "user": (
                '{"id":123,"first_name":"Eugene",'
                '"last_name":"Sabir","username":"evsab"}'
            ),
            "chat_type": "supergroup",
            "start_param": launch_token,
        }
    )

    client = TestClient(create_app())

    response = client.get(
        "/api/tma/bootstrap",
        headers={"X-Telegram-Init-Data": init_data},
    )

    assert response.status_code == 200
    assert response.json() == {
        "context": {
            "user": {
                "id": 123,
                "first_name": "Eugene",
                "last_name": "Sabir",
                "username": "evsab",
            },
            "chat": {
                "id": -1001234567890,
                "type": "supergroup",
                "title": "Футбольные прогнозы",
            },
        }
    }

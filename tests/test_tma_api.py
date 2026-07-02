from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from app.database import create_connection, initialize_database
from app.main import create_app
from app.tma_auth import calculate_init_data_hash
from app.tma_launch import create_tma_launch_token


BOT_TOKEN = "123456789:test-token"
TELEGRAM_CHAT_ID = -1001234567890
CHAT_TITLE = "Футбольные прогнозы"


def build_signed_init_data(fields: dict[str, str]) -> str:
    signed_fields = fields.copy()
    signed_fields["hash"] = calculate_init_data_hash(
        signed_fields,
        bot_token=BOT_TOKEN,
    )
    return urlencode(signed_fields)


def configure_test_environment(
    *,
    monkeypatch,
    database_path: Path,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv(
        "BOT_USERNAME",
        "ZhpchshtsPredictorBot",
    )
    monkeypatch.setenv("DATABASE_PATH", str(database_path))


def build_context_init_data() -> str:
    now = int(time.time())
    launch_token = create_tma_launch_token(
        chat_id=TELEGRAM_CHAT_ID,
        chat_type="supergroup",
        chat_title=CHAT_TITLE,
        secret=BOT_TOKEN,
        now=now,
    )

    return build_signed_init_data(
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


def build_tma_headers(
    *,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = {
        "X-Telegram-Init-Data": build_context_init_data(),
    }

    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key

    return headers


def test_bootstrap_rejects_missing_init_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    response = client.get("/api/tma/bootstrap")

    assert response.status_code == 401
    assert response.json() == {
        "detail": "Telegram init data is required.",
    }


def test_bootstrap_returns_verified_context_and_empty_active_contests(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    response = client.get(
        "/api/tma/bootstrap",
        headers=build_tma_headers(),
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
                "id": TELEGRAM_CHAT_ID,
                "type": "supergroup",
                "title": CHAT_TITLE,
            },
        },
        "active_contests": [],
    }


def test_bootstrap_returns_all_active_contests_for_context_chat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    with create_connection(database_path) as connection:
        chat_id = int(
            connection.execute(
                """
                INSERT INTO chats (telegram_chat_id, title)
                VALUES (?, ?)
                """,
                (TELEGRAM_CHAT_ID, CHAT_TITLE),
            ).lastrowid
        )
        other_chat_id = int(
            connection.execute(
                """
                INSERT INTO chats (telegram_chat_id, title)
                VALUES (?, ?)
                """,
                (-1009876543210, "Другой чат"),
            ).lastrowid
        )

        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, "Чемпионат мира 2026", "world-cup-2026", 1),
        )
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (
                chat_id,
                "Лига чемпионов 2026/27",
                "champions-league-2026-27",
                1,
            ),
        )
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, "Архивный конкурс", "archived-contest", 0),
        )
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (other_chat_id, "Чужой конкурс", "other-chat-contest", 1),
        )

    client = TestClient(create_app())

    response = client.get(
        "/api/tma/bootstrap",
        headers=build_tma_headers(),
    )

    assert response.status_code == 200

    response_data = response.json()

    assert response_data["context"]["chat"] == {
        "id": TELEGRAM_CHAT_ID,
        "type": "supergroup",
        "title": CHAT_TITLE,
    }
    assert [
        {
            "name": contest["name"],
            "slug": contest["slug"],
        }
        for contest in response_data["active_contests"]
    ] == [
        {
            "name": "Лига чемпионов 2026/27",
            "slug": "champions-league-2026-27",
        },
        {
            "name": "Чемпионат мира 2026",
            "slug": "world-cup-2026",
        },
    ]
    assert all(contest["created_at"] for contest in response_data["active_contests"])


def test_create_contest_rejects_missing_init_data(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    response = client.post(
        "/api/tma/contests",
        headers={"Idempotency-Key": "create-contest-1"},
        json={"name": "ЧМ-2026: прогнозы"},
    )

    assert response.status_code == 401
    assert response.json() == {
        "detail": "Telegram init data is required.",
    }


def test_create_contest_requires_idempotency_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(),
        json={"name": "ЧМ-2026: прогнозы"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Не передан ключ идемпотентности создания конкурса.",
    }


def test_create_contest_creates_world_cup_2026_contest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(
            idempotency_key="create-contest-1",
        ),
        json={"name": "ЧМ-2026: прогнозы"},
    )

    assert response.status_code == 201

    response_data = response.json()

    assert response_data["was_created"] is True
    assert response_data["contest"]["id"] == 1
    assert response_data["contest"]["name"] == "ЧМ-2026: прогнозы"
    assert response_data["contest"]["slug"].startswith("world-cup-2026-")
    assert response_data["contest"]["created_at"]

    with create_connection(database_path) as connection:
        contests_count = connection.execute("SELECT COUNT(*) FROM contests").fetchone()[
            0
        ]
        competitions_count = connection.execute(
            "SELECT COUNT(*) FROM competitions"
        ).fetchone()[0]
        scoring_rule_sets_count = connection.execute(
            "SELECT COUNT(*) FROM scoring_rule_sets"
        ).fetchone()[0]
        events_count = connection.execute("SELECT COUNT(*) FROM event_log").fetchone()[
            0
        ]
        requests_count = connection.execute(
            "SELECT COUNT(*) FROM contest_creation_requests"
        ).fetchone()[0]

    assert contests_count == 1
    assert competitions_count == 1
    assert scoring_rule_sets_count == 1
    assert events_count == 1
    assert requests_count == 1


def test_create_contest_reuses_result_for_same_idempotency_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    first_response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(
            idempotency_key="same-request",
        ),
        json={"name": "ЧМ-2026: прогнозы"},
    )
    second_response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(
            idempotency_key="same-request",
        ),
        json={"name": "ЧМ-2026: прогнозы"},
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 200

    first_response_data = first_response.json()
    second_response_data = second_response.json()

    assert first_response_data["was_created"] is True
    assert second_response_data["was_created"] is False
    assert second_response_data["contest"] == first_response_data["contest"]

    with create_connection(database_path) as connection:
        contests_count = connection.execute("SELECT COUNT(*) FROM contests").fetchone()[
            0
        ]
        events_count = connection.execute("SELECT COUNT(*) FROM event_log").fetchone()[
            0
        ]

    assert contests_count == 1
    assert events_count == 1


def test_create_contest_rejects_reused_key_with_different_name(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    first_response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(
            idempotency_key="same-request",
        ),
        json={"name": "Первый конкурс"},
    )
    second_response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(
            idempotency_key="same-request",
        ),
        json={"name": "Другой конкурс"},
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 409
    assert second_response.json() == {
        "detail": (
            "Этот запрос на создание конкурса уже использован с другими данными."
        ),
    }

    with create_connection(database_path) as connection:
        contests_count = connection.execute("SELECT COUNT(*) FROM contests").fetchone()[
            0
        ]

    assert contests_count == 1


def test_create_contest_validates_name_without_creating_records(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(
            idempotency_key="invalid-name",
        ),
        json={"name": "   "},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Введите название конкурса.",
    }

    with create_connection(database_path) as connection:
        chats_count = connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        users_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        contests_count = connection.execute("SELECT COUNT(*) FROM contests").fetchone()[
            0
        ]

    assert chats_count == 0
    assert users_count == 0
    assert contests_count == 0


def test_create_contest_allows_parallel_contests_in_one_chat(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )

    client = TestClient(create_app())

    first_response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(
            idempotency_key="first-request",
        ),
        json={"name": "Основной конкурс"},
    )
    second_response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(
            idempotency_key="second-request",
        ),
        json={"name": "Конкурс для друзей"},
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert (
        first_response.json()["contest"]["id"]
        != second_response.json()["contest"]["id"]
    )

    bootstrap_response = client.get(
        "/api/tma/bootstrap",
        headers=build_tma_headers(),
    )

    assert bootstrap_response.status_code == 200
    assert [
        contest["name"] for contest in bootstrap_response.json()["active_contests"]
    ] == [
        "Конкурс для друзей",
        "Основной конкурс",
    ]

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetChatAdministrators
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.database import create_connection, initialize_database
from app.main import create_app as create_application
from app.supermoderator_service import (
    SupermoderatorAssignment,
    assign_supermoderator,
    revoke_supermoderator,
)
from app.telegram_username_resolver import (
    ResolvedTelegramUser,
    UnavailableTelegramUsernameResolver,
)
from app.tma_api import (
    _authorize_contest_management,
    _authorize_role_management,
    get_telegram_administrators_client,
    get_telegram_username_resolver,
)
from app.tma_auth import calculate_init_data_hash
from app.tma_launch import create_tma_launch_token
from app.user_service import ChatActor, upsert_chat_actor


BOT_TOKEN = "123456789:test-token"
TELEGRAM_CHAT_ID = -1001234567890
CHAT_TITLE = "Футбольные прогнозы"


class FakeTelegramAdministratorsClient:
    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        assert chat_id == TELEGRAM_CHAT_ID
        return []


class AdminTelegramAdministratorsClient:
    calls = 0

    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        assert chat_id == TELEGRAM_CHAT_ID
        self.calls += 1
        return [SimpleNamespace(user=SimpleNamespace(id=123))]


class MutableTelegramAdministratorsClient:
    def __init__(self, administrator_ids: list[int]) -> None:
        self.administrator_ids = administrator_ids
        self.calls = 0

    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        assert chat_id == TELEGRAM_CHAT_ID
        self.calls += 1
        return [
            SimpleNamespace(user=SimpleNamespace(id=telegram_user_id))
            for telegram_user_id in self.administrator_ids
        ]


class FakeUsernameResolver:
    def __init__(self) -> None:
        self.usernames: list[str] = []

    async def resolve_username(self, username: str) -> ResolvedTelegramUser:
        self.usernames.append(username)
        return ResolvedTelegramUser(
            telegram_user_id=456,
            username="target_user",
            first_name="Target",
            last_name="User",
        )

    async def close(self) -> None:
        return None


def create_app() -> FastAPI:
    app = create_application()
    app.dependency_overrides[get_telegram_administrators_client] = (
        FakeTelegramAdministratorsClient
    )
    return app


def create_app_with_telegram_client(telegram_client: object) -> FastAPI:
    app = create_application()
    app.dependency_overrides[get_telegram_administrators_client] = lambda: (
        telegram_client
    )
    return app


def assign_current_user_as_supermoderator(
    database_path: Path,
) -> tuple[ChatActor, SupermoderatorAssignment]:
    actor = upsert_chat_actor(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        chat_title=CHAT_TITLE,
        telegram_user_id=123,
        username="evsab",
        first_name="Eugene",
        last_name="Sabir",
    )
    assignment = assign_supermoderator(
        database_path=database_path,
        chat_id=actor.chat_id,
        user_id=actor.actor_user_id,
        assigned_by_user_id=actor.actor_user_id,
    )
    return actor, assignment


class UnavailableTelegramAdministratorsClient:
    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        raise TelegramNetworkError(
            method=GetChatAdministrators(chat_id=chat_id),
            message="private network detail",
        )


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
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "false")


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


def create_tma_contest(
    client: TestClient,
    *,
    idempotency_key: str = "create-contest-1",
    name: str = "ЧМ-2026: прогнозы",
) -> dict[str, object]:
    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key=idempotency_key),
        json={"name": name},
    )

    assert response.status_code == 201
    return response.json()["contest"]


def create_role_management_app(
    *,
    telegram_client=AdminTelegramAdministratorsClient,
    username_resolver=None,
) -> FastAPI:
    app = create_application()
    app.dependency_overrides[get_telegram_administrators_client] = telegram_client
    if username_resolver is not None:
        app.dependency_overrides[get_telegram_username_resolver] = lambda: (
            username_resolver
        )
    return app


def test_telegram_admin_resolves_assigns_lists_and_revokes_supermoderator(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    resolver = FakeUsernameResolver()
    admin_client = AdminTelegramAdministratorsClient()
    client = TestClient(
        create_role_management_app(
            telegram_client=lambda: admin_client,
            username_resolver=resolver,
        )
    )

    resolved = client.post(
        "/api/tma/access/users/resolve",
        headers=build_tma_headers(),
        json={"username": "@Target_User"},
    )

    assert resolved.status_code == 200
    assert resolver.usernames == ["@Target_User"]
    user = resolved.json()["user"]
    assert user["telegram_user_id"] == 456
    assert resolved.json()["has_active_assignment"] is False
    assert resolved.json()["effective_role"] == "participant"

    assigned = client.put(
        f"/api/tma/access/supermoderators/{user['telegram_user_id']}",
        headers=build_tma_headers(),
    )
    repeated = client.put(
        f"/api/tma/access/supermoderators/{user['telegram_user_id']}",
        headers=build_tma_headers(),
    )
    listed = client.get(
        "/api/tma/access/supermoderators",
        headers=build_tma_headers(),
    )

    assert assigned.status_code == 200
    assert assigned.json()["created"] is True
    assert repeated.json()["created"] is False
    assert len(listed.json()["assignments"]) == 1
    item = listed.json()["assignments"][0]
    assert item["user"]["telegram_user_id"] == 456
    assert item["assigned_by"]["telegram_user_id"] == 123
    assert item["effective_role"] == "supermoderator"

    revoked = client.delete(
        f"/api/tma/access/supermoderators/{user['telegram_user_id']}",
        headers=build_tma_headers(),
    )
    repeated_revoke = client.delete(
        f"/api/tma/access/supermoderators/{user['telegram_user_id']}",
        headers=build_tma_headers(),
    )

    assert revoked.status_code == 200
    assert revoked.json()["assignment"]["revoked_at"] is not None
    assert repeated_revoke.status_code == 404
    assert repeated_revoke.json()["detail"]["code"] == "active_assignment_not_found"
    assert resolver.usernames == ["@Target_User"]
    assert admin_client.calls == 6


def test_telegram_admin_assigns_unknown_user_by_exact_telegram_id_without_mtproto(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(
        create_role_management_app(
            username_resolver=UnavailableTelegramUsernameResolver(),
        )
    )

    resolved = client.post(
        "/api/tma/access/users/resolve",
        headers=build_tma_headers(),
        json={"target": "789012345"},
    )
    assigned = client.put(
        "/api/tma/access/supermoderators/789012345",
        headers=build_tma_headers(),
    )
    repeated = client.put(
        "/api/tma/access/supermoderators/789012345",
        headers=build_tma_headers(),
    )

    assert resolved.status_code == 200
    assert resolved.json()["selection_type"] == "telegram_user_id"
    assert resolved.json()["user"] == {
        "id": resolved.json()["user"]["id"],
        "telegram_user_id": 789012345,
        "username": None,
        "first_name": "",
        "last_name": None,
    }
    assert assigned.status_code == 200
    assert assigned.json()["created"] is True
    assert assigned.json()["assignment"]["user"]["telegram_user_id"] == 789012345
    assert repeated.json()["created"] is False


def test_invalid_telegram_user_id_is_rejected_before_target_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(
        create_role_management_app(
            username_resolver=UnavailableTelegramUsernameResolver(),
        )
    )

    resolved = client.post(
        "/api/tma/access/users/resolve",
        headers=build_tma_headers(),
        json={"target": "-123"},
    )
    assigned = client.put(
        "/api/tma/access/supermoderators/0",
        headers=build_tma_headers(),
    )

    assert resolved.status_code == 400
    assert resolved.json()["detail"]["code"] == "telegram_user_id_invalid"
    assert assigned.status_code == 400
    assert assigned.json()["detail"]["code"] == "telegram_user_id_invalid"
    with create_connection(database_path) as connection:
        invalid_targets = connection.execute(
            "SELECT COUNT(*) FROM users WHERE telegram_user_id <= 0"
        ).fetchone()[0]
    assert invalid_targets == 0


def test_non_admin_cannot_use_username_resolver(monkeypatch, tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    resolver = FakeUsernameResolver()
    client = TestClient(
        create_role_management_app(
            telegram_client=FakeTelegramAdministratorsClient,
            username_resolver=resolver,
        )
    )

    response = client.post(
        "/api/tma/access/users/resolve",
        headers=build_tma_headers(),
        json={"username": "target_user"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "telegram_admin_required"
    assert resolver.usernames == []


def test_telegram_admin_resolves_username_without_leading_at(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    resolver = FakeUsernameResolver()
    client = TestClient(create_role_management_app(username_resolver=resolver))

    response = client.post(
        "/api/tma/access/users/resolve",
        headers=build_tma_headers(),
        json={"target": "target_user"},
    )

    assert response.status_code == 200
    assert response.json()["selection_type"] == "username"
    assert response.json()["user"]["telegram_user_id"] == 456
    assert resolver.usernames == ["target_user"]


def test_role_management_fails_closed_when_admin_check_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(
        create_role_management_app(
            telegram_client=UnavailableTelegramAdministratorsClient,
            username_resolver=FakeUsernameResolver(),
        )
    )

    response = client.get(
        "/api/tma/access/supermoderators",
        headers=build_tma_headers(),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "telegram_admin_verification_unavailable"
    )


def test_resolve_is_unavailable_without_mtproto_configuration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(
        create_role_management_app(
            username_resolver=UnavailableTelegramUsernameResolver(),
        )
    )

    response = client.post(
        "/api/tma/access/users/resolve",
        headers=build_tma_headers(),
        json={"username": "target_user"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == ("username_resolution_not_configured")


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
        "access": {
            "verification_status": "verified",
            "role": "participant",
            "can_manage_contests": False,
            "can_manage_roles": False,
            "enforcement_enabled": False,
        },
        "active_contests": [],
        "completed_contests": [],
    }


def test_bootstrap_returns_active_and_completed_contests_for_context_chat(
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
            (chat_id, "Завершённый конкурс", "completed-contest", 0),
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
    assert [
        {
            "name": contest["name"],
            "slug": contest["slug"],
        }
        for contest in response_data["completed_contests"]
    ] == [
        {
            "name": "Завершённый конкурс",
            "slug": "completed-contest",
        },
    ]
    assert all(contest["created_at"] for contest in response_data["active_contests"])


def test_telegram_failure_keeps_bootstrap_data_and_does_not_enable_enforcement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )
    app = create_application()
    app.dependency_overrides[get_telegram_administrators_client] = (
        FakeTelegramAdministratorsClient
    )
    client = TestClient(app)

    contest = create_tma_contest(client)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    app.dependency_overrides[get_telegram_administrators_client] = (
        UnavailableTelegramAdministratorsClient
    )
    with create_connection(database_path) as connection:
        chat_id = connection.execute(
            "SELECT id FROM chats WHERE telegram_chat_id = ?",
            (TELEGRAM_CHAT_ID,),
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, 0)
            """,
            (chat_id, "Завершённый конкурс", "completed-contest"),
        )
    response = client.get("/api/tma/bootstrap", headers=build_tma_headers())

    assert response.status_code == 200
    response_data = response.json()
    assert response_data["context"]["chat"]["id"] == TELEGRAM_CHAT_ID
    assert [item["id"] for item in response_data["active_contests"]] == [contest["id"]]
    assert response_data["access"] == {
        "verification_status": "unavailable",
        "role": None,
        "can_manage_contests": False,
        "can_manage_roles": False,
        "enforcement_enabled": True,
    }
    assert [item["slug"] for item in response_data["completed_contests"]] == [
        "completed-contest"
    ]


def test_contest_management_is_allowed_without_enforcement_or_telegram_check(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class UnexpectedTelegramAdministratorsClient:
        async def get_chat_administrators(self, chat_id: int) -> list[object]:
            raise AssertionError(f"Unexpected Telegram request for chat {chat_id}")

    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(
        create_app_with_telegram_client(UnexpectedTelegramAdministratorsClient())
    )

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="enforcement-disabled"),
        json={"name": "Разрешённый конкурс"},
    )

    assert response.status_code == 201


def test_telegram_admin_can_manage_contests_with_enforcement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    telegram_client = AdminTelegramAdministratorsClient()
    client = TestClient(create_app_with_telegram_client(telegram_client))

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="telegram-admin"),
        json={"name": "Конкурс администратора"},
    )

    assert response.status_code == 201
    assert telegram_client.calls == 1


def test_telegram_admin_gain_and_loss_apply_to_the_next_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    telegram_client = MutableTelegramAdministratorsClient([])
    client = TestClient(create_app_with_telegram_client(telegram_client))
    payload = {"name": "Конкурс с актуальными правами"}

    denied_before_gain = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="admin-rights"),
        json=payload,
    )
    telegram_client.administrator_ids = [123]
    allowed_after_gain = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="admin-rights"),
        json=payload,
    )
    telegram_client.administrator_ids = []
    denied_after_loss = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="admin-rights-after-loss"),
        json={"name": "Второй конкурс"},
    )

    assert denied_before_gain.status_code == 403
    assert allowed_after_gain.status_code == 201
    assert denied_after_loss.status_code == 403
    assert telegram_client.calls == 3
    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM contest_creation_requests"
            ).fetchone()[0]
            == 1
        )


def test_verified_supermoderator_can_manage_contests_with_enforcement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    assign_current_user_as_supermoderator(database_path)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    client = TestClient(create_app())

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="verified-supermoderator"),
        json={"name": "Конкурс супермодератора"},
    )

    assert response.status_code == 201


def test_supermoderator_assignment_from_another_chat_does_not_grant_access(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    actor = upsert_chat_actor(
        database_path=database_path,
        telegram_chat_id=-1009876543210,
        chat_title="Другой чат",
        telegram_user_id=123,
        username="evsab",
        first_name="Eugene",
        last_name="Sabir",
    )
    assign_supermoderator(
        database_path=database_path,
        chat_id=actor.chat_id,
        user_id=actor.actor_user_id,
        assigned_by_user_id=actor.actor_user_id,
    )
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    client = TestClient(create_app())

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="other-chat-supermoderator"),
        json={"name": "Недоступный конкурс"},
    )

    assert response.status_code == 403
    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 0


def test_participant_cannot_manage_contests_or_create_idempotency_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    client = TestClient(create_app())

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="participant-denied"),
        json={"name": "Запрещённый конкурс"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "contest_management_forbidden",
        "message": (
            "Управлять конкурсами могут только администраторы чата и супермодераторы."
        ),
    }
    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM contest_creation_requests"
            ).fetchone()[0]
            == 0
        )


def test_denied_match_creation_does_not_create_match_or_idempotency_record(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(create_app())
    contest = create_tma_contest(
        client,
        idempotency_key="contest-before-enforcement",
    )
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")

    response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="participant-match-denied"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "contest_management_forbidden"
    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM match_creation_requests"
            ).fetchone()[0]
            == 0
        )


def test_contest_management_verification_unavailable_is_503(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    client = TestClient(
        create_app_with_telegram_client(UnavailableTelegramAdministratorsClient())
    )

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="verification-unavailable"),
        json={"name": "Недоступный конкурс"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == (
        "contest_management_verification_unavailable"
    )


def test_local_supermoderator_can_manage_when_telegram_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    assign_current_user_as_supermoderator(database_path)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    client = TestClient(
        create_app_with_telegram_client(UnavailableTelegramAdministratorsClient())
    )

    response = client.post(
        "/api/tma/contests",
        headers=build_tma_headers(idempotency_key="local-supermoderator"),
        json={"name": "Конкурс супермодератора"},
    )

    assert response.status_code == 201


def test_supermoderator_assignment_and_revocation_apply_to_next_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    client = TestClient(
        create_app_with_telegram_client(UnavailableTelegramAdministratorsClient())
    )
    headers = build_tma_headers(idempotency_key="role-refresh")
    payload = {"name": "Проверка актуальной роли"}

    assert (
        client.post("/api/tma/contests", headers=headers, json=payload).status_code
        == 503
    )

    actor, assignment = assign_current_user_as_supermoderator(database_path)
    assert (
        client.post("/api/tma/contests", headers=headers, json=payload).status_code
        == 201
    )

    revoke_supermoderator(
        database_path=database_path,
        chat_id=actor.chat_id,
        user_id=assignment.user_id,
        revoked_by_user_id=actor.actor_user_id,
    )
    repeated_response = client.post(
        "/api/tma/contests",
        headers=headers,
        json=payload,
    )

    assert repeated_response.status_code == 503
    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM contest_creation_requests"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("is_supermoderator", [False, True])
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/api/tma/access/supermoderators", None),
        ("POST", "/api/tma/access/users/resolve", {"target": "456"}),
        ("PUT", "/api/tma/access/supermoderators/456", None),
        ("DELETE", "/api/tma/access/supermoderators/456", None),
    ],
)
def test_participants_and_supermoderators_cannot_use_role_management_routes(
    monkeypatch,
    tmp_path: Path,
    is_supermoderator: bool,
    method: str,
    path: str,
    payload: dict[str, str] | None,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    if is_supermoderator:
        assign_current_user_as_supermoderator(database_path)
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    client = TestClient(
        create_role_management_app(
            telegram_client=FakeTelegramAdministratorsClient,
            username_resolver=FakeUsernameResolver(),
        )
    )
    with create_connection(database_path) as connection:
        counts_before = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("chats", "users", "supermoderator_assignments")
        )

    response = client.request(
        method,
        path,
        headers=build_tma_headers(),
        json=payload,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "telegram_admin_required"
    with create_connection(database_path) as connection:
        counts_after = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("chats", "users", "supermoderator_assignments")
        )
    assert counts_after == counts_before


def test_tma_route_registry_is_complete_and_uses_expected_authorization() -> None:
    expected_routes = {
        ("GET", "/api/tma/bootstrap"): "read",
        ("GET", "/api/tma/access/supermoderators"): "role_management",
        ("POST", "/api/tma/access/users/resolve"): "role_management",
        (
            "PUT",
            "/api/tma/access/supermoderators/{telegram_user_id}",
        ): "role_management",
        (
            "DELETE",
            "/api/tma/access/supermoderators/{telegram_user_id}",
        ): "role_management",
        ("POST", "/api/tma/contests"): "contest_management",
        (
            "DELETE",
            "/api/tma/contests/{contest_id}/matches/{match_id}",
        ): "contest_management",
        ("GET", "/api/tma/contests/{contest_id}"): "read",
        (
            "POST",
            "/api/tma/contests/{contest_id}/complete",
        ): "contest_management",
        ("DELETE", "/api/tma/contests/{contest_id}"): "contest_management",
        (
            "POST",
            "/api/tma/contests/{contest_id}/matches",
        ): "contest_management",
        (
            "PUT",
            "/api/tma/contests/{contest_id}/matches/{match_id}/prediction",
        ): "prediction",
        (
            "PUT",
            "/api/tma/contests/{contest_id}/matches/{match_id}/result",
        ): "contest_management",
        (
            "PUT",
            "/api/tma/contests/{contest_id}/match-prediction-publication/settings",
        ): "contest_management",
        (
            "PUT",
            "/api/tma/contests/{contest_id}/champion-prediction/settings",
        ): "contest_management",
        (
            "PUT",
            "/api/tma/contests/{contest_id}/champion-prediction",
        ): "prediction",
        (
            "PUT",
            "/api/tma/contests/{contest_id}/champion",
        ): "contest_management",
    }
    app = create_application()
    route_dependencies = {
        (method, route.path): {
            dependency.call for dependency in route.dependant.dependencies
        }
        for route in app.routes
        if hasattr(route, "dependant")
        for method in route.methods
        if route.path.startswith("/api/tma/")
    }

    assert route_dependencies.keys() == expected_routes.keys()
    for route, category in expected_routes.items():
        dependencies = route_dependencies[route]
        assert (_authorize_contest_management in dependencies) is (
            category == "contest_management"
        )
        assert (_authorize_role_management in dependencies) is (
            category == "role_management"
        )


def test_participant_predictions_remain_available_with_enforcement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class UnexpectedTelegramAdministratorsClient:
        async def get_chat_administrators(self, chat_id: int) -> list[object]:
            raise AssertionError(f"Unexpected Telegram request for chat {chat_id}")

    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    app = create_app()
    client = TestClient(app)
    contest = create_tma_contest(client, idempotency_key="prediction-contest")
    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="prediction-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )
    assert match_response.status_code == 201
    match = match_response.json()["match"]
    settings_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": "2030-06-01T18:00:00Z",
            "points": 5,
        },
    )
    assert settings_response.status_code == 200

    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")
    app.dependency_overrides[get_telegram_administrators_client] = (
        UnexpectedTelegramAdministratorsClient
    )
    match_prediction_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}/prediction",
        headers=build_tma_headers(),
        json={
            "predicted_home_score": 1,
            "predicted_away_score": 0,
            "predicted_advancing_team_id": match["home_team_id"],
        },
    )
    champion_prediction_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction",
        headers=build_tma_headers(),
        json={"predicted_team_id": match["home_team_id"]},
    )

    assert match_prediction_response.status_code == 201
    assert champion_prediction_response.status_code == 200


def test_prediction_payload_cannot_replace_the_verified_author(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(create_app())
    contest = create_tma_contest(client, idempotency_key="spoofed-author-contest")
    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="spoofed-author-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )
    assert match_response.status_code == 201
    match = match_response.json()["match"]
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")

    response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}/prediction",
        headers=build_tma_headers(),
        json={
            "predicted_home_score": 1,
            "predicted_away_score": 0,
            "predicted_advancing_team_id": match["home_team_id"],
            "telegram_user_id": 999,
            "user_id": 999,
            "contest_id": 999,
            "match_id": 999,
        },
    )

    assert response.status_code == 201
    with create_connection(database_path) as connection:
        authors = connection.execute(
            """
            SELECT users.telegram_user_id
            FROM match_predictions
            JOIN users ON users.id = match_predictions.user_id
            """
        ).fetchall()
    assert [row["telegram_user_id"] for row in authors] == [123]


def test_denied_administrative_operations_do_not_mutate_multiple_domain_areas(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(create_app())
    contest = create_tma_contest(client, idempotency_key="denied-mutations-contest")
    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="denied-mutations-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )
    assert match_response.status_code == 201
    match = match_response.json()["match"]
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")

    result_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 1,
            "away_score": 0,
            "advancing_team_id": match["home_team_id"],
        },
    )
    settings_response = client.put(
        (f"/api/tma/contests/{contest['id']}/match-prediction-publication/settings"),
        headers=build_tma_headers(),
        json={"enabled": True},
    )
    deletion_response = client.delete(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert result_response.status_code == 403
    assert settings_response.status_code == 403
    assert deletion_response.status_code == 403
    with create_connection(database_path) as connection:
        contest_row = connection.execute(
            """
            SELECT match_prediction_publication_enabled
            FROM contests
            WHERE id = ?
            """,
            (contest["id"],),
        ).fetchone()
        assert contest_row is not None
        assert contest_row["match_prediction_publication_enabled"] == 0
        stored_match = connection.execute(
            """
            SELECT matches.status,
                   matches.home_score_final,
                   matches.away_score_final,
                   ties.advancing_team_id
            FROM matches
            JOIN ties ON ties.id = matches.tie_id
            WHERE matches.id = ?
            """,
            (match["id"],),
        ).fetchone()
        assert tuple(stored_match) == ("scheduled", None, None, None)


def test_participant_can_read_contest_matches_and_leaderboard_with_enforcement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(create_app())
    contest = create_tma_contest(client, idempotency_key="readable-contest")
    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="readable-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )
    assert match_response.status_code == 201
    match = match_response.json()["match"]
    prediction_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}/prediction",
        headers=build_tma_headers(),
        json={
            "predicted_home_score": 1,
            "predicted_away_score": 0,
            "predicted_advancing_team_id": match["home_team_id"],
        },
    )
    assert prediction_response.status_code == 201
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "true")

    bootstrap_response = client.get(
        "/api/tma/bootstrap",
        headers=build_tma_headers(),
    )
    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert bootstrap_response.status_code == 200
    assert [item["id"] for item in bootstrap_response.json()["active_contests"]] == [
        contest["id"]
    ]
    assert contest_response.status_code == 200
    contest_details = contest_response.json()["contest"]
    assert [item["id"] for item in contest_details["matches"]] == [match["id"]]
    assert len(contest_details["leaderboard"]) == 1
    assert contest_details["leaderboard"][0]["participant_name"] == "Eugene Sabir"


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


def test_get_contest_returns_details_with_empty_matches(
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
    contest = create_tma_contest(client)

    response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "contest": {
            "id": contest["id"],
            "name": "ЧМ-2026: прогнозы",
            "slug": contest["slug"],
            "created_at": contest["created_at"],
            "is_active": True,
            "match_prediction_publication": {
                "is_enabled": False,
            },
            "champion_prediction": {
                "is_enabled": False,
                "deadline_at": None,
                "points": 5,
                "candidates": [],
                "prediction": None,
                "actual_champion": None,
                "is_open": False,
                "is_tournament_completed": False,
                "awarded_points": None,
            },
            "leaderboard": [],
            "matches": [],
        }
    }


def test_match_prediction_publication_settings_can_be_enabled(
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
    contest = create_tma_contest(client)

    response = client.put(
        (f"/api/tma/contests/{contest['id']}/match-prediction-publication/settings"),
        headers=build_tma_headers(),
        json={"enabled": True},
    )

    assert response.status_code == 200
    assert response.json() == {
        "match_prediction_publication": {
            "is_enabled": True,
        },
    }

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    assert contest_response.json()["contest"]["match_prediction_publication"] == {
        "is_enabled": True,
    }


def test_get_contest_returns_not_found_for_contest_from_other_chat(
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
        other_chat_id = int(
            connection.execute(
                """
                INSERT INTO chats (telegram_chat_id, title)
                VALUES (?, ?)
                """,
                (-1009876543210, "Другой чат"),
            ).lastrowid
        )
        contest_id = int(
            connection.execute(
                """
                INSERT INTO contests (chat_id, name, slug, is_active)
                VALUES (?, ?, ?, ?)
                """,
                (
                    other_chat_id,
                    "Чужой конкурс",
                    "other-contest",
                    1,
                ),
            ).lastrowid
        )

    client = TestClient(create_app())
    response = client.get(
        f"/api/tma/contests/{contest_id}",
        headers=build_tma_headers(),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Конкурс не найден."}


def test_create_match_creates_match_and_returns_contest_details(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )
    fixed_now = datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.tma_api._utc_now", lambda: fixed_now)
    client = TestClient(create_app())
    contest = create_tma_contest(client)

    response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-match-1"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2026-06-11T18:00:00Z",
        },
    )

    assert response.status_code == 201

    response_data = response.json()

    assert response_data["was_created"] is True
    assert response_data["match"] == {
        "id": 1,
        "tie_id": 1,
        "home_team_id": 1,
        "home_team_name": "Аргентина",
        "away_team_id": 2,
        "away_team_name": "Бразилия",
        "starts_at_utc": "2026-06-11T18:00:00Z",
        "status": "started",
        "result": None,
        "prediction": None,
        "prediction_score": None,
    }

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    assert contest_response.json()["contest"]["matches"] == [response_data["match"]]


def test_create_match_reuses_result_for_same_idempotency_key(
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
    contest = create_tma_contest(client)
    request_data = {
        "home_team_name": "Аргентина",
        "away_team_name": "Бразилия",
        "starts_at_utc": "2026-06-11T18:00:00Z",
    }

    first_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="same-match-request"),
        json=request_data,
    )
    second_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="same-match-request"),
        json=request_data,
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 200
    assert first_response.json()["was_created"] is True
    assert second_response.json()["was_created"] is False
    assert second_response.json()["match"] == first_response.json()["match"]

    with create_connection(database_path) as connection:
        matches_count = connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        events_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM event_log
            WHERE event_type = 'match.created'
            """
        ).fetchone()[0]

    assert matches_count == 1
    assert events_count == 1


def test_create_match_requires_idempotency_key(
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
    contest = create_tma_contest(client)

    response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2026-06-11T18:00:00Z",
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Не передан ключ идемпотентности создания матча.",
    }


def test_get_contest_returns_not_found_for_unknown_contest(
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
        "/api/tma/contests/999",
        headers=build_tma_headers(),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Конкурс не найден."}


def test_save_match_prediction_creates_updates_and_returns_prediction(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-future-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    match_id = match_response.json()["match"]["id"]

    first_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/prediction",
        headers=build_tma_headers(),
        json={
            "predicted_home_score": 2,
            "predicted_away_score": 1,
            "predicted_advancing_team_id": 1,
        },
    )
    second_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/prediction",
        headers=build_tma_headers(),
        json={
            "predicted_home_score": 3,
            "predicted_away_score": 1,
            "predicted_advancing_team_id": 1,
        },
    )

    assert first_response.status_code == 201
    assert first_response.json() == {
        "prediction": {
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": 1,
        },
        "was_created": True,
    }

    assert second_response.status_code == 200
    assert second_response.json() == {
        "prediction": {
            "home_score": 3,
            "away_score": 1,
            "advancing_team_id": 1,
        },
        "was_created": False,
    }

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    assert contest_response.json()["contest"]["matches"] == [
        {
            "id": match_id,
            "tie_id": 1,
            "home_team_id": 1,
            "home_team_name": "Аргентина",
            "away_team_id": 2,
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
            "status": "scheduled",
            "result": None,
            "prediction": {
                "home_score": 3,
                "away_score": 1,
                "advancing_team_id": 1,
            },
            "prediction_score": None,
        },
    ]


def test_save_match_prediction_rejects_closed_match(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-closed-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2020-06-11T18:00:00Z",
        },
    )
    assert match_response.status_code == 201
    match_id = match_response.json()["match"]["id"]

    response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/prediction",
        headers=build_tma_headers(),
        json={
            "predicted_home_score": 2,
            "predicted_away_score": 1,
            "predicted_advancing_team_id": 1,
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Прогнозы на этот матч уже закрыты.",
    }


def test_save_match_prediction_rejects_negative_score(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-future-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )
    assert match_response.status_code == 201
    match_id = match_response.json()["match"]["id"]

    response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/prediction",
        headers=build_tma_headers(),
        json={
            "predicted_home_score": -1,
            "predicted_away_score": 0,
            "predicted_advancing_team_id": 1,
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Прогноз первой команды не может быть отрицательным.",
    }


def test_save_match_result_creates_updates_and_returns_result(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-result-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2020-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    match_id = match_response.json()["match"]["id"]

    first_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": 1,
        },
    )
    second_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 1,
            "away_score": 1,
            "advancing_team_id": 2,
        },
    )

    assert first_response.status_code == 201
    assert first_response.json() == {
        "result": {
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": 1,
        },
        "was_created": True,
    }

    assert second_response.status_code == 200
    assert second_response.json() == {
        "result": {
            "home_score": 1,
            "away_score": 1,
            "advancing_team_id": 2,
        },
        "was_created": False,
    }

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    assert contest_response.json()["contest"]["matches"] == [
        {
            "id": match_id,
            "tie_id": 1,
            "home_team_id": 1,
            "home_team_name": "Аргентина",
            "away_team_id": 2,
            "away_team_name": "Бразилия",
            "starts_at_utc": "2020-06-11T18:00:00Z",
            "status": "finished",
            "result": {
                "home_score": 1,
                "away_score": 1,
                "advancing_team_id": 2,
            },
            "prediction": None,
            "prediction_score": None,
        },
    ]


def test_get_contest_returns_prediction_score_after_result(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-score-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    match = match_response.json()["match"]
    match_id = match["id"]
    home_team_id = match["home_team_id"]

    prediction_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/prediction",
        headers=build_tma_headers(),
        json={
            "predicted_home_score": 2,
            "predicted_away_score": 1,
            "predicted_advancing_team_id": home_team_id,
        },
    )

    assert prediction_response.status_code == 201

    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE matches
            SET starts_at_utc = ?
            WHERE id = ?
            """,
            ("2020-06-11T18:00:00Z", match_id),
        )

    result_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": home_team_id,
        },
    )

    assert result_response.status_code == 201

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200

    saved_match = contest_response.json()["contest"]["matches"][0]

    assert saved_match["prediction_score"] == {
        "total_points": 4,
        "awards": [
            {
                "type": "exact_score",
                "points": 3,
            },
            {
                "type": "advancing_team",
                "points": 1,
            },
        ],
    }
    assert contest_response.json()["contest"]["leaderboard"] == [
        {
            "place": 1,
            "participant_name": "Eugene Sabir",
            "total_points": 4,
            "match_predictions_count": 1,
            "champion_prediction_count": 0,
            "total_matches_count": 1,
            "prediction_history": [saved_match],
            "champion_prediction_history": None,
        }
    ]


def test_save_match_result_rejects_match_before_start(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-future-result-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    match_id = match_response.json()["match"]["id"]

    response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": 1,
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Результат можно внести только после начала матча.",
    }


def test_save_match_result_rejects_negative_score(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-result-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    match_id = match_response.json()["match"]["id"]

    response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/result",
        headers=build_tma_headers(),
        json={
            "home_score": -1,
            "away_score": 0,
            "advancing_team_id": 1,
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Результат первой команды не может быть отрицательным.",
    }


def test_save_match_result_rejects_cancelled_match(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-cancelled-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    match_id = match_response.json()["match"]["id"]

    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE matches
            SET status = 'cancelled'
            WHERE id = ?
            """,
            (match_id,),
        )

    response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": 1,
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Для отменённого матча нельзя сохранить результат.",
    }


def test_complete_contest_rejects_unfinished_match(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="unfinished-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2020-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    response = client.post(
        f"/api/tma/contests/{contest['id']}/complete",
        headers=build_tma_headers(),
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Сначала внесите финальные результаты всех матчей.",
    }


def test_complete_contest_moves_it_to_completed_and_blocks_new_match(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="completed-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2020-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    match = match_response.json()["match"]

    result_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": match["home_team_id"],
        },
    )

    assert result_response.status_code == 201

    completion_response = client.post(
        f"/api/tma/contests/{contest['id']}/complete",
        headers=build_tma_headers(),
    )

    assert completion_response.status_code == 200
    assert completion_response.json()["contest"]["id"] == contest["id"]
    assert completion_response.json()["contest"]["is_active"] is False

    bootstrap_response = client.get(
        "/api/tma/bootstrap",
        headers=build_tma_headers(),
    )

    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["active_contests"] == []
    assert [
        completed_contest["id"]
        for completed_contest in bootstrap_response.json()["completed_contests"]
    ] == [contest["id"]]

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    assert contest_response.json()["contest"]["is_active"] is False
    assert contest_response.json()["contest"]["matches"][0]["result"] == {
        "home_score": 2,
        "away_score": 1,
        "advancing_team_id": match["home_team_id"],
    }

    blocked_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="match-after-completion"),
        json={
            "home_team_name": "Франция",
            "away_team_name": "Испания",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )

    assert blocked_response.status_code == 409
    assert blocked_response.json() == {
        "detail": "Конкурс завершён. Изменения в нём больше недоступны.",
    }


def test_delete_contest_deletes_active_contest(
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
    contest = create_tma_contest(client)

    response = client.delete(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert response.status_code == 204
    assert response.content == b""

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 404
    assert contest_response.json() == {"detail": "Конкурс не найден."}

    bootstrap_response = client.get(
        "/api/tma/bootstrap",
        headers=build_tma_headers(),
    )

    assert bootstrap_response.status_code == 200
    assert bootstrap_response.json()["active_contests"] == []


def test_delete_contest_rejects_contest_from_other_chat(
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
        other_chat_id = int(
            connection.execute(
                """
                INSERT INTO chats (telegram_chat_id, title)
                VALUES (?, ?)
                """,
                (-1009876543210, "Другой чат"),
            ).lastrowid
        )
        contest_id = int(
            connection.execute(
                """
                INSERT INTO contests (chat_id, name, slug, is_active)
                VALUES (?, ?, ?, ?)
                """,
                (
                    other_chat_id,
                    "Чужой конкурс",
                    "other-contest",
                    1,
                ),
            ).lastrowid
        )

    client = TestClient(create_app())

    response = client.delete(
        f"/api/tma/contests/{contest_id}",
        headers=build_tma_headers(),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Конкурс не найден."}

    with create_connection(database_path) as connection:
        contest_row = connection.execute(
            """
            SELECT id
            FROM contests
            WHERE id = ?
            """,
            (contest_id,),
        ).fetchone()

    assert contest_row is not None


def test_delete_contest_rejects_completed_contest(
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
    contest = create_tma_contest(client)

    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contests
            SET is_active = 0
            WHERE id = ?
            """,
            (contest["id"],),
        )

    response = client.delete(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Завершённый конкурс удалить нельзя.",
    }

    with create_connection(database_path) as connection:
        contest_row = connection.execute(
            """
            SELECT is_active
            FROM contests
            WHERE id = ?
            """,
            (contest["id"],),
        ).fetchone()

    assert contest_row is not None
    assert contest_row["is_active"] == 0


def test_delete_match_returns_no_content_and_removes_match(
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

    contest = create_tma_contest(
        client,
        idempotency_key="create-contest-for-match-deletion",
    )

    create_match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(
            idempotency_key="create-match-for-deletion",
        ),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Франция",
            "starts_at_utc": "2030-07-19T18:00:00Z",
        },
    )

    assert create_match_response.status_code == 201

    match_id = create_match_response.json()["match"]["id"]

    delete_response = client.delete(
        f"/api/tma/contests/{contest['id']}/matches/{match_id}",
        headers=build_tma_headers(),
    )

    assert delete_response.status_code == 204
    assert delete_response.content == b""

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    assert contest_response.json()["contest"]["matches"] == []


def test_delete_match_rejects_unknown_match(
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

    contest = create_tma_contest(
        client,
        idempotency_key="create-contest-for-unknown-match-deletion",
    )

    delete_response = client.delete(
        f"/api/tma/contests/{contest['id']}/matches/999",
        headers=build_tma_headers(),
    )

    assert delete_response.status_code == 404
    assert delete_response.json() == {
        "detail": "Матч не найден.",
    }


def test_delete_match_rejects_completed_contest(
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

    contest = create_tma_contest(
        client,
        idempotency_key="create-completed-contest-for-match-deletion",
    )

    create_match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(
            idempotency_key="create-match-in-completed-contest",
        ),
        json={
            "home_team_name": "Испания",
            "away_team_name": "Англия",
            "starts_at_utc": "2020-07-19T18:00:00Z",
        },
    )

    assert create_match_response.status_code == 201

    match = create_match_response.json()["match"]

    save_result_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": match["home_team_id"],
        },
    )

    assert save_result_response.status_code == 201

    complete_contest_response = client.post(
        f"/api/tma/contests/{contest['id']}/complete",
        headers=build_tma_headers(),
    )

    assert complete_contest_response.status_code == 200

    delete_response = client.delete(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}",
        headers=build_tma_headers(),
    )

    assert delete_response.status_code == 409
    assert delete_response.json() == {
        "detail": "Конкурс завершён. Изменения в нём больше недоступны.",
    }


def test_get_contest_returns_leaderboard_completeness_with_champion_prediction(
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
    contest = create_tma_contest(client)

    match_response = client.post(
        f"/api/tma/contests/{contest['id']}/matches",
        headers=build_tma_headers(idempotency_key="create-champion-match"),
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2030-06-11T18:00:00Z",
        },
    )

    assert match_response.status_code == 201

    home_team_id = match_response.json()["match"]["home_team_id"]

    settings_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": "2030-06-10T18:00:00Z",
            "points": 5,
        },
    )

    assert settings_response.status_code == 200

    champion_prediction_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction",
        headers=build_tma_headers(),
        json={"predicted_team_id": home_team_id},
    )

    assert champion_prediction_response.status_code == 200

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    assert contest_response.json()["contest"]["leaderboard"] == [
        {
            "place": 1,
            "participant_name": "Eugene Sabir",
            "total_points": 0,
            "match_predictions_count": 0,
            "champion_prediction_count": 1,
            "total_matches_count": 1,
            "prediction_history": [],
            "champion_prediction_history": None,
        }
    ]

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.access_control import TelegramAdministratorsClient
from app.audit_service import (
    AuditActor,
    AuditActorRole,
    AuditEntityType,
    AuditEventType,
    record_audit_event,
)
from app.database import create_connection, initialize_database
from app.main import create_app as create_application
from app.supermoderator_service import assign_supermoderator
from app.tma_api import get_telegram_administrators_client
from app.tma_auth import calculate_init_data_hash
from app.tma_launch import create_tma_launch_token
from app.user_service import upsert_chat_actor, upsert_telegram_user


BOT_TOKEN = "123456789:test-token"
TELEGRAM_CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009876543210
TELEGRAM_USER_ID = 123
OTHER_USER_ID = 456


class MutableTelegramAdministratorsClient:
    def __init__(self, administrator_ids: list[int]) -> None:
        self.administrator_ids = administrator_ids
        self.calls = 0

    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        self.calls += 1
        return [
            SimpleNamespace(user=SimpleNamespace(id=telegram_user_id))
            for telegram_user_id in self.administrator_ids
        ]


def _configure_environment(
    monkeypatch,
    *,
    database_path: Path,
    enforcement_enabled: bool = True,
) -> None:
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("BOT_USERNAME", "ZhpchshtsPredictorBot")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv(
        "ROLE_ENFORCEMENT_ENABLED",
        "true" if enforcement_enabled else "false",
    )


def _build_headers(
    *,
    telegram_chat_id: int = TELEGRAM_CHAT_ID,
    telegram_user_id: int = TELEGRAM_USER_ID,
) -> dict[str, str]:
    now = int(time.time())
    launch_token = create_tma_launch_token(
        chat_id=telegram_chat_id,
        chat_type="supergroup",
        chat_title="Футбольные прогнозы",
        secret=BOT_TOKEN,
        now=now,
    )
    fields = {
        "auth_date": str(now),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {
                "id": telegram_user_id,
                "first_name": "Евгений",
                "last_name": "Сабиров",
                "username": "evsab",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "chat_type": "supergroup",
        "start_param": launch_token,
    }
    fields["hash"] = calculate_init_data_hash(fields, bot_token=BOT_TOKEN)
    return {"X-Telegram-Init-Data": urlencode(fields)}


def _create_app(
    telegram_client: TelegramAdministratorsClient,
) -> FastAPI:
    app = create_application()
    app.dependency_overrides[get_telegram_administrators_client] = lambda: (
        telegram_client
    )
    return app


def _create_actor(
    database_path: Path,
    *,
    telegram_chat_id: int = TELEGRAM_CHAT_ID,
    telegram_user_id: int = TELEGRAM_USER_ID,
) -> AuditActor:
    upsert_chat_actor(
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        chat_title="Футбольные прогнозы",
        telegram_user_id=telegram_user_id,
        username="evsab" if telegram_user_id == TELEGRAM_USER_ID else "other",
        first_name="Евгений" if telegram_user_id == TELEGRAM_USER_ID else "Иван",
        last_name="Сабиров" if telegram_user_id == TELEGRAM_USER_ID else None,
    )
    return AuditActor(
        telegram_chat_id=telegram_chat_id,
        telegram_user_id=telegram_user_id,
        role=AuditActorRole.TELEGRAM_ADMIN,
    )


def _record_event(
    database_path: Path,
    *,
    actor: AuditActor,
    event_type: AuditEventType = AuditEventType.CONTEST_CREATED,
    entity_type: AuditEntityType = AuditEntityType.CONTEST,
    entity_id: int | None = 7,
    contest_id: int | None = 7,
    before_state: dict[str, object] | None = None,
    after_state: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
    created_at: datetime | None = None,
) -> int:
    if after_state is None and before_state is None:
        after_state = {
            "id": entity_id,
            "name": f"Конкурс {contest_id}",
        }
    with create_connection(database_path) as connection:
        return record_audit_event(
            connection,
            actor=actor,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            contest_id=contest_id,
            before_state=before_state,
            after_state=after_state,
            metadata=metadata,
            created_at=created_at,
        )


def test_telegram_admin_reads_current_chat_audit_with_user_identity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "admin-audit.db"
    initialize_database(database_path)
    actor = _create_actor(database_path)
    event_id = _record_event(database_path, actor=actor)
    _configure_environment(monkeypatch, database_path=database_path)
    telegram_client = MutableTelegramAdministratorsClient([TELEGRAM_USER_ID])

    response = TestClient(_create_app(telegram_client)).get(
        "/api/tma/audit-events",
        headers=_build_headers(),
    )

    assert response.status_code == 200
    assert telegram_client.calls == 1
    payload = response.json()
    assert payload["next_cursor"] is None
    assert [event["id"] for event in payload["events"]] == [event_id]
    event = payload["events"][0]
    assert event["actor_user_id"] == TELEGRAM_USER_ID
    assert event["actor_role"] == "telegram_admin"
    assert event["actor"] == {
        "telegram_user_id": TELEGRAM_USER_ID,
        "username": "evsab",
        "first_name": "Евгений",
        "last_name": "Сабиров",
    }
    assert event["before_state"] is None
    assert event["after_state"]["name"] == "Конкурс 7"
    assert event["metadata"] is None


def test_supermoderator_reads_audit_with_fresh_access_check(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "supermoderator-audit.db"
    initialize_database(database_path)
    actor = _create_actor(database_path)
    chat_actor = upsert_chat_actor(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        chat_title="Футбольные прогнозы",
        telegram_user_id=TELEGRAM_USER_ID,
        username="evsab",
        first_name="Евгений",
        last_name="Сабиров",
    )
    assign_supermoderator(
        database_path=database_path,
        chat_id=chat_actor.chat_id,
        user_id=chat_actor.actor_user_id,
        assigned_by_user_id=chat_actor.actor_user_id,
        audit_actor=actor,
    )
    _record_event(database_path, actor=actor)
    _configure_environment(monkeypatch, database_path=database_path)
    telegram_client = MutableTelegramAdministratorsClient([])

    enforced_response = TestClient(_create_app(telegram_client)).get(
        "/api/tma/audit-events",
        headers=_build_headers(),
    )

    assert enforced_response.status_code == 200
    assert telegram_client.calls == 1

    _configure_environment(
        monkeypatch,
        database_path=database_path,
        enforcement_enabled=False,
    )
    unenforced_client = MutableTelegramAdministratorsClient([])
    unenforced_response = TestClient(_create_app(unenforced_client)).get(
        "/api/tma/audit-events",
        headers=_build_headers(),
    )

    assert unenforced_response.status_code == 200
    assert unenforced_client.calls == 1


def test_participant_cannot_read_audit_when_role_enforcement_is_enabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "participant-audit.db"
    initialize_database(database_path)
    _record_event(database_path, actor=_create_actor(database_path))
    _configure_environment(monkeypatch, database_path=database_path)

    response = TestClient(_create_app(MutableTelegramAdministratorsClient([]))).get(
        "/api/tma/audit-events",
        headers=_build_headers(),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "contest_management_forbidden"


def test_audit_api_isolates_chats_even_when_filters_match(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "chat-isolation.db"
    initialize_database(database_path)
    current_actor = _create_actor(database_path)
    other_actor = _create_actor(
        database_path,
        telegram_chat_id=OTHER_CHAT_ID,
        telegram_user_id=OTHER_USER_ID,
    )
    current_event_id = _record_event(
        database_path,
        actor=current_actor,
        entity_id=11,
        contest_id=11,
        after_state={"id": 11, "name": "Текущий чат"},
    )
    _record_event(
        database_path,
        actor=other_actor,
        entity_id=11,
        contest_id=11,
        after_state={"id": 11, "name": "Другой чат"},
    )
    _configure_environment(monkeypatch, database_path=database_path)

    response = TestClient(
        _create_app(MutableTelegramAdministratorsClient([TELEGRAM_USER_ID]))
    ).get(
        "/api/tma/audit-events?contest_id=11&entity_type=contest",
        headers=_build_headers(),
    )

    assert response.status_code == 200
    assert [event["id"] for event in response.json()["events"]] == [current_event_id]
    assert response.json()["filter_options"]["contests"] == [
        {"id": 11, "name": "Текущий чат", "is_deleted": False}
    ]


def test_audit_filters_and_deleted_entity_context(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-filters.db"
    initialize_database(database_path)
    actor = _create_actor(database_path)
    upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=OTHER_USER_ID,
        username="ivan",
        first_name="Иван",
        last_name=None,
    )
    other_actor = AuditActor(
        telegram_chat_id=TELEGRAM_CHAT_ID,
        telegram_user_id=OTHER_USER_ID,
        role=AuditActorRole.SUPERMODERATOR,
    )
    created_contest_id = _record_event(
        database_path,
        actor=actor,
        entity_id=21,
        contest_id=21,
        after_state={"id": 21, "name": "Удалённый кубок"},
    )
    deleted_match_id = _record_event(
        database_path,
        actor=other_actor,
        event_type=AuditEventType.MATCH_DELETED,
        entity_type=AuditEntityType.MATCH,
        entity_id=31,
        contest_id=21,
        before_state={
            "id": 31,
            "home_team": {"id": 1, "name": "Испания"},
            "away_team": {"id": 2, "name": "Франция"},
            "status": "scheduled",
        },
        after_state=None,
    )
    deleted_contest_id = _record_event(
        database_path,
        actor=actor,
        event_type=AuditEventType.CONTEST_DELETED,
        entity_type=AuditEntityType.CONTEST,
        entity_id=21,
        contest_id=21,
        before_state={"id": 21, "name": "Удалённый кубок"},
        after_state=None,
    )
    _configure_environment(monkeypatch, database_path=database_path)
    client = TestClient(
        _create_app(MutableTelegramAdministratorsClient([TELEGRAM_USER_ID]))
    )

    filter_queries = {
        "contest_id=21": {
            created_contest_id,
            deleted_match_id,
            deleted_contest_id,
        },
        "event_type=match_deleted": {deleted_match_id},
        "entity_type=match": {deleted_match_id},
        f"actor_user_id={OTHER_USER_ID}": {deleted_match_id},
    }
    for query, expected_ids in filter_queries.items():
        response = client.get(
            f"/api/tma/audit-events?{query}",
            headers=_build_headers(),
        )
        assert response.status_code == 200
        assert {event["id"] for event in response.json()["events"]} == expected_ids

    response = client.get(
        "/api/tma/audit-events?contest_id=21",
        headers=_build_headers(),
    )
    payload = response.json()
    assert payload["filter_options"]["contests"] == [
        {"id": 21, "name": "Удалённый кубок", "is_deleted": True}
    ]
    match_event = next(
        event for event in payload["events"] if event["id"] == deleted_match_id
    )
    assert match_event["entity"]["display_name"] == "Испания — Франция"
    assert match_event["entity"]["is_deleted"] is True
    assert match_event["contest"]["name"] == "Удалённый кубок"
    assert match_event["contest"]["is_deleted"] is True


def test_audit_order_and_cursor_are_stable_without_gaps_or_duplicates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-cursor.db"
    initialize_database(database_path)
    actor = _create_actor(database_path)
    shared_time = datetime(2026, 7, 26, 17, 0, tzinfo=timezone.utc)
    event_ids = [
        _record_event(
            database_path,
            actor=actor,
            entity_id=index,
            contest_id=index,
            after_state={"id": index, "name": f"Конкурс {index}"},
            created_at=shared_time if index > 1 else shared_time - timedelta(days=1),
        )
        for index in range(1, 6)
    ]
    expected_order = list(reversed(event_ids[1:])) + [event_ids[0]]
    _configure_environment(monkeypatch, database_path=database_path)
    client = TestClient(
        _create_app(MutableTelegramAdministratorsClient([TELEGRAM_USER_ID]))
    )

    collected_ids: list[int] = []
    cursor = None
    while True:
        path = "/api/tma/audit-events?limit=2"
        if cursor is not None:
            path += f"&cursor={cursor}"
        response = client.get(path, headers=_build_headers())
        assert response.status_code == 200
        payload = response.json()
        collected_ids.extend(event["id"] for event in payload["events"])
        cursor = payload["next_cursor"]
        if cursor is None:
            break

    assert collected_ids == expected_order
    assert len(collected_ids) == len(set(collected_ids))


def test_deleted_contest_acceptance_flow_is_readable_through_tma_api(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-acceptance.db"
    initialize_database(database_path)
    _configure_environment(monkeypatch, database_path=database_path)
    client = TestClient(
        _create_app(MutableTelegramAdministratorsClient([TELEGRAM_USER_ID]))
    )
    create_contest_headers = {
        **_build_headers(),
        "Idempotency-Key": "audit-acceptance-contest",
    }
    contest_response = client.post(
        "/api/tma/contests",
        headers=create_contest_headers,
        json={"name": "Проверка аудита"},
    )
    assert contest_response.status_code == 201
    contest_id = contest_response.json()["contest"]["id"]

    publication_response = client.put(
        (f"/api/tma/contests/{contest_id}/match-prediction-publication/settings"),
        headers=_build_headers(),
        json={"enabled": True},
    )
    assert publication_response.status_code == 200

    create_match_headers = {
        **_build_headers(),
        "Idempotency-Key": "audit-acceptance-match",
    }
    match_response = client.post(
        f"/api/tma/contests/{contest_id}/matches",
        headers=create_match_headers,
        json={
            "home_team_name": "Испания",
            "away_team_name": "Франция",
            "starts_at_utc": "2030-06-01T12:00:00Z",
        },
    )
    assert match_response.status_code == 201
    match_id = match_response.json()["match"]["id"]

    assert (
        client.delete(
            f"/api/tma/contests/{contest_id}/matches/{match_id}",
            headers=_build_headers(),
        ).status_code
        == 204
    )
    assert (
        client.delete(
            f"/api/tma/contests/{contest_id}",
            headers=_build_headers(),
        ).status_code
        == 204
    )

    history_response = client.get(
        f"/api/tma/audit-events?contest_id={contest_id}",
        headers=_build_headers(),
    )
    assert history_response.status_code == 200
    payload = history_response.json()
    assert [event["event_type"] for event in payload["events"]] == [
        "contest_deleted",
        "match_deleted",
        "match_created",
        "contest_updated",
        "contest_created",
    ]
    assert payload["filter_options"]["contests"] == [
        {"id": contest_id, "name": "Проверка аудита", "is_deleted": True}
    ]
    match_events = [
        event for event in payload["events"] if event["entity_type"] == "match"
    ]
    assert {event["entity"]["display_name"] for event in match_events} == {
        "Испания — Франция"
    }
    assert all(event["contest"]["name"] == "Проверка аудита" for event in match_events)
    publication_event = next(
        event for event in payload["events"] if event["event_type"] == "contest_updated"
    )
    assert (
        publication_event["before_state"]["match_prediction_publication_enabled"]
        is False
    )
    assert (
        publication_event["after_state"]["match_prediction_publication_enabled"] is True
    )


def test_audit_invalid_parameters_are_predictable_and_reads_do_not_mutate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-validation.db"
    initialize_database(database_path)
    _record_event(database_path, actor=_create_actor(database_path))
    _configure_environment(monkeypatch, database_path=database_path)
    client = TestClient(
        _create_app(MutableTelegramAdministratorsClient([TELEGRAM_USER_ID]))
    )
    with create_connection(database_path) as connection:
        before_count = connection.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]

    assert (
        client.get(
            "/api/tma/audit-events?event_type=unknown",
            headers=_build_headers(),
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/tma/audit-events?entity_type=unknown",
            headers=_build_headers(),
        ).status_code
        == 422
    )
    cursor_response = client.get(
        "/api/tma/audit-events?cursor=not-a-cursor",
        headers=_build_headers(),
    )
    assert cursor_response.status_code == 400
    assert cursor_response.json()["detail"]["code"] == "audit_cursor_invalid"
    shaped_invalid_cursor = base64.urlsafe_b64encode(
        b'{"created_at":"not-a-date","id":1}'
    ).decode("ascii")
    assert (
        client.get(
            f"/api/tma/audit-events?cursor={shaped_invalid_cursor}",
            headers=_build_headers(),
        ).status_code
        == 400
    )

    successful_response = client.get(
        "/api/tma/audit-events",
        headers=_build_headers(),
    )
    assert successful_response.status_code == 200
    with create_connection(database_path) as connection:
        after_count = connection.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]
    assert after_count == before_count


def test_audit_invalid_json_is_reported_instead_of_replaced(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "audit-invalid-json.db"
    initialize_database(database_path)
    actor = _create_actor(database_path)
    event_id = _record_event(database_path, actor=actor)
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE audit_events SET after_state = ? WHERE id = ?",
            ("{invalid", event_id),
        )
    _configure_environment(monkeypatch, database_path=database_path)

    response = TestClient(
        _create_app(MutableTelegramAdministratorsClient([TELEGRAM_USER_ID]))
    ).get(
        "/api/tma/audit-events",
        headers=_build_headers(),
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "audit_data_invalid"

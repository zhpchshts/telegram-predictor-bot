from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.audit_service import (
    AuditActor,
    AuditActorRole,
    AuditEntityType,
    AuditEventType,
    record_audit_event,
)
from app.contest_service import (
    complete_contest,
    create_match,
    create_world_cup_2026_contest,
    delete_contest,
    delete_match,
    save_champion_prediction_settings,
    save_contest_champion,
    save_match_result,
)
from app.database import create_connection, initialize_database
from app.supermoderator_service import (
    assign_supermoderator_with_status,
    revoke_supermoderator,
)
from app.user_service import get_or_create_telegram_user, upsert_chat_actor


CHAT_ID = -1001234567890
ACTOR_USER_ID = 123456789
ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=ACTOR_USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


def _create_contest(database_path: Path, *, key: str = "contest"):
    return create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Чат",
        telegram_user_id=ACTOR_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        contest_name="Кубок",
        idempotency_key=key,
        audit_actor=ACTOR,
    ).contest


def _create_match(database_path: Path, contest_id: int, *, key: str = "match"):
    return create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ACTOR_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        home_team_name="Испания",
        away_team_name="Франция",
        starts_at_utc="2026-06-01T12:00:00Z",
        idempotency_key=key,
        audit_actor=ACTOR,
    ).match


def _events(database_path: Path) -> list[sqlite3.Row]:
    with create_connection(database_path) as connection:
        return connection.execute("SELECT * FROM audit_events ORDER BY id").fetchall()


def test_audit_schema_is_added_without_removing_existing_data(tmp_path: Path) -> None:
    database_path = tmp_path / "existing.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy_data (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_data VALUES ('preserved')")

    initialize_database(database_path)

    with create_connection(database_path) as connection:
        assert connection.execute("SELECT value FROM legacy_data").fetchone()[0] == (
            "preserved"
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(audit_events)").fetchall()
        }
        table_sql = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'audit_events'
            """
        ).fetchone()["sql"]
    assert {
        "id",
        "created_at",
        "chat_id",
        "actor_user_id",
        "actor_role",
        "event_type",
        "entity_type",
        "entity_id",
        "contest_id",
        "before_state",
        "after_state",
        "metadata",
    } <= columns
    assert {
        "idx_audit_events_chat_created",
        "idx_audit_events_contest_created",
        "idx_audit_events_event_type",
    } <= indexes
    assert "'unverified'" in table_sql


def test_audit_service_stores_deterministic_json_and_utc_time(tmp_path: Path) -> None:
    database_path = tmp_path / "audit.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        record_audit_event(
            connection,
            actor=ACTOR,
            event_type=AuditEventType.CONTEST_UPDATED,
            entity_type=AuditEntityType.CONTEST,
            entity_id=7,
            contest_id=7,
            before_state={"z": 1, "a": "до"},
            after_state={"a": "после", "z": 2},
            metadata=None,
            created_at=datetime(
                2026,
                7,
                25,
                8,
                30,
                15,
                123456,
                tzinfo=timezone.utc,
            ),
        )
        connection.commit()

    event = _events(database_path)[0]
    assert event["created_at"] == "2026-07-25T08:30:15.123456Z"
    assert event["before_state"] == '{"a":"до","z":1}'
    assert json.loads(event["after_state"]) == {"a": "после", "z": 2}
    assert event["metadata"] is None


def test_contest_match_result_champion_and_deletion_events(tmp_path: Path) -> None:
    database_path = tmp_path / "actions.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest.id)

    first_result = save_match_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        match_id=match.id,
        telegram_user_id=ACTOR_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        home_score=2,
        away_score=1,
        advancing_team_id=match.home_team_id,
        now_utc=datetime(2026, 6, 2, tzinfo=timezone.utc),
        audit_actor=ACTOR,
    )
    assert first_result.was_created is True
    save_match_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        match_id=match.id,
        telegram_user_id=ACTOR_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        home_score=1,
        away_score=1,
        advancing_team_id=match.away_team_id,
        now_utc=datetime(2026, 6, 2, tzinfo=timezone.utc),
        audit_actor=ACTOR,
    )
    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ACTOR_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        enabled=True,
        deadline_at="2026-06-01T13:00:00Z",
        points=5,
        audit_actor=ACTOR,
    )
    save_contest_champion(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ACTOR_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        champion_team_id=match.away_team_id,
        now_utc=datetime(2026, 6, 2, tzinfo=timezone.utc),
        audit_actor=ACTOR,
    )
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ACTOR_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        now_utc=datetime(2026, 6, 2, tzinfo=timezone.utc),
        audit_actor=ACTOR,
    )

    disposable = _create_contest(database_path, key="disposable")
    disposable_match = _create_match(
        database_path,
        disposable.id,
        key="disposable-match",
    )
    delete_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=disposable.id,
        match_id=disposable_match.id,
        telegram_user_id=ACTOR_USER_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        audit_actor=ACTOR,
    )
    delete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=disposable.id,
        audit_actor=ACTOR,
    )

    events = _events(database_path)
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "contest_created",
        "match_created",
        "match_result_set",
        "match_result_changed",
        "contest_updated",
        "contest_champion_set",
        "contest_finished",
        "contest_created",
        "match_created",
        "match_deleted",
        "contest_deleted",
    ]
    changed_result = events[3]
    assert changed_result["chat_id"] == CHAT_ID
    assert changed_result["actor_user_id"] == ACTOR_USER_ID
    assert changed_result["actor_role"] == "telegram_admin"
    assert json.loads(changed_result["before_state"])["home_score"] == 2
    assert json.loads(changed_result["after_state"])["home_score"] == 1
    assert events[-1]["after_state"] is None


def test_idempotent_creation_and_unchanged_result_do_not_add_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "idempotent.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    _create_contest(database_path)
    match = _create_match(database_path, contest.id)
    _create_match(database_path, contest.id)

    result_kwargs = {
        "database_path": database_path,
        "telegram_chat_id": CHAT_ID,
        "contest_id": contest.id,
        "match_id": match.id,
        "telegram_user_id": ACTOR_USER_ID,
        "first_name": "Администратор",
        "last_name": None,
        "username": "admin",
        "home_score": 2,
        "away_score": 1,
        "advancing_team_id": match.home_team_id,
        "now_utc": datetime(2026, 6, 2, tzinfo=timezone.utc),
        "audit_actor": ACTOR,
    }
    save_match_result(**result_kwargs)
    save_match_result(**result_kwargs)

    assert [event["event_type"] for event in _events(database_path)] == [
        "contest_created",
        "match_created",
        "match_result_set",
    ]


def test_supermoderator_events_distinguish_actor_and_target(tmp_path: Path) -> None:
    database_path = tmp_path / "roles.db"
    initialize_database(database_path)
    actor = upsert_chat_actor(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Чат",
        telegram_user_id=ACTOR_USER_ID,
        username="admin",
        first_name="Администратор",
        last_name=None,
    )
    target = get_or_create_telegram_user(
        database_path=database_path,
        telegram_user_id=987654321,
    )

    assigned = assign_supermoderator_with_status(
        database_path=database_path,
        chat_id=actor.chat_id,
        user_id=target.id,
        assigned_by_user_id=actor.actor_user_id,
        audit_actor=ACTOR,
    )
    assign_supermoderator_with_status(
        database_path=database_path,
        chat_id=actor.chat_id,
        user_id=target.id,
        assigned_by_user_id=actor.actor_user_id,
        audit_actor=ACTOR,
    )
    revoke_supermoderator(
        database_path=database_path,
        chat_id=actor.chat_id,
        user_id=target.id,
        revoked_by_user_id=actor.actor_user_id,
        audit_actor=ACTOR,
    )

    events = _events(database_path)
    assert [event["event_type"] for event in events] == [
        "supermoderator_assigned",
        "supermoderator_revoked",
    ]
    assert events[0]["actor_user_id"] == ACTOR_USER_ID
    assert json.loads(events[0]["after_state"])["target_telegram_user_id"] == (
        target.telegram_user_id
    )
    assert events[0]["entity_id"] == assigned.assignment.id


def test_audit_failure_rolls_back_administrative_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "rollback.db"
    initialize_database(database_path)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr("app.contest_service.record_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        _create_contest(database_path)

    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM contests").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
        )


def test_main_change_failure_does_not_leave_audit_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "main-rollback.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)

    def fail_publication(*args, **kwargs):
        raise RuntimeError("synthetic main change failure")

    monkeypatch.setattr(
        "app.contest_service.create_contest_completed_publication",
        fail_publication,
    )
    with pytest.raises(RuntimeError, match="synthetic main change failure"):
        complete_contest(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest.id,
            telegram_user_id=ACTOR_USER_ID,
            first_name="Администратор",
            last_name=None,
            username="admin",
            now_utc=datetime(2026, 6, 2, tzinfo=timezone.utc),
            audit_actor=ACTOR,
        )

    with create_connection(database_path) as connection:
        is_active = connection.execute(
            "SELECT is_active FROM contests WHERE id = ?",
            (contest.id,),
        ).fetchone()["is_active"]
    assert is_active == 1
    assert [event["event_type"] for event in _events(database_path)] == [
        "contest_created"
    ]

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


class AuditEventType(StrEnum):
    CONTEST_CREATED = "contest_created"
    CONTEST_UPDATED = "contest_updated"
    CONTEST_FINISHED = "contest_finished"
    CONTEST_DELETED = "contest_deleted"
    MATCH_CREATED = "match_created"
    MATCH_UPDATED = "match_updated"
    MATCH_DELETED = "match_deleted"
    MATCH_RESULT_SET = "match_result_set"
    MATCH_RESULT_CHANGED = "match_result_changed"
    CONTEST_CHAMPION_SET = "contest_champion_set"
    CONTEST_CHAMPION_CHANGED = "contest_champion_changed"
    SWISS_STAGE_SETTINGS_UPDATED = "swiss_stage_settings_updated"
    SWISS_STAGE_RESULT_SET = "swiss_stage_result_set"
    SWISS_STAGE_RESULT_CHANGED = "swiss_stage_result_changed"
    SUPERMODERATOR_ASSIGNED = "supermoderator_assigned"
    SUPERMODERATOR_REVOKED = "supermoderator_revoked"


class AuditEntityType(StrEnum):
    CONTEST = "contest"
    MATCH = "match"
    SWISS_STAGE_PREDICTION = "swiss_stage_prediction"
    SUPERMODERATOR_ASSIGNMENT = "supermoderator_assignment"


class AuditActorRole(StrEnum):
    TELEGRAM_ADMIN = "telegram_admin"
    SUPERMODERATOR = "supermoderator"
    PARTICIPANT = "participant"


@dataclass(frozen=True, slots=True)
class AuditActor:
    telegram_chat_id: int
    telegram_user_id: int
    role: AuditActorRole | str


def record_audit_event(
    connection: sqlite3.Connection,
    *,
    actor: AuditActor,
    event_type: AuditEventType,
    entity_type: AuditEntityType,
    entity_id: int | None,
    contest_id: int | None,
    before_state: Mapping[str, Any] | None,
    after_state: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None = None,
    created_at: datetime | None = None,
) -> int:
    event_time = created_at or datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        raise ValueError("Audit event time must be timezone-aware.")
    event_time = event_time.astimezone(timezone.utc)

    cursor = connection.execute(
        """
        INSERT INTO audit_events (
            created_at,
            chat_id,
            actor_user_id,
            actor_role,
            event_type,
            entity_type,
            entity_id,
            contest_id,
            before_state,
            after_state,
            metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _serialize_datetime(event_time),
            actor.telegram_chat_id,
            actor.telegram_user_id,
            _normalize_actor_role(actor.role),
            event_type.value,
            entity_type.value,
            entity_id,
            contest_id,
            _serialize_json(before_state),
            _serialize_json(after_state),
            _serialize_json(metadata),
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Audit event was not created.")
    return int(cursor.lastrowid)


def _normalize_actor_role(value: AuditActorRole | str) -> str:
    try:
        return AuditActorRole(value).value
    except ValueError as error:
        raise ValueError("Audit actor role is invalid.") from error


def _serialize_json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Audit snapshot datetime must be timezone-aware.")
        return _serialize_datetime(value.astimezone(timezone.utc))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    raise TypeError(f"Unsupported audit snapshot value: {type(value).__name__}")


def _serialize_datetime(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

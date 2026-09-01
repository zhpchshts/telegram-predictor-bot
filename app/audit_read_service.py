from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.audit_service import AuditEntityType, AuditEventType
from app.database import create_connection


@dataclass(frozen=True, slots=True)
class AuditEventsPage:
    events: list[dict[str, object]]
    next_cursor: str | None
    contest_options: list[dict[str, object]]
    actor_options: list[dict[str, object]]


@dataclass(frozen=True, slots=True)
class _AuditCursor:
    created_at: str
    event_id: int


class AuditCursorInvalidError(ValueError):
    pass


class AuditDataIntegrityError(RuntimeError):
    def __init__(self, *, event_id: int, field_name: str) -> None:
        super().__init__(f"Audit event {event_id} has invalid {field_name} JSON data.")
        self.event_id = event_id
        self.field_name = field_name


def read_audit_events(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int | None = None,
    event_type: AuditEventType | None = None,
    entity_type: AuditEntityType | None = None,
    actor_user_id: int | None = None,
    cursor: str | None = None,
    limit: int = 30,
) -> AuditEventsPage:
    decoded_cursor = _decode_cursor(cursor) if cursor is not None else None
    with create_connection(database_path) as connection:
        contest_options = _load_contest_options(
            connection,
            telegram_chat_id=telegram_chat_id,
        )
        actor_options = _load_actor_options(
            connection,
            telegram_chat_id=telegram_chat_id,
        )
        rows = _load_event_rows(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
            event_type=event_type,
            entity_type=entity_type,
            actor_user_id=actor_user_id,
            cursor=decoded_cursor,
            limit=limit,
        )
        has_next_page = len(rows) > limit
        page_rows = rows[:limit]
        parsed_events = [_parse_event_row(row) for row in page_rows]
        users = _load_users(
            connection,
            telegram_user_ids=_event_user_ids(parsed_events),
        )
        teams = _load_teams(
            connection,
            team_ids=_event_team_ids(parsed_events),
        )

    contest_by_id = {int(option["id"]): option for option in contest_options}
    events = [
        _enrich_event(
            event,
            users=users,
            teams=teams,
            contest_by_id=contest_by_id,
        )
        for event in parsed_events
    ]
    next_cursor = None
    if has_next_page and page_rows:
        last_row = page_rows[-1]
        next_cursor = _encode_cursor(
            _AuditCursor(
                created_at=str(last_row["created_at"]),
                event_id=int(last_row["id"]),
            )
        )

    return AuditEventsPage(
        events=events,
        next_cursor=next_cursor,
        contest_options=contest_options,
        actor_options=actor_options,
    )


def _load_event_rows(
    connection: sqlite3.Connection,
    *,
    telegram_chat_id: int,
    contest_id: int | None,
    event_type: AuditEventType | None,
    entity_type: AuditEntityType | None,
    actor_user_id: int | None,
    cursor: _AuditCursor | None,
    limit: int,
) -> list[sqlite3.Row]:
    conditions = ["chat_id = ?"]
    parameters: list[object] = [telegram_chat_id]
    if contest_id is not None:
        conditions.append("contest_id = ?")
        parameters.append(contest_id)
    if event_type is not None:
        conditions.append("event_type = ?")
        parameters.append(event_type.value)
    if entity_type is not None:
        conditions.append("entity_type = ?")
        parameters.append(entity_type.value)
    if actor_user_id is not None:
        conditions.append("actor_user_id = ?")
        parameters.append(actor_user_id)
    if cursor is not None:
        conditions.append("(created_at < ? OR (created_at = ? AND id < ?))")
        parameters.extend(
            (
                cursor.created_at,
                cursor.created_at,
                cursor.event_id,
            )
        )
    parameters.append(limit + 1)
    return connection.execute(
        f"""
        SELECT
            id,
            created_at,
            actor_user_id,
            actor_role,
            event_type,
            entity_type,
            entity_id,
            contest_id,
            before_state,
            after_state,
            metadata
        FROM audit_events
        WHERE {" AND ".join(conditions)}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def _load_contest_options(
    connection: sqlite3.Connection,
    *,
    telegram_chat_id: int,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        WITH contest_ids AS (
            SELECT DISTINCT contest_id
            FROM audit_events
            WHERE chat_id = ?
              AND contest_id IS NOT NULL
        ),
        ranked_snapshots AS (
            SELECT
                contest_id,
                id AS audit_event_id,
                event_type,
                before_state,
                after_state,
                ROW_NUMBER() OVER (
                    PARTITION BY contest_id
                    ORDER BY created_at DESC, id DESC
                ) AS row_number
            FROM audit_events
            WHERE chat_id = ?
              AND contest_id IS NOT NULL
              AND entity_type = ?
        )
        SELECT
            contest_ids.contest_id,
            contests.name AS current_name,
            contests.template_key AS current_template_key,
            ranked_snapshots.event_type AS latest_event_type,
            ranked_snapshots.audit_event_id,
            ranked_snapshots.before_state,
            ranked_snapshots.after_state
        FROM contest_ids
        LEFT JOIN chats
          ON chats.telegram_chat_id = ?
        LEFT JOIN contests
          ON contests.id = contest_ids.contest_id
         AND contests.chat_id = chats.id
        LEFT JOIN ranked_snapshots
          ON ranked_snapshots.contest_id = contest_ids.contest_id
         AND ranked_snapshots.row_number = 1
        ORDER BY contest_ids.contest_id DESC
        """,
        (
            telegram_chat_id,
            telegram_chat_id,
            AuditEntityType.CONTEST.value,
            telegram_chat_id,
        ),
    ).fetchall()
    options: list[dict[str, object]] = []
    for row in rows:
        contest_id = int(row["contest_id"])
        before_state = _parse_json_field(
            row["before_state"],
            event_id=(
                int(row["audit_event_id"])
                if row["audit_event_id"] is not None
                else contest_id
            ),
            field_name="before_state",
        )
        after_state = _parse_json_field(
            row["after_state"],
            event_id=(
                int(row["audit_event_id"])
                if row["audit_event_id"] is not None
                else contest_id
            ),
            field_name="after_state",
        )
        name = _first_non_empty_string(
            row["current_name"],
            _state_value(after_state, "name"),
            _state_value(before_state, "name"),
        )
        template_key = _first_non_empty_string(
            row["current_template_key"],
            _state_value(after_state, "template_key"),
            _state_value(before_state, "template_key"),
        )
        is_deleted = (
            row["current_name"] is None
            and row["latest_event_type"] == AuditEventType.CONTEST_DELETED.value
        )
        option: dict[str, object] = {
            "id": contest_id,
            "name": name or f"Конкурс #{contest_id}",
            "is_deleted": is_deleted,
        }
        if template_key is not None:
            option["template_key"] = template_key
        options.append(option)
    return options


def _load_actor_options(
    connection: sqlite3.Connection,
    *,
    telegram_chat_id: int,
) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT
            audit_events.actor_user_id,
            users.username,
            users.first_name,
            users.last_name
        FROM audit_events
        LEFT JOIN users
          ON users.telegram_user_id = audit_events.actor_user_id
        WHERE audit_events.chat_id = ?
        GROUP BY
            audit_events.actor_user_id,
            users.username,
            users.first_name,
            users.last_name
        ORDER BY
            COALESCE(users.first_name, users.username, '') COLLATE NOCASE,
            audit_events.actor_user_id
        """,
        (telegram_chat_id,),
    ).fetchall()
    return [
        _serialize_user_row(
            telegram_user_id=int(row["actor_user_id"]),
            row=row,
        )
        for row in rows
    ]


def _load_users(
    connection: sqlite3.Connection,
    *,
    telegram_user_ids: set[int],
) -> dict[int, dict[str, object]]:
    if not telegram_user_ids:
        return {}
    placeholders = ", ".join("?" for _ in telegram_user_ids)
    rows = connection.execute(
        f"""
        SELECT telegram_user_id, username, first_name, last_name
        FROM users
        WHERE telegram_user_id IN ({placeholders})
        """,
        sorted(telegram_user_ids),
    ).fetchall()
    return {
        int(row["telegram_user_id"]): _serialize_user_row(
            telegram_user_id=int(row["telegram_user_id"]),
            row=row,
        )
        for row in rows
    }


def _load_teams(
    connection: sqlite3.Connection,
    *,
    team_ids: set[int],
) -> dict[int, str]:
    if not team_ids:
        return {}
    placeholders = ", ".join("?" for _ in team_ids)
    rows = connection.execute(
        f"""
        SELECT id, name
        FROM teams
        WHERE id IN ({placeholders})
        """,
        sorted(team_ids),
    ).fetchall()
    return {int(row["id"]): str(row["name"]) for row in rows}


def _parse_event_row(row: sqlite3.Row) -> dict[str, object]:
    event_id = int(row["id"])
    return {
        "id": event_id,
        "created_at": str(row["created_at"]),
        "actor_user_id": int(row["actor_user_id"]),
        "actor_role": str(row["actor_role"]),
        "event_type": str(row["event_type"]),
        "entity_type": str(row["entity_type"]),
        "entity_id": (int(row["entity_id"]) if row["entity_id"] is not None else None),
        "contest_id": (
            int(row["contest_id"]) if row["contest_id"] is not None else None
        ),
        "before_state": _parse_json_field(
            row["before_state"],
            event_id=event_id,
            field_name="before_state",
        ),
        "after_state": _parse_json_field(
            row["after_state"],
            event_id=event_id,
            field_name="after_state",
        ),
        "metadata": _parse_json_field(
            row["metadata"],
            event_id=event_id,
            field_name="metadata",
        ),
    }


def _parse_json_field(
    value: object,
    *,
    event_id: int,
    field_name: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise AuditDataIntegrityError(
            event_id=event_id,
            field_name=field_name,
        ) from error
    if not isinstance(parsed, dict):
        raise AuditDataIntegrityError(
            event_id=event_id,
            field_name=field_name,
        )
    return parsed


def _event_user_ids(events: list[dict[str, object]]) -> set[int]:
    user_ids = {int(event["actor_user_id"]) for event in events}
    for event in events:
        target_user_id = _target_telegram_user_id(event)
        if target_user_id is not None:
            user_ids.add(target_user_id)
    return user_ids


def _event_team_ids(events: list[dict[str, object]]) -> set[int]:
    team_ids: set[int] = set()
    for event in events:
        for state_name in ("before_state", "after_state"):
            state = event[state_name]
            if not isinstance(state, dict):
                continue
            for key in ("champion_team_id", "advancing_team_id"):
                value = state.get(key)
                if _is_integer(value):
                    team_ids.add(int(value))
            for key in ("home_team", "away_team"):
                team = state.get(key)
                if isinstance(team, dict) and _is_integer(team.get("id")):
                    team_ids.add(int(team["id"]))
            candidate_teams = state.get("teams")
            if isinstance(candidate_teams, list):
                for team in candidate_teams:
                    if isinstance(team, dict) and _is_integer(team.get("id")):
                        team_ids.add(int(team["id"]))
            actual_result = state.get("actual_result")
            if isinstance(actual_result, dict):
                for key in ("direct_team_ids", "elimination_team_ids"):
                    values = actual_result.get(key)
                    if isinstance(values, list):
                        team_ids.update(
                            int(value) for value in values if _is_integer(value)
                        )
    return team_ids


def _enrich_event(
    event: dict[str, object],
    *,
    users: dict[int, dict[str, object]],
    teams: dict[int, str],
    contest_by_id: dict[int, dict[str, object]],
) -> dict[str, object]:
    actor_user_id = int(event["actor_user_id"])
    contest_id = event["contest_id"]
    contest = contest_by_id.get(int(contest_id)) if _is_integer(contest_id) else None
    target_user_id = _target_telegram_user_id(event)
    target_user = users.get(target_user_id) if target_user_id is not None else None
    related_teams = [
        {"id": team_id, "name": name}
        for team_id, name in sorted(teams.items())
        if team_id in _event_team_ids([event])
    ]
    return {
        **event,
        "actor": users.get(actor_user_id),
        "contest": contest,
        "entity": _build_entity(
            event,
            contest=contest,
            target_user=target_user,
            target_user_id=target_user_id,
        ),
        "related_teams": related_teams,
    }


def _build_entity(
    event: dict[str, object],
    *,
    contest: dict[str, object] | None,
    target_user: dict[str, object] | None,
    target_user_id: int | None,
) -> dict[str, object]:
    entity_id = event["entity_id"]
    entity_type = event["entity_type"]
    before_state = event["before_state"]
    after_state = event["after_state"]
    if entity_type == AuditEntityType.CONTEST.value:
        name = (
            str(contest["name"])
            if contest is not None
            else _first_non_empty_string(
                _state_value(after_state, "name"),
                _state_value(before_state, "name"),
            )
        )
        return {
            "id": entity_id,
            "type": entity_type,
            "display_name": name or _fallback_entity_name("Конкурс", entity_id),
            "is_deleted": event["event_type"] == AuditEventType.CONTEST_DELETED.value,
        }
    if entity_type == AuditEntityType.MATCH.value:
        state = after_state if isinstance(after_state, dict) else before_state
        home_name = _nested_state_value(state, "home_team", "name")
        away_name = _nested_state_value(state, "away_team", "name")
        display_name = (
            f"{home_name} — {away_name}"
            if home_name and away_name
            else _fallback_entity_name("Матч", entity_id)
        )
        return {
            "id": entity_id,
            "type": entity_type,
            "display_name": display_name,
            "is_deleted": event["event_type"] == AuditEventType.MATCH_DELETED.value,
        }
    if entity_type == AuditEntityType.SWISS_STAGE_PREDICTION.value:
        is_champions_league = (
            contest is not None
            and contest.get("template_key") == "champions_league_2026_27"
        )
        return {
            "id": entity_id,
            "type": entity_type,
            "display_name": (
                "Итоги лигового этапа"
                if is_champions_league
                else "Итоги швейцарского этапа"
            ),
            "is_deleted": False,
        }
    target_display = _user_display_name(target_user, target_user_id)
    return {
        "id": entity_id,
        "type": entity_type,
        "display_name": target_display,
        "target_user_id": target_user_id,
        "target_user": target_user,
        "is_deleted": False,
    }


def _target_telegram_user_id(event: dict[str, object]) -> int | None:
    for state_name in ("metadata", "after_state", "before_state"):
        state = event.get(state_name)
        if not isinstance(state, dict):
            continue
        value = state.get("target_telegram_user_id")
        if _is_integer(value) and int(value) > 0:
            return int(value)
    return None


def _serialize_user_row(
    *,
    telegram_user_id: int,
    row: sqlite3.Row,
) -> dict[str, object]:
    return {
        "telegram_user_id": telegram_user_id,
        "username": str(row["username"]) if row["username"] else None,
        "first_name": str(row["first_name"]) if row["first_name"] else None,
        "last_name": str(row["last_name"]) if row["last_name"] else None,
    }


def _user_display_name(
    user: dict[str, object] | None,
    telegram_user_id: int | None,
) -> str:
    if user is not None:
        full_name = " ".join(
            str(value)
            for value in (user.get("first_name"), user.get("last_name"))
            if value
        )
        if full_name:
            return full_name
        if user.get("username"):
            return f"@{user['username']}"
    if telegram_user_id is not None:
        return f"Telegram ID {telegram_user_id}"
    return "Назначение роли"


def _fallback_entity_name(prefix: str, entity_id: object) -> str:
    return f"{prefix} #{entity_id}" if entity_id is not None else prefix


def _state_value(state: object, key: str) -> object | None:
    return state.get(key) if isinstance(state, dict) else None


def _nested_state_value(state: object, key: str, nested_key: str) -> str | None:
    nested = _state_value(state, key)
    if not isinstance(nested, dict):
        return None
    value = nested.get(nested_key)
    return str(value) if value else None


def _first_non_empty_string(*values: object) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value)
    return None


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _encode_cursor(cursor: _AuditCursor) -> str:
    payload = json.dumps(
        {
            "created_at": cursor.created_at,
            "id": cursor.event_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> _AuditCursor:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            f"{value}{padding}",
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise AuditCursorInvalidError("Audit cursor is invalid.") from error
    if not isinstance(payload, dict):
        raise AuditCursorInvalidError("Audit cursor is invalid.")
    created_at = payload.get("created_at")
    event_id = payload.get("id")
    if (
        not isinstance(created_at, str)
        or not created_at
        or not _is_integer(event_id)
        or int(event_id) <= 0
    ):
        raise AuditCursorInvalidError("Audit cursor is invalid.")
    try:
        parsed_created_at = datetime.fromisoformat(
            created_at.removesuffix("Z") + "+00:00"
        )
    except ValueError as error:
        raise AuditCursorInvalidError("Audit cursor is invalid.") from error
    if not created_at.endswith("Z") or parsed_created_at.tzinfo is None:
        raise AuditCursorInvalidError("Audit cursor is invalid.")
    return _AuditCursor(
        created_at=created_at,
        event_id=int(event_id),
    )

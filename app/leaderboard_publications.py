from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from app.audit_service import (
    AuditActor,
    AuditEntityType,
    AuditEventType,
    record_audit_event,
)
from app.contest_service import (
    ContestNotFoundError,
    get_contest_details,
    resolve_leaderboard_tiebreak_reason,
)
from app.database import database_connection
from app.publication_outbox import (
    create_manual_leaderboard_publication,
    serialize_service_time,
)
from app.user_service import upsert_telegram_user


class IntermediateLeaderboardUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IntermediateLeaderboardPublicationResult:
    request_id: int
    was_created: bool


def queue_intermediate_leaderboard_publication(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    idempotency_key: str,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> IntermediateLeaderboardPublicationResult:
    normalized_key = _normalize_idempotency_key(idempotency_key)
    captured_at = _resolve_now_utc(now_utc)
    actor = upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=telegram_user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
    )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        contest_id=contest_id,
        telegram_user_id=telegram_user_id,
        now_utc=captured_at,
    )

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest = connection.execute(
            """
            SELECT contests.id, contests.name, contests.is_active
            FROM contests
            JOIN chats ON chats.id = contests.chat_id
            WHERE contests.id = ? AND chats.telegram_chat_id = ?
            """,
            (contest_id, telegram_chat_id),
        ).fetchone()
        if contest is None:
            raise ContestNotFoundError("Конкурс не найден в этом чате.")

        existing = connection.execute(
            """
            SELECT id
            FROM leaderboard_publication_snapshots
            WHERE contest_id = ?
              AND actor_user_id = ?
              AND idempotency_key = ?
            """,
            (contest_id, actor.id, normalized_key),
        ).fetchone()
        if existing is not None:
            return IntermediateLeaderboardPublicationResult(
                request_id=int(existing["id"]),
                was_created=False,
            )

        if not bool(contest["is_active"]) or not details.is_active:
            raise IntermediateLeaderboardUnavailableError(
                "Для завершённого конкурса нельзя публиковать промежуточный рейтинг."
            )
        if not details.leaderboard or not any(
            entry.calculated_predictions_count > 0 for entry in details.leaderboard
        ):
            raise IntermediateLeaderboardUnavailableError(
                "Промежуточный рейтинг появится после расчёта первого прогноза."
            )

        snapshot = _build_snapshot(
            contest_name=str(contest["name"]),
            captured_at=captured_at,
            leaderboard=details.leaderboard,
        )
        snapshot_cursor = connection.execute(
            """
            INSERT INTO leaderboard_publication_snapshots (
                contest_id,
                actor_user_id,
                idempotency_key,
                captured_at,
                snapshot_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                contest_id,
                actor.id,
                normalized_key,
                serialize_service_time(captured_at),
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
        if snapshot_cursor.lastrowid is None:
            raise RuntimeError("Не удалось сохранить снимок рейтинга.")
        snapshot_id = int(snapshot_cursor.lastrowid)

        record_audit_event(
            connection,
            actor=audit_actor,
            event_type=(AuditEventType.INTERMEDIATE_LEADERBOARD_PUBLICATION_REQUESTED),
            entity_type=AuditEntityType.CONTEST,
            entity_id=contest_id,
            contest_id=contest_id,
            before_state=None,
            after_state={
                "captured_at": captured_at,
                "participant_count": len(details.leaderboard),
            },
            metadata={"snapshot_id": snapshot_id},
            created_at=captured_at,
        )

        event_cursor = connection.execute(
            """
            INSERT INTO event_log (
                contest_id,
                actor_user_id,
                event_type,
                entity_type,
                entity_id,
                payload_json
            )
            VALUES (?, ?, ?, 'leaderboard_snapshot', ?, '{}')
            """,
            (
                contest_id,
                actor.id,
                "contest.intermediate_leaderboard_publication_requested",
                snapshot_id,
            ),
        )
        if event_cursor.lastrowid is None:
            raise RuntimeError("Не удалось записать событие публикации рейтинга.")

        create_manual_leaderboard_publication(
            connection,
            contest_id=contest_id,
            snapshot_id=snapshot_id,
            event_id=int(event_cursor.lastrowid),
            now_utc=captured_at,
        )

    return IntermediateLeaderboardPublicationResult(
        request_id=snapshot_id,
        was_created=True,
    )


def _build_snapshot(*, contest_name: str, captured_at: datetime, leaderboard) -> dict:
    top_tiebreak_reason = None
    if len(leaderboard) > 1 and (
        leaderboard[0].total_points == leaderboard[1].total_points
    ):
        top_tiebreak_reason = resolve_leaderboard_tiebreak_reason(
            leaderboard[0],
            leaderboard[1],
        )
    return {
        "version": 1,
        "contest_name": contest_name,
        "captured_at": serialize_service_time(captured_at),
        "top_tiebreak_reason": top_tiebreak_reason,
        "entries": [
            {
                "place": entry.place,
                "participant_name": entry.participant_name,
                "total_points": entry.total_points,
            }
            for entry in leaderboard
        ],
    }


def _normalize_idempotency_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Не передан ключ публикации рейтинга.")
    if len(normalized) > 128:
        raise ValueError("Некорректный ключ публикации рейтинга.")
    return normalized


def _resolve_now_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")
    return value.astimezone(timezone.utc)

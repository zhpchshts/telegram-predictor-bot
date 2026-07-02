from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import secrets

from app.database import database_connection


WORLD_CUP_2026_COMPETITION_NAME = "Чемпионат мира"
WORLD_CUP_2026_SEASON = "2026"
WORLD_CUP_2026_COMPETITION_TYPE = "world_cup"

DEFAULT_EXACT_SCORE_POINTS = 3
DEFAULT_GOAL_DIFFERENCE_POINTS = 2
DEFAULT_OUTCOME_POINTS = 1
DEFAULT_ADVANCING_TEAM_POINTS = 1


class ContestCreationConflictError(ValueError):
    """Raised when an idempotency key is reused with different request data."""


@dataclass(frozen=True, slots=True)
class ActiveContestSummary:
    id: int
    name: str
    slug: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ContestCreationResult:
    contest: ActiveContestSummary
    was_created: bool


def get_active_contests(
    *,
    database_path: Path,
    telegram_chat_id: int,
) -> tuple[ActiveContestSummary, ...]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                contests.id,
                contests.name,
                contests.slug,
                contests.created_at
            FROM contests
            JOIN chats ON chats.id = contests.chat_id
            WHERE chats.telegram_chat_id = ?
              AND contests.is_active = 1
            ORDER BY contests.created_at DESC, contests.id DESC
            """,
            (telegram_chat_id,),
        ).fetchall()

    return tuple(_active_contest_summary_from_row(row) for row in rows)


def create_world_cup_2026_contest(
    *,
    database_path: Path,
    telegram_chat_id: int,
    chat_title: str | None,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    contest_name: str,
    idempotency_key: str,
) -> ContestCreationResult:
    normalized_contest_name = _normalize_contest_name(contest_name)
    normalized_idempotency_key = _normalize_idempotency_key(idempotency_key)
    request_fingerprint = _build_request_fingerprint(normalized_contest_name)

    with database_connection(database_path) as connection:
        chat_id = _upsert_chat(
            connection,
            telegram_chat_id=telegram_chat_id,
            title=chat_title,
        )
        user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

        existing_request = connection.execute(
            """
            SELECT request_fingerprint, contest_id
            FROM contest_creation_requests
            WHERE chat_id = ?
              AND actor_user_id = ?
              AND idempotency_key = ?
            """,
            (chat_id, user_id, normalized_idempotency_key),
        ).fetchone()

        if existing_request is not None:
            if existing_request["request_fingerprint"] != request_fingerprint:
                raise ContestCreationConflictError(
                    "Этот запрос на создание конкурса уже использован с другими данными."
                )

            contest_row = connection.execute(
                """
                SELECT id, name, slug, created_at
                FROM contests
                WHERE id = ?
                """,
                (existing_request["contest_id"],),
            ).fetchone()

            if contest_row is None:
                raise RuntimeError(
                    "Не удалось найти конкурс, созданный по предыдущему запросу."
                )

            return ContestCreationResult(
                contest=_active_contest_summary_from_row(contest_row),
                was_created=False,
            )

        slug = _build_contest_slug()

        contest_id = int(
            connection.execute(
                """
                INSERT INTO contests (chat_id, name, slug)
                VALUES (?, ?, ?)
                """,
                (chat_id, normalized_contest_name, slug),
            ).lastrowid
        )

        competition_id = int(
            connection.execute(
                """
                INSERT INTO competitions (
                    contest_id,
                    name,
                    season,
                    competition_type
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    contest_id,
                    WORLD_CUP_2026_COMPETITION_NAME,
                    WORLD_CUP_2026_SEASON,
                    WORLD_CUP_2026_COMPETITION_TYPE,
                ),
            ).lastrowid
        )

        scoring_rule_set_id = int(
            connection.execute(
                """
                INSERT INTO scoring_rule_sets (
                    competition_id,
                    version,
                    exact_score_points,
                    goal_difference_points,
                    outcome_points,
                    advancing_team_points
                )
                VALUES (?, 1, ?, ?, ?, ?)
                """,
                (
                    competition_id,
                    DEFAULT_EXACT_SCORE_POINTS,
                    DEFAULT_GOAL_DIFFERENCE_POINTS,
                    DEFAULT_OUTCOME_POINTS,
                    DEFAULT_ADVANCING_TEAM_POINTS,
                ),
            ).lastrowid
        )

        event_payload = json.dumps(
            {
                "competition": {
                    "id": competition_id,
                    "name": WORLD_CUP_2026_COMPETITION_NAME,
                    "season": WORLD_CUP_2026_SEASON,
                    "type": WORLD_CUP_2026_COMPETITION_TYPE,
                },
                "contest_name": normalized_contest_name,
                "scoring_rule_set": {
                    "advancing_team_points": DEFAULT_ADVANCING_TEAM_POINTS,
                    "exact_score_points": DEFAULT_EXACT_SCORE_POINTS,
                    "goal_difference_points": DEFAULT_GOAL_DIFFERENCE_POINTS,
                    "id": scoring_rule_set_id,
                    "outcome_points": DEFAULT_OUTCOME_POINTS,
                    "version": 1,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        connection.execute(
            """
            INSERT INTO event_log (
                contest_id,
                actor_user_id,
                event_type,
                entity_type,
                entity_id,
                payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                contest_id,
                user_id,
                "contest.created",
                "contest",
                contest_id,
                event_payload,
            ),
        )

        connection.execute(
            """
            INSERT INTO contest_creation_requests (
                chat_id,
                actor_user_id,
                idempotency_key,
                request_fingerprint,
                contest_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user_id,
                normalized_idempotency_key,
                request_fingerprint,
                contest_id,
            ),
        )

        contest_row = connection.execute(
            """
            SELECT id, name, slug, created_at
            FROM contests
            WHERE id = ?
            """,
            (contest_id,),
        ).fetchone()

    if contest_row is None:
        raise RuntimeError("Не удалось создать конкурс.")

    return ContestCreationResult(
        contest=_active_contest_summary_from_row(contest_row),
        was_created=True,
    )


def _upsert_chat(
    connection,
    *,
    telegram_chat_id: int,
    title: str | None,
) -> int:
    connection.execute(
        """
        INSERT INTO chats (telegram_chat_id, title)
        VALUES (?, ?)
        ON CONFLICT(telegram_chat_id) DO UPDATE SET
            title = excluded.title
        """,
        (telegram_chat_id, title or "Без названия"),
    )

    row = connection.execute(
        """
        SELECT id
        FROM chats
        WHERE telegram_chat_id = ?
        """,
        (telegram_chat_id,),
    ).fetchone()

    if row is None:
        raise RuntimeError("Не удалось определить чат конкурса.")

    return int(row["id"])


def _upsert_user(
    connection,
    *,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
) -> int:
    connection.execute(
        """
        INSERT INTO users (
            telegram_user_id,
            username,
            first_name,
            last_name
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name
        """,
        (
            telegram_user_id,
            username,
            first_name,
            last_name,
        ),
    )

    row = connection.execute(
        """
        SELECT id
        FROM users
        WHERE telegram_user_id = ?
        """,
        (telegram_user_id,),
    ).fetchone()

    if row is None:
        raise RuntimeError("Не удалось определить участника конкурса.")

    return int(row["id"])


def _normalize_contest_name(value: str) -> str:
    normalized_value = " ".join(value.split())

    if not normalized_value:
        raise ValueError("Введите название конкурса.")

    if len(normalized_value) > 80:
        raise ValueError("Название конкурса не должно быть длиннее 80 символов.")

    return normalized_value


def _normalize_idempotency_key(value: str) -> str:
    normalized_value = value.strip()

    if not normalized_value:
        raise ValueError("Не передан ключ создания конкурса.")

    if len(normalized_value) > 128:
        raise ValueError("Некорректный ключ создания конкурса.")

    return normalized_value


def _build_request_fingerprint(contest_name: str) -> str:
    payload = json.dumps(
        {
            "competition_name": WORLD_CUP_2026_COMPETITION_NAME,
            "competition_season": WORLD_CUP_2026_SEASON,
            "competition_type": WORLD_CUP_2026_COMPETITION_TYPE,
            "contest_name": contest_name,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_contest_slug() -> str:
    return f"world-cup-2026-{secrets.token_hex(8)}"


def _active_contest_summary_from_row(row) -> ActiveContestSummary:
    return ActiveContestSummary(
        id=int(row["id"]),
        name=str(row["name"]),
        slug=str(row["slug"]),
        created_at=str(row["created_at"]),
    )

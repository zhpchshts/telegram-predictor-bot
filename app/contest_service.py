from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets

from app.database import database_connection
from app.scoring_service import (
    recalculate_match_prediction_scores,
    recalculate_tie_prediction_scores,
)


WORLD_CUP_2026_COMPETITION_NAME = "Чемпионат мира"
WORLD_CUP_2026_SEASON = "2026"
WORLD_CUP_2026_COMPETITION_TYPE = "world_cup"

DEFAULT_EXACT_SCORE_POINTS = 3
DEFAULT_GOAL_DIFFERENCE_POINTS = 2
DEFAULT_OUTCOME_POINTS = 1
DEFAULT_ADVANCING_TEAM_POINTS = 1


class ContestCreationConflictError(ValueError):
    """Raised when an idempotency key is reused with different request data."""


class ContestNotFoundError(ValueError):
    """Raised when a contest is unavailable in the current Telegram chat."""


class MatchCreationConflictError(ValueError):
    """Raised when an idempotency key is reused with different request data."""


class MatchNotFoundError(ValueError):
    """Raised when a match is unavailable in the current contest."""


class PredictionUnavailableError(ValueError):
    """Raised when a prediction can no longer be changed."""


class MatchResultUnavailableError(ValueError):
    """Raised when a result cannot be saved for the match."""


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


@dataclass(frozen=True, slots=True)
class MatchPrediction:
    home_score: int
    away_score: int
    advancing_team_id: int


@dataclass(frozen=True, slots=True)
class MatchResult:
    home_score: int
    away_score: int
    advancing_team_id: int


@dataclass(frozen=True, slots=True)
class PredictionScoreAward:
    score_type: str
    points: int


@dataclass(frozen=True, slots=True)
class MatchPredictionScore:
    total_points: int
    awards: tuple[PredictionScoreAward, ...]


@dataclass(frozen=True, slots=True)
class MatchSummary:
    id: int
    tie_id: int
    home_team_id: int
    home_team_name: str
    away_team_id: int
    away_team_name: str
    starts_at_utc: str
    status: str
    result: MatchResult | None
    prediction: MatchPrediction | None
    prediction_score: MatchPredictionScore | None


@dataclass(frozen=True, slots=True)
class ContestDetails:
    id: int
    name: str
    slug: str
    created_at: str
    matches: tuple[MatchSummary, ...]


@dataclass(frozen=True, slots=True)
class MatchCreationResult:
    match: MatchSummary
    was_created: bool


@dataclass(frozen=True, slots=True)
class MatchPredictionSaveResult:
    prediction: MatchPrediction
    was_created: bool


@dataclass(frozen=True, slots=True)
class MatchResultSaveResult:
    result: MatchResult
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


def get_contest_details(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int | None = None,
) -> ContestDetails:
    with database_connection(database_path) as connection:
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        match_rows = connection.execute(
            """
            SELECT
                matches.id,
                matches.tie_id,
                home_team.id AS home_team_id,
                home_team.name AS home_team_name,
                away_team.id AS away_team_id,
                away_team.name AS away_team_name,
                matches.starts_at_utc,
                matches.status,
                matches.home_score_final,
                matches.away_score_final,
                ties.advancing_team_id,
                match_predictions.predicted_home_score,
                match_predictions.predicted_away_score,
                tie_predictions.predicted_advancing_team_id,
                match_prediction_scores.score_type AS match_score_type,
                match_prediction_scores.points AS match_score_points,
                tie_prediction_scores.points AS advancing_team_points
            FROM matches
            JOIN ties
                ON ties.id = matches.tie_id
            JOIN stages
                ON stages.id = matches.stage_id
            JOIN competitions
                ON competitions.id = stages.competition_id
            JOIN teams AS home_team
                ON home_team.id = matches.home_team_id
            JOIN teams AS away_team
                ON away_team.id = matches.away_team_id
            LEFT JOIN match_predictions
                ON match_predictions.match_id = matches.id
                AND match_predictions.user_id = (
                    SELECT users.id
                    FROM users
                    WHERE users.telegram_user_id = ?
                )
            LEFT JOIN tie_predictions
                ON tie_predictions.tie_id = matches.tie_id
                AND tie_predictions.user_id = (
                    SELECT users.id
                    FROM users
                    WHERE users.telegram_user_id = ?
                )
            LEFT JOIN match_prediction_scores
                ON match_prediction_scores.match_prediction_id =
                    match_predictions.id
            LEFT JOIN tie_prediction_scores
                ON tie_prediction_scores.tie_prediction_id =
                    tie_predictions.id
            WHERE competitions.contest_id = ?
            ORDER BY matches.starts_at_utc ASC, matches.id ASC
            """,
            (telegram_user_id, telegram_user_id, contest_id),
        ).fetchall()

    return ContestDetails(
        id=int(contest_row["id"]),
        name=str(contest_row["name"]),
        slug=str(contest_row["slug"]),
        created_at=str(contest_row["created_at"]),
        matches=tuple(_match_summary_from_row(row) for row in match_rows),
    )


def create_match(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    home_team_name: str,
    away_team_name: str,
    starts_at_utc: str,
    idempotency_key: str,
) -> MatchCreationResult:
    normalized_home_team_name = _normalize_team_name(
        home_team_name,
        field_name="Название первой команды",
    )
    normalized_away_team_name = _normalize_team_name(
        away_team_name,
        field_name="Название второй команды",
    )
    if normalized_home_team_name.casefold() == normalized_away_team_name.casefold():
        raise ValueError("В матче должны участвовать разные команды.")

    normalized_starts_at_utc = _normalize_starts_at_utc(starts_at_utc)
    normalized_idempotency_key = _normalize_match_idempotency_key(idempotency_key)
    request_fingerprint = _build_match_request_fingerprint(
        home_team_name=normalized_home_team_name,
        away_team_name=normalized_away_team_name,
        starts_at_utc=normalized_starts_at_utc,
    )

    with database_connection(database_path) as connection:
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        existing_request = connection.execute(
            """
            SELECT request_fingerprint, match_id
            FROM match_creation_requests
            WHERE contest_id = ?
              AND actor_user_id = ?
              AND idempotency_key = ?
            """,
            (contest_id, actor_user_id, normalized_idempotency_key),
        ).fetchone()
        if existing_request is not None:
            if existing_request["request_fingerprint"] != request_fingerprint:
                raise MatchCreationConflictError(
                    "Этот запрос на создание матча уже использован с другими данными."
                )

            match_row = _get_match_row(
                connection,
                contest_id=contest_id,
                match_id=int(existing_request["match_id"]),
            )
            if match_row is None:
                raise RuntimeError(
                    "Не удалось найти матч, созданный по предыдущему запросу."
                )
            return MatchCreationResult(
                match=_match_summary_from_row(match_row),
                was_created=False,
            )

        competition_row = connection.execute(
            """
            SELECT
                competitions.id AS competition_id,
                scoring_rule_sets.id AS scoring_rule_set_id
            FROM competitions
            JOIN scoring_rule_sets
                ON scoring_rule_sets.competition_id = competitions.id
            WHERE competitions.contest_id = ?
              AND competitions.is_active = 1
              AND scoring_rule_sets.is_active = 1
            ORDER BY competitions.id ASC, scoring_rule_sets.version DESC
            LIMIT 1
            """,
            (contest_id,),
        ).fetchone()
        if competition_row is None:
            raise RuntimeError("Не удалось найти активные правила конкурса.")

        stage_id, stage_name, stage_type = _get_or_create_first_stage(
            connection,
            competition_id=int(competition_row["competition_id"]),
        )
        home_team_id, home_team_was_created = _find_or_create_team(
            connection,
            team_name=normalized_home_team_name,
        )
        away_team_id, away_team_was_created = _find_or_create_team(
            connection,
            team_name=normalized_away_team_name,
        )
        scoring_rule_set_id = int(competition_row["scoring_rule_set_id"])
        tie_position = _get_next_tie_position(
            connection,
            stage_id=stage_id,
        )
        tie_name = f"{normalized_home_team_name} — {normalized_away_team_name}"

        tie_id = int(
            connection.execute(
                """
                INSERT INTO ties (
                    stage_id,
                    scoring_rule_set_id,
                    name,
                    position,
                    is_two_legged
                )
                VALUES (?, ?, ?, ?, 0)
                """,
                (
                    stage_id,
                    scoring_rule_set_id,
                    tie_name,
                    tie_position,
                ),
            ).lastrowid
        )

        match_id = int(
            connection.execute(
                """
                INSERT INTO matches (
                    stage_id,
                    tie_id,
                    scoring_rule_set_id,
                    home_team_id,
                    away_team_id,
                    starts_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    stage_id,
                    tie_id,
                    scoring_rule_set_id,
                    home_team_id,
                    away_team_id,
                    normalized_starts_at_utc,
                ),
            ).lastrowid
        )
        event_payload = json.dumps(
            {
                "away_team": {
                    "id": away_team_id,
                    "name": normalized_away_team_name,
                    "was_created": away_team_was_created,
                },
                "home_team": {
                    "id": home_team_id,
                    "name": normalized_home_team_name,
                    "was_created": home_team_was_created,
                },
                "scoring_rule_set_id": scoring_rule_set_id,
                "stage": {
                    "id": stage_id,
                    "name": stage_name,
                    "type": stage_type,
                },
                "tie": {
                    "id": tie_id,
                    "is_two_legged": False,
                    "name": tie_name,
                    "position": tie_position,
                },
                "starts_at_utc": normalized_starts_at_utc,
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
                actor_user_id,
                "match.created",
                "match",
                match_id,
                event_payload,
            ),
        )
        connection.execute(
            """
            INSERT INTO match_creation_requests (
                contest_id,
                actor_user_id,
                idempotency_key,
                request_fingerprint,
                match_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                contest_id,
                actor_user_id,
                normalized_idempotency_key,
                request_fingerprint,
                match_id,
            ),
        )
        match_row = _get_match_row(
            connection,
            contest_id=contest_id,
            match_id=match_id,
        )

    if match_row is None:
        raise RuntimeError("Не удалось создать матч.")

    return MatchCreationResult(
        match=_match_summary_from_row(match_row),
        was_created=True,
    )


def save_match_prediction(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    match_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    predicted_home_score: int,
    predicted_away_score: int,
    predicted_advancing_team_id: int,
    now_utc: datetime | None = None,
) -> MatchPredictionSaveResult:
    normalized_home_score = _normalize_prediction_score(
        predicted_home_score,
        field_name="Прогноз первой команды",
    )
    normalized_away_score = _normalize_prediction_score(
        predicted_away_score,
        field_name="Прогноз второй команды",
    )
    normalized_advancing_team_id = _normalize_advancing_team_id(
        predicted_advancing_team_id,
        field_name="Прогноз победителя противостояния",
    )
    resolved_now_utc = _resolve_now_utc(now_utc)

    with database_connection(database_path) as connection:
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        match_row = _get_match_row(
            connection,
            contest_id=contest_id,
            match_id=match_id,
        )

        if match_row is None:
            raise MatchNotFoundError("Матч не найден.")

        if not _is_prediction_open(match_row, now_utc=resolved_now_utc):
            raise PredictionUnavailableError("Прогнозы на этот матч уже закрыты.")

        if match_row["tie_id"] is None:
            raise RuntimeError("У матча не определено противостояние.")

        _validate_advancing_team_for_match(
            match_row,
            advancing_team_id=normalized_advancing_team_id,
            home_score=normalized_home_score,
            away_score=normalized_away_score,
            field_name="Прогноз победителя противостояния",
        )

        tie_id = int(match_row["tie_id"])
        user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

        existing_match_prediction = connection.execute(
            """
            SELECT id
            FROM match_predictions
            WHERE match_id = ? AND user_id = ?
            """,
            (match_id, user_id),
        ).fetchone()

        existing_tie_prediction = connection.execute(
            """
            SELECT id
            FROM tie_predictions
            WHERE tie_id = ? AND user_id = ?
            """,
            (tie_id, user_id),
        ).fetchone()

        connection.execute(
            """
            INSERT INTO match_predictions (
                match_id,
                user_id,
                predicted_home_score,
                predicted_away_score
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(match_id, user_id) DO UPDATE SET
                predicted_home_score = excluded.predicted_home_score,
                predicted_away_score = excluded.predicted_away_score,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                match_id,
                user_id,
                normalized_home_score,
                normalized_away_score,
            ),
        )

        connection.execute(
            """
            INSERT INTO tie_predictions (
                tie_id,
                user_id,
                predicted_advancing_team_id
            )
            VALUES (?, ?, ?)
            ON CONFLICT(tie_id, user_id) DO UPDATE SET
                predicted_advancing_team_id = excluded.predicted_advancing_team_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                tie_id,
                user_id,
                normalized_advancing_team_id,
            ),
        )

        return MatchPredictionSaveResult(
            prediction=MatchPrediction(
                home_score=normalized_home_score,
                away_score=normalized_away_score,
                advancing_team_id=normalized_advancing_team_id,
            ),
            was_created=(
                existing_match_prediction is None and existing_tie_prediction is None
            ),
        )


def save_match_result(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    match_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    home_score: int,
    away_score: int,
    advancing_team_id: int,
    now_utc: datetime | None = None,
) -> MatchResultSaveResult:
    normalized_home_score = _normalize_match_result_score(
        home_score,
        field_name="Результат первой команды",
    )
    normalized_away_score = _normalize_match_result_score(
        away_score,
        field_name="Результат второй команды",
    )
    normalized_advancing_team_id = _normalize_advancing_team_id(
        advancing_team_id,
        field_name="Победитель противостояния",
    )
    resolved_now_utc = _resolve_now_utc(now_utc)

    with database_connection(database_path) as connection:
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        match_row = _get_match_row(
            connection,
            contest_id=contest_id,
            match_id=match_id,
        )

        if match_row is None:
            raise MatchNotFoundError("Матч не найден.")

        if str(match_row["status"]) == "cancelled":
            raise MatchResultUnavailableError(
                "Для отменённого матча нельзя сохранить результат."
            )

        if not _is_match_result_available(
            match_row,
            now_utc=resolved_now_utc,
        ):
            raise MatchResultUnavailableError(
                "Результат можно внести только после начала матча."
            )

        if match_row["tie_id"] is None:
            raise RuntimeError("У матча не определено противостояние.")

        _validate_advancing_team_for_match(
            match_row,
            advancing_team_id=normalized_advancing_team_id,
            home_score=normalized_home_score,
            away_score=normalized_away_score,
            field_name="Победитель противостояния",
        )

        tie_id = int(match_row["tie_id"])
        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        previous_result = _match_result_from_row(match_row)

        connection.execute(
            """
            UPDATE matches
            SET
                status = 'finished',
                home_score_final = ?,
                away_score_final = ?
            WHERE id = ?
            """,
            (
                normalized_home_score,
                normalized_away_score,
                match_id,
            ),
        )

        connection.execute(
            """
            UPDATE ties
            SET advancing_team_id = ?
            WHERE id = ?
            """,
            (
                normalized_advancing_team_id,
                tie_id,
            ),
        )

        recalculate_match_prediction_scores(
            connection,
            match_id=match_id,
        )
        recalculate_tie_prediction_scores(
            connection,
            tie_id=tie_id,
        )

        event_payload = json.dumps(
            {
                "previous_result": (
                    {
                        "advancing_team_id": previous_result.advancing_team_id,
                        "away_score": previous_result.away_score,
                        "home_score": previous_result.home_score,
                    }
                    if previous_result is not None
                    else None
                ),
                "result": {
                    "advancing_team_id": normalized_advancing_team_id,
                    "away_score": normalized_away_score,
                    "home_score": normalized_home_score,
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
                actor_user_id,
                (
                    "match.result_recorded"
                    if previous_result is None
                    else "match.result_corrected"
                ),
                "match",
                match_id,
                event_payload,
            ),
        )

        return MatchResultSaveResult(
            result=MatchResult(
                home_score=normalized_home_score,
                away_score=normalized_away_score,
                advancing_team_id=normalized_advancing_team_id,
            ),
            was_created=previous_result is None,
        )


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


def _get_active_contest_row(
    connection,
    *,
    telegram_chat_id: int,
    contest_id: int,
):
    row = connection.execute(
        """
        SELECT contests.id, contests.name, contests.slug, contests.created_at
        FROM contests
        JOIN chats ON chats.id = contests.chat_id
        WHERE contests.id = ?
          AND chats.telegram_chat_id = ?
          AND contests.is_active = 1
        """,
        (contest_id, telegram_chat_id),
    ).fetchone()
    if row is None:
        raise ContestNotFoundError("Конкурс не найден.")
    return row


def _get_match_row(
    connection,
    *,
    contest_id: int,
    match_id: int,
):
    return connection.execute(
        """
        SELECT
            matches.id,
            matches.tie_id,
            home_team.id AS home_team_id,
            home_team.name AS home_team_name,
            away_team.id AS away_team_id,
            away_team.name AS away_team_name,
            matches.starts_at_utc,
            matches.status,
            matches.home_score_final,
            matches.away_score_final,
            ties.advancing_team_id
        FROM matches
        JOIN ties
            ON ties.id = matches.tie_id
        JOIN stages
            ON stages.id = matches.stage_id
        JOIN competitions
            ON competitions.id = stages.competition_id
        JOIN teams AS home_team
            ON home_team.id = matches.home_team_id
        JOIN teams AS away_team
            ON away_team.id = matches.away_team_id
        WHERE competitions.contest_id = ?
            AND matches.id = ?
        """,
        (contest_id, match_id),
    ).fetchone()


def _get_or_create_first_stage(
    connection,
    *,
    competition_id: int,
) -> tuple[int, str, str]:
    row = connection.execute(
        """
        SELECT id, name, stage_type
        FROM stages
        WHERE competition_id = ?
        ORDER BY position ASC, id ASC
        LIMIT 1
        """,
        (competition_id,),
    ).fetchone()
    if row is not None:
        return (
            int(row["id"]),
            str(row["name"]),
            str(row["stage_type"]),
        )

    stage_id = int(
        connection.execute(
            """
            INSERT INTO stages (
                competition_id,
                name,
                position,
                stage_type
            )
            VALUES (?, ?, ?, ?)
            """,
            (competition_id, "Плей-офф", 1, "knockout"),
        ).lastrowid
    )
    return stage_id, "Плей-офф", "knockout"


def _get_next_tie_position(
    connection,
    *,
    stage_id: int,
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(position), 0) + 1 AS next_position
        FROM ties
        WHERE stage_id = ?
        """,
        (stage_id,),
    ).fetchone()

    if row is None:
        raise RuntimeError("Не удалось определить позицию противостояния.")

    return int(row["next_position"])


def _find_or_create_team(
    connection,
    *,
    team_name: str,
) -> tuple[int, bool]:
    normalized_team_name = team_name.casefold()
    rows = connection.execute(
        """
        SELECT id, name
        FROM teams
        ORDER BY id ASC
        """
    ).fetchall()

    for row in rows:
        if str(row["name"]).casefold() == normalized_team_name:
            return int(row["id"]), False

    team_id = int(
        connection.execute(
            """
            INSERT INTO teams (name)
            VALUES (?)
            """,
            (team_name,),
        ).lastrowid
    )
    return team_id, True


def _normalize_team_name(
    value: str,
    *,
    field_name: str,
) -> str:
    normalized_value = " ".join(value.split())
    if not normalized_value:
        raise ValueError(f"{field_name} обязательно.")
    if len(normalized_value) > 80:
        raise ValueError(f"{field_name} не должно быть длиннее 80 символов.")
    return normalized_value


def _normalize_starts_at_utc(value: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError("Укажите дату и время начала матча.")

    try:
        parsed_value = datetime.fromisoformat(normalized_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Некорректная дата и время начала матча.") from error

    if parsed_value.tzinfo is None or parsed_value.utcoffset() is None:
        raise ValueError("Дата и время начала матча должны содержать часовой пояс.")

    return (
        parsed_value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_match_idempotency_key(value: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError("Не передан ключ создания матча.")
    if len(normalized_value) > 128:
        raise ValueError("Некорректный ключ создания матча.")
    return normalized_value


def _build_match_request_fingerprint(
    *,
    home_team_name: str,
    away_team_name: str,
    starts_at_utc: str,
) -> str:
    payload = json.dumps(
        {
            "away_team_name": away_team_name,
            "home_team_name": home_team_name,
            "starts_at_utc": starts_at_utc,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _match_result_from_row(row) -> MatchResult | None:
    if (
        "home_score_final" not in row.keys()
        or "away_score_final" not in row.keys()
        or "advancing_team_id" not in row.keys()
        or row["home_score_final"] is None
        or row["away_score_final"] is None
        or row["advancing_team_id"] is None
    ):
        return None

    return MatchResult(
        home_score=int(row["home_score_final"]),
        away_score=int(row["away_score_final"]),
        advancing_team_id=int(row["advancing_team_id"]),
    )


def _match_summary_from_row(row) -> MatchSummary:
    result = _match_result_from_row(row)
    prediction = None

    if (
        "predicted_home_score" in row.keys()
        and "predicted_away_score" in row.keys()
        and "predicted_advancing_team_id" in row.keys()
        and row["predicted_home_score"] is not None
        and row["predicted_away_score"] is not None
        and row["predicted_advancing_team_id"] is not None
    ):
        prediction = MatchPrediction(
            home_score=int(row["predicted_home_score"]),
            away_score=int(row["predicted_away_score"]),
            advancing_team_id=int(row["predicted_advancing_team_id"]),
        )

    prediction_score = _match_prediction_score_from_row(
        row,
        result=result,
        prediction=prediction,
    )

    return MatchSummary(
        id=int(row["id"]),
        tie_id=int(row["tie_id"]),
        home_team_id=int(row["home_team_id"]),
        home_team_name=str(row["home_team_name"]),
        away_team_id=int(row["away_team_id"]),
        away_team_name=str(row["away_team_name"]),
        starts_at_utc=str(row["starts_at_utc"]),
        status=str(row["status"]),
        result=result,
        prediction=prediction,
        prediction_score=prediction_score,
    )


def _match_prediction_score_from_row(
    row,
    *,
    result: MatchResult | None,
    prediction: MatchPrediction | None,
) -> MatchPredictionScore | None:
    if result is None or prediction is None:
        return None

    awards: list[PredictionScoreAward] = []

    if (
        "match_score_type" in row.keys()
        and "match_score_points" in row.keys()
        and row["match_score_type"] is not None
        and row["match_score_points"] is not None
    ):
        awards.append(
            PredictionScoreAward(
                score_type=str(row["match_score_type"]),
                points=int(row["match_score_points"]),
            )
        )

    if (
        "advancing_team_points" in row.keys()
        and row["advancing_team_points"] is not None
    ):
        awards.append(
            PredictionScoreAward(
                score_type="advancing_team",
                points=int(row["advancing_team_points"]),
            )
        )

    return MatchPredictionScore(
        total_points=sum(award.points for award in awards),
        awards=tuple(awards),
    )


def _normalize_prediction_score(
    value: int,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} должен быть целым числом.")
    if value < 0:
        raise ValueError(f"{field_name} не может быть отрицательным.")
    return value


def _normalize_match_result_score(
    value: int,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} должен быть целым числом.")
    if value < 0:
        raise ValueError(f"{field_name} не может быть отрицательным.")
    return value


def _normalize_advancing_team_id(
    value: int,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} должен быть идентификатором команды.")

    if value <= 0:
        raise ValueError(f"{field_name} должен быть идентификатором команды.")

    return value


def _validate_advancing_team_for_match(
    match_row,
    *,
    advancing_team_id: int,
    home_score: int,
    away_score: int,
    field_name: str,
) -> None:
    home_team_id = int(match_row["home_team_id"])
    away_team_id = int(match_row["away_team_id"])

    if advancing_team_id not in {home_team_id, away_team_id}:
        raise ValueError(f"{field_name} должен быть одной из команд матча.")

    if home_score == away_score:
        return

    expected_advancing_team_id = (
        home_team_id if home_score > away_score else away_team_id
    )

    if advancing_team_id != expected_advancing_team_id:
        raise ValueError(f"{field_name} должен совпадать с победителем по счёту.")


def _resolve_now_utc(now_utc: datetime | None) -> datetime:
    if now_utc is None:
        return datetime.now(timezone.utc)

    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")

    return now_utc.astimezone(timezone.utc)


def _is_prediction_open(
    match_row,
    *,
    now_utc: datetime,
) -> bool:
    if str(match_row["status"]) != "scheduled":
        return False

    try:
        starts_at_utc = datetime.fromisoformat(
            str(match_row["starts_at_utc"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise RuntimeError("У матча сохранена некорректная дата начала.") from error

    if starts_at_utc.tzinfo is None or starts_at_utc.utcoffset() is None:
        raise RuntimeError("У матча сохранена дата начала без часового пояса.")

    return starts_at_utc.astimezone(timezone.utc) > now_utc


def _is_match_result_available(
    match_row,
    *,
    now_utc: datetime,
) -> bool:
    status = str(match_row["status"])

    if status == "finished":
        return True

    if status not in {"scheduled", "started"}:
        return False

    try:
        starts_at_utc = datetime.fromisoformat(
            str(match_row["starts_at_utc"]).replace("Z", "+00:00")
        )
    except ValueError as error:
        raise RuntimeError("У матча сохранена некорректная дата начала.") from error

    if starts_at_utc.tzinfo is None or starts_at_utc.utcoffset() is None:
        raise RuntimeError("У матча сохранена дата начала без часового пояса.")

    return starts_at_utc.astimezone(timezone.utc) <= now_utc


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

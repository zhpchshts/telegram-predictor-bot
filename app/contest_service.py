from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
from typing import Literal

from app.audit_service import (
    AuditActor,
    AuditEntityType,
    AuditEventType,
    record_audit_event,
)
from app.database import database_connection
from app.match_lifecycle import start_due_matches
from app.publication_outbox import (
    create_contest_completed_publication,
    create_or_revise_champion_publication,
    create_or_revise_champion_predictions_publication,
    create_or_revise_match_result_publication,
    handle_match_publication_deletion,
    revise_champion_publication_for_related_change,
    transition_contest_publications_for_master_switch,
)
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


class ContestCompletedError(ValueError):
    """Raised when a write is attempted for a completed contest."""


class ContestCompletionUnavailableError(ValueError):
    """Raised when a contest cannot be completed yet."""


class MatchCreationConflictError(ValueError):
    """Raised when an idempotency key is reused with different request data."""


class MatchNotFoundError(ValueError):
    """Raised when a match is unavailable in the current contest."""


class PredictionUnavailableError(ValueError):
    """Raised when a prediction can no longer be changed."""


class MatchResultUnavailableError(ValueError):
    """Raised when a result cannot be saved for the match."""


class ChampionUnavailableError(ValueError):
    """Raised when the tournament champion cannot be saved yet."""


class ChampionPredictionSettingsLockedError(ValueError):
    """Raised when champion prediction settings are locked by a saved result."""


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
class TeamSummary:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class ChampionPredictionDetails:
    is_enabled: bool
    deadline_at: str | None
    points: int
    candidates: tuple[TeamSummary, ...]
    prediction: TeamSummary | None
    actual_champion: TeamSummary | None
    is_open: bool
    is_tournament_completed: bool
    awarded_points: int | None


@dataclass(frozen=True, slots=True)
class MatchPredictionPublicationSettings:
    is_enabled: bool


@dataclass(frozen=True, slots=True)
class ChampionPredictionHistory:
    prediction: TeamSummary
    actual_champion: TeamSummary | None
    awarded_points: int | None


@dataclass(frozen=True, slots=True)
class LeaderboardTiebreakMetrics:
    exact_score_count: int
    goal_difference_count: int
    outcome_count: int
    drawn_advancing_team_count: int
    correct_champion_count: int


LeaderboardTiebreakReason = Literal[
    "exact_score",
    "goal_difference",
    "outcome",
    "drawn_advancing_team",
    "champion",
    "draw",
]

LEADERBOARD_SPORTING_TIEBREAKS: tuple[tuple[LeaderboardTiebreakReason, str], ...] = (
    ("exact_score", "exact_score_count"),
    ("goal_difference", "goal_difference_count"),
    ("outcome", "outcome_count"),
    ("drawn_advancing_team", "drawn_advancing_team_count"),
    ("champion", "correct_champion_count"),
)


@dataclass(frozen=True, slots=True)
class ContestLeaderboardEntry:
    place: int
    participant_name: str
    total_points: int
    match_predictions_count: int
    champion_prediction_count: int
    total_matches_count: int
    tiebreak_metrics: LeaderboardTiebreakMetrics
    prediction_history: tuple[MatchSummary, ...] = ()
    champion_prediction_history: ChampionPredictionHistory | None = None


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
    is_active: bool
    match_prediction_publication: MatchPredictionPublicationSettings
    champion_prediction: ChampionPredictionDetails
    leaderboard: tuple[ContestLeaderboardEntry, ...]
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


def get_completed_contests(
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
              AND contests.is_active = 0
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
    now_utc: datetime | None = None,
) -> ContestDetails:
    resolved_now_utc = _resolve_now_utc(now_utc)

    with database_connection(database_path) as connection:
        contest_row = _get_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )

    if bool(contest_row["is_active"]):
        start_due_matches(
            database_path=database_path,
            contest_id=contest_id,
            now_utc=resolved_now_utc,
        )

    with database_connection(database_path) as connection:
        contest_row = _get_contest_row(
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

        champion_prediction = _get_champion_prediction_details(
            connection,
            contest_id=contest_id,
            telegram_user_id=telegram_user_id,
            now_utc=resolved_now_utc,
        )
        match_prediction_publication = _get_match_prediction_publication_settings(
            connection,
            contest_id=contest_id,
        )

        leaderboard_rows = connection.execute(
            """
            WITH contest_participants AS (
                SELECT match_predictions.user_id
                FROM match_predictions
                JOIN matches
                    ON matches.id = match_predictions.match_id
                JOIN stages
                    ON stages.id = matches.stage_id
                JOIN competitions
                    ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?

                UNION

                SELECT tie_predictions.user_id
                FROM tie_predictions
                JOIN ties
                    ON ties.id = tie_predictions.tie_id
                JOIN stages
                    ON stages.id = ties.stage_id
                JOIN competitions
                    ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?

                UNION

                SELECT champion_predictions.user_id
                FROM champion_predictions
                WHERE champion_predictions.contest_id = ?
            ),
            score_points AS (
                SELECT
                    match_predictions.user_id,
                    match_prediction_scores.points
                FROM match_prediction_scores
                JOIN match_predictions
                    ON match_predictions.id =
                    match_prediction_scores.match_prediction_id
                JOIN matches
                    ON matches.id = match_predictions.match_id
                JOIN stages
                    ON stages.id = matches.stage_id
                JOIN competitions
                    ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?

                UNION ALL

                SELECT
                    tie_predictions.user_id,
                    tie_prediction_scores.points
                FROM tie_prediction_scores
                JOIN tie_predictions
                    ON tie_predictions.id =
                    tie_prediction_scores.tie_prediction_id
                JOIN ties
                    ON ties.id = tie_predictions.tie_id
                JOIN stages
                    ON stages.id = ties.stage_id
                JOIN competitions
                    ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?

                UNION ALL

                SELECT
                    champion_predictions.user_id,
                    contests.champion_prediction_points AS points
                FROM champion_predictions
                JOIN contests
                    ON contests.id = champion_predictions.contest_id
                WHERE champion_predictions.contest_id = ?
                    AND contests.champion_prediction_enabled = 1
                    AND contests.champion_team_id IS NOT NULL
                    AND champion_predictions.predicted_team_id =
                    contests.champion_team_id
            ),
            score_totals AS (
                SELECT
                    score_points.user_id,
                    SUM(score_points.points) AS total_points
                FROM score_points
                GROUP BY score_points.user_id
            ),
            match_score_tiebreaks AS (
                SELECT
                    match_predictions.user_id,
                    SUM(
                        CASE
                            WHEN match_prediction_scores.score_type =
                                'exact_score'
                            THEN 1
                            ELSE 0
                        END
                    ) AS exact_score_count,
                    SUM(
                        CASE
                            WHEN match_prediction_scores.score_type =
                                'goal_difference'
                            THEN 1
                            ELSE 0
                        END
                    ) AS goal_difference_count,
                    SUM(
                        CASE
                            WHEN match_prediction_scores.score_type = 'outcome'
                            THEN 1
                            ELSE 0
                        END
                    ) AS outcome_count
                FROM match_prediction_scores
                JOIN match_predictions
                    ON match_predictions.id =
                    match_prediction_scores.match_prediction_id
                JOIN matches
                    ON matches.id = match_predictions.match_id
                JOIN stages
                    ON stages.id = matches.stage_id
                JOIN competitions
                    ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?
                GROUP BY match_predictions.user_id
            ),
            drawn_advancing_team_tiebreaks AS (
                SELECT
                    tie_predictions.user_id,
                    COUNT(*) AS drawn_advancing_team_count
                FROM tie_prediction_scores
                JOIN tie_predictions
                    ON tie_predictions.id =
                    tie_prediction_scores.tie_prediction_id
                WHERE EXISTS (
                    SELECT 1
                    FROM matches
                    JOIN stages
                        ON stages.id = matches.stage_id
                    JOIN competitions
                        ON competitions.id = stages.competition_id
                    JOIN match_predictions
                        ON match_predictions.match_id = matches.id
                        AND match_predictions.user_id = tie_predictions.user_id
                    WHERE matches.tie_id = tie_predictions.tie_id
                        AND competitions.contest_id = ?
                        AND match_predictions.predicted_home_score =
                            match_predictions.predicted_away_score
                )
                GROUP BY tie_predictions.user_id
            ),
            champion_tiebreaks AS (
                SELECT
                    champion_predictions.user_id,
                    1 AS correct_champion_count
                FROM champion_predictions
                JOIN contests
                    ON contests.id = champion_predictions.contest_id
                WHERE champion_predictions.contest_id = ?
                    AND contests.champion_prediction_enabled = 1
                    AND contests.champion_team_id IS NOT NULL
                    AND champion_predictions.predicted_team_id =
                    contests.champion_team_id
            )
            SELECT
                users.id AS user_id,
                users.telegram_user_id,
                users.first_name,
                users.last_name,
                COALESCE(score_totals.total_points, 0) AS total_points,
                COALESCE(
                    match_score_tiebreaks.exact_score_count,
                    0
                ) AS exact_score_count,
                COALESCE(
                    match_score_tiebreaks.goal_difference_count,
                    0
                ) AS goal_difference_count,
                COALESCE(
                    match_score_tiebreaks.outcome_count,
                    0
                ) AS outcome_count,
                COALESCE(
                    drawn_advancing_team_tiebreaks.drawn_advancing_team_count,
                    0
                ) AS drawn_advancing_team_count,
                COALESCE(
                    champion_tiebreaks.correct_champion_count,
                    0
                ) AS correct_champion_count,
                (
                    SELECT COUNT(*)
                    FROM match_predictions
                    JOIN matches
                        ON matches.id = match_predictions.match_id
                    JOIN stages
                        ON stages.id = matches.stage_id
                    JOIN competitions
                        ON competitions.id = stages.competition_id
                    WHERE competitions.contest_id = ?
                        AND matches.status != 'cancelled'
                        AND match_predictions.user_id = users.id
                ) AS match_predictions_count,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM champion_predictions
                        JOIN contests
                            ON contests.id =
                            champion_predictions.contest_id
                        WHERE champion_predictions.contest_id = ?
                            AND champion_predictions.user_id = users.id
                            AND contests.champion_prediction_enabled = 1
                    ) THEN 1
                    ELSE 0
                END AS champion_prediction_count,
                (
                    SELECT COUNT(*)
                    FROM matches
                    JOIN stages
                        ON stages.id = matches.stage_id
                    JOIN competitions
                        ON competitions.id = stages.competition_id
                    WHERE competitions.contest_id = ?
                        AND matches.status != 'cancelled'
                ) AS total_matches_count
            FROM contest_participants
            JOIN users
                ON users.id = contest_participants.user_id
            LEFT JOIN score_totals
                ON score_totals.user_id = contest_participants.user_id
            LEFT JOIN match_score_tiebreaks
                ON match_score_tiebreaks.user_id = contest_participants.user_id
            LEFT JOIN drawn_advancing_team_tiebreaks
                ON drawn_advancing_team_tiebreaks.user_id =
                contest_participants.user_id
            LEFT JOIN champion_tiebreaks
                ON champion_tiebreaks.user_id = contest_participants.user_id
            ORDER BY users.id ASC
            """,
            (
                contest_id,
                contest_id,
                contest_id,
                contest_id,
                contest_id,
                contest_id,
                contest_id,
                contest_id,
                contest_id,
                contest_id,
                contest_id,
                contest_id,
            ),
        ).fetchall()

        leaderboard_prediction_rows = connection.execute(
            """
            SELECT
                match_predictions.user_id,
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
            FROM match_predictions
            JOIN matches
            ON matches.id = match_predictions.match_id
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
            LEFT JOIN tie_predictions
            ON tie_predictions.tie_id = matches.tie_id
            AND tie_predictions.user_id = match_predictions.user_id
            LEFT JOIN match_prediction_scores
            ON match_prediction_scores.match_prediction_id = match_predictions.id
            LEFT JOIN tie_prediction_scores
            ON tie_prediction_scores.tie_prediction_id = tie_predictions.id
            WHERE competitions.contest_id = ?
            AND matches.status != 'cancelled'
            ORDER BY
                match_predictions.user_id ASC,
                matches.starts_at_utc DESC,
                matches.id DESC
            """,
            (contest_id,),
        ).fetchall()

        leaderboard_champion_prediction_rows = connection.execute(
            """
            SELECT
                champion_predictions.user_id,
                predicted_team.id AS predicted_team_id,
                predicted_team.name AS predicted_team_name,
                actual_champion.id AS actual_champion_id,
                actual_champion.name AS actual_champion_name,
                CASE
                    WHEN contests.champion_team_id IS NULL THEN NULL
                    WHEN champion_predictions.predicted_team_id =
                        contests.champion_team_id
                    THEN contests.champion_prediction_points
                    ELSE 0
                END AS awarded_points
            FROM champion_predictions
            JOIN contests
            ON contests.id = champion_predictions.contest_id
            JOIN teams AS predicted_team
            ON predicted_team.id = champion_predictions.predicted_team_id
            LEFT JOIN teams AS actual_champion
            ON actual_champion.id = contests.champion_team_id
            WHERE champion_predictions.contest_id = ?
            AND contests.champion_prediction_enabled = 1
            """,
            (contest_id,),
        ).fetchall()

        return ContestDetails(
            id=int(contest_row["id"]),
            name=str(contest_row["name"]),
            slug=str(contest_row["slug"]),
            created_at=str(contest_row["created_at"]),
            is_active=bool(contest_row["is_active"]),
            match_prediction_publication=match_prediction_publication,
            champion_prediction=champion_prediction,
            leaderboard=_contest_leaderboard_from_rows(
                leaderboard_rows,
                contest_slug=str(contest_row["slug"]),
                prediction_history_by_user=_leaderboard_prediction_history_by_user(
                    leaderboard_prediction_rows,
                    now_utc=resolved_now_utc,
                ),
                champion_prediction_history_by_user=(
                    _leaderboard_champion_prediction_history_by_user(
                        leaderboard_champion_prediction_rows,
                    )
                    if not champion_prediction.is_open
                    else {}
                ),
            ),
            matches=tuple(_match_summary_from_row(row) for row in match_rows),
        )


def complete_contest(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> None:
    resolved_now_utc = _resolve_now_utc(now_utc)

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )

        incomplete_match = connection.execute(
            """
            SELECT matches.id
            FROM matches
            JOIN ties ON ties.id = matches.tie_id
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?
              AND (
                  matches.status != 'finished'
                  OR matches.home_score_final IS NULL
                  OR matches.away_score_final IS NULL
                  OR ties.advancing_team_id IS NULL
              )
            LIMIT 1
            """,
            (contest_id,),
        ).fetchone()

        if incomplete_match is not None:
            raise ContestCompletionUnavailableError(
                "Сначала внесите финальные результаты всех матчей."
            )

        if bool(contest_row["champion_prediction_enabled"]):
            champion_deadline_at = contest_row["champion_prediction_deadline_at"]
            if champion_deadline_at is None:
                raise ContestCompletionUnavailableError(
                    "Сначала укажите дедлайн прогноза на чемпиона."
                )
            if _is_champion_prediction_open(
                str(champion_deadline_at),
                now_utc=resolved_now_utc,
            ):
                raise ContestCompletionUnavailableError(
                    "Конкурс можно завершить после закрытия прогнозов на чемпиона."
                )

        if (
            bool(contest_row["champion_prediction_enabled"])
            and contest_row["champion_team_id"] is None
        ):
            raise ContestCompletionUnavailableError(
                "Сначала укажите фактического чемпиона."
            )

        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

        completion_update = connection.execute(
            """
            UPDATE contests
            SET is_active = 0
            WHERE id = ?
              AND is_active = 1
            """,
            (contest_id,),
        )

        if completion_update.rowcount != 1:
            raise ContestCompletedError(
                "Конкурс завершён. Изменения в нём больше недоступны."
            )
        completed_contest_row = _get_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=AuditEventType.CONTEST_FINISHED,
            entity_type=AuditEntityType.CONTEST,
            entity_id=contest_id,
            contest_id=contest_id,
            before_state=_contest_snapshot(contest_row),
            after_state=_contest_snapshot(completed_contest_row),
        )

        event_payload = json.dumps(
            {"is_active": False},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        completion_event = connection.execute(
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
                "contest.completed",
                "contest",
                contest_id,
                event_payload,
            ),
        )
        if completion_event.lastrowid is None:
            raise RuntimeError("Не удалось записать событие завершения конкурса.")
        create_contest_completed_publication(
            connection,
            contest_id=contest_id,
            event_id=int(completion_event.lastrowid),
        )


def delete_contest(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    audit_actor: AuditActor,
) -> None:
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest_row = _get_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )

        if not bool(contest_row["is_active"]):
            raise ContestCompletedError("Завершённый конкурс удалить нельзя.")

        deletion = connection.execute(
            """
            DELETE FROM contests
            WHERE id = ?
            AND is_active = 1
            """,
            (contest_id,),
        )

        if deletion.rowcount != 1:
            raise ContestCompletedError(
                "Конкурс завершён. Изменения в нём больше недоступны."
            )
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=AuditEventType.CONTEST_DELETED,
            entity_type=AuditEntityType.CONTEST,
            entity_id=contest_id,
            contest_id=contest_id,
            before_state=_contest_snapshot(contest_row),
            after_state=None,
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
    audit_actor: AuditActor,
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
        connection.execute("BEGIN IMMEDIATE")
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
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=AuditEventType.MATCH_CREATED,
            entity_type=AuditEntityType.MATCH,
            entity_id=match_id,
            contest_id=contest_id,
            before_state=None,
            after_state=_match_snapshot(match_row),
        )

    if match_row is None:
        raise RuntimeError("Не удалось создать матч.")

    return MatchCreationResult(
        match=_match_summary_from_row(match_row),
        was_created=True,
    )


def delete_match(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    match_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    audit_actor: AuditActor,
) -> None:
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )

        match_row = connection.execute(
            """
            SELECT
                matches.id,
                matches.tie_id,
                matches.starts_at_utc,
                matches.status,
                home_team.id AS home_team_id,
                home_team.name AS home_team_name,
                away_team.id AS away_team_id,
                away_team.name AS away_team_name
            FROM matches
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

        if match_row is None:
            raise MatchNotFoundError("Матч не найден.")

        tie_id = int(match_row["tie_id"]) if match_row["tie_id"] is not None else None

        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

        event_payload = json.dumps(
            {
                "away_team": {
                    "id": int(match_row["away_team_id"]),
                    "name": str(match_row["away_team_name"]),
                },
                "home_team": {
                    "id": int(match_row["home_team_id"]),
                    "name": str(match_row["home_team_name"]),
                },
                "starts_at_utc": str(match_row["starts_at_utc"]),
                "status": str(match_row["status"]),
                "tie_id": tie_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        deletion_event = connection.execute(
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
                "match.deleted",
                "match",
                match_id,
                event_payload,
            ),
        )
        if deletion_event.lastrowid is None:
            raise RuntimeError("Не удалось записать событие удаления матча.")
        handle_match_publication_deletion(
            connection,
            contest_id=contest_id,
            match_id=match_id,
            event_id=int(deletion_event.lastrowid),
        )

        deleted_match = connection.execute(
            """
            DELETE FROM matches
            WHERE id = ?
            """,
            (match_id,),
        )

        if deleted_match.rowcount != 1:
            raise MatchNotFoundError("Матч не найден.")

        if tie_id is not None:
            remaining_match = connection.execute(
                """
                SELECT 1
                FROM matches
                WHERE tie_id = ?
                LIMIT 1
                """,
                (tie_id,),
            ).fetchone()

            if remaining_match is None:
                connection.execute(
                    """
                    DELETE FROM ties
                    WHERE id = ?
                    """,
                    (tie_id,),
                )
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=AuditEventType.MATCH_DELETED,
            entity_type=AuditEntityType.MATCH,
            entity_id=match_id,
            contest_id=contest_id,
            before_state=_match_snapshot(match_row),
            after_state=None,
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
        connection.execute("BEGIN IMMEDIATE")
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
    audit_actor: AuditActor,
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
        connection.execute("BEGIN IMMEDIATE")
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
        if event_cursor.lastrowid is None:
            raise RuntimeError("Не удалось записать событие результата матча.")
        create_or_revise_match_result_publication(
            connection,
            contest_id=contest_id,
            match_id=match_id,
            event_id=int(event_cursor.lastrowid),
            was_created=previous_result is None,
            now_utc=resolved_now_utc,
        )
        saved_match_row = _get_match_row(
            connection,
            contest_id=contest_id,
            match_id=match_id,
        )
        if saved_match_row is None:
            raise RuntimeError("Не удалось повторно прочитать результат матча.")
        previous_result_state = _match_result_snapshot(previous_result)
        saved_result_state = _match_result_snapshot(
            MatchResult(
                home_score=normalized_home_score,
                away_score=normalized_away_score,
                advancing_team_id=normalized_advancing_team_id,
            )
        )
        if previous_result_state != saved_result_state:
            _record_audit(
                connection,
                audit_actor=audit_actor,
                telegram_chat_id=telegram_chat_id,
                event_type=(
                    AuditEventType.MATCH_RESULT_SET
                    if previous_result is None
                    else AuditEventType.MATCH_RESULT_CHANGED
                ),
                entity_type=AuditEntityType.MATCH,
                entity_id=match_id,
                contest_id=contest_id,
                before_state=_match_snapshot(match_row),
                after_state=_match_snapshot(saved_match_row),
            )

        return MatchResultSaveResult(
            result=MatchResult(
                home_score=normalized_home_score,
                away_score=normalized_away_score,
                advancing_team_id=normalized_advancing_team_id,
            ),
            was_created=previous_result is None,
        )


def save_match_prediction_publication_settings(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    enabled: bool,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> None:
    if not isinstance(enabled, bool):
        raise ValueError(
            "Настройка публикации прогнозов должна быть включена или выключена."
        )

    resolved_now_utc = _resolve_now_utc(now_utc)

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest_row = _get_active_contest_row(
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
        was_enabled = bool(contest_row["match_prediction_publication_enabled"])
        if enabled == was_enabled:
            return

        enabled_at_utc = (
            _serialize_datetime_utc(resolved_now_utc)
            if enabled and not was_enabled
            else None
        )

        connection.execute(
            """
            UPDATE contests
            SET
                match_prediction_publication_enabled = ?,
                match_prediction_publication_enabled_at = CASE
                    WHEN ? = 1
                        AND match_prediction_publication_enabled = 0
                    THEN ?
                    WHEN ? = 0 THEN NULL
                    ELSE match_prediction_publication_enabled_at
                END
            WHERE id = ?
            """,
            (
                int(enabled),
                int(enabled),
                enabled_at_utc,
                int(enabled),
                contest_id,
            ),
        )

        settings_event_id = _write_match_prediction_publication_event(
            connection,
            contest_id=contest_id,
            actor_user_id=actor_user_id,
            enabled=enabled,
            previous_enabled=was_enabled,
        )
        transition_contest_publications_for_master_switch(
            connection,
            contest_id=contest_id,
            enabled=enabled,
            event_id=settings_event_id,
            now_utc=resolved_now_utc,
        )
        updated_contest_row = _get_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=AuditEventType.CONTEST_UPDATED,
            entity_type=AuditEntityType.CONTEST,
            entity_id=contest_id,
            contest_id=contest_id,
            before_state=_contest_snapshot(contest_row),
            after_state=_contest_snapshot(updated_contest_row),
            metadata={"changed_section": "match_prediction_publication"},
        )


def save_champion_prediction_settings(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    enabled: bool,
    deadline_at: str | None,
    points: int,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> None:
    resolved_now_utc = _resolve_now_utc(now_utc)

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )

        previous_row = _get_champion_prediction_configuration_row(
            connection,
            contest_id=contest_id,
        )
        if previous_row is None:
            raise RuntimeError("Не удалось найти настройки прогноза чемпиона.")

        if previous_row["champion_team_id"] is not None:
            raise ChampionPredictionSettingsLockedError(
                "Настройки прогноза на чемпиона нельзя изменить после указания "
                "фактического чемпиона."
            )

        normalized_enabled = _normalize_champion_prediction_enabled(enabled)
        normalized_points = _normalize_champion_prediction_points(points)

        if normalized_enabled:
            if deadline_at is None:
                raise ValueError("Укажите, когда прогноз на чемпиона закрывается.")

            normalized_deadline_at = _normalize_champion_prediction_deadline_at(
                deadline_at
            )
        else:
            normalized_deadline_at = None

        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

        settings_update = connection.execute(
            """
            UPDATE contests
            SET
                champion_prediction_enabled = ?,
                champion_prediction_deadline_at = ?,
                champion_prediction_points = ?,
                champion_team_id = CASE
                    WHEN ? = 1 THEN champion_team_id
                    ELSE NULL
                END
            WHERE id = ?
              AND champion_team_id IS NULL
            """,
            (
                int(normalized_enabled),
                normalized_deadline_at,
                normalized_points,
                int(normalized_enabled),
                contest_id,
            ),
        )
        if settings_update.rowcount != 1:
            raise ChampionPredictionSettingsLockedError(
                "Настройки прогноза на чемпиона нельзя изменить после указания "
                "фактического чемпиона."
            )

        settings_event_id = _write_champion_event(
            connection,
            contest_id=contest_id,
            actor_user_id=actor_user_id,
            event_type="contest.champion_prediction_settings_updated",
            payload={
                "enabled": normalized_enabled,
                "deadline_at": normalized_deadline_at,
                "points": normalized_points,
                "previous_enabled": bool(previous_row["champion_prediction_enabled"]),
                "previous_deadline_at": previous_row["champion_prediction_deadline_at"],
                "previous_points": int(previous_row["champion_prediction_points"]),
            },
        )
        revise_champion_publication_for_related_change(
            connection,
            contest_id=contest_id,
            event_id=settings_event_id,
            now_utc=resolved_now_utc,
        )
        if bool(previous_row["match_prediction_publication_enabled"]):
            create_or_revise_champion_predictions_publication(
                connection,
                contest_id=contest_id,
                event_id=settings_event_id,
                now_utc=resolved_now_utc,
            )
        updated_contest_row = _get_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        if _contest_snapshot(contest_row) != _contest_snapshot(updated_contest_row):
            _record_audit(
                connection,
                audit_actor=audit_actor,
                telegram_chat_id=telegram_chat_id,
                event_type=AuditEventType.CONTEST_UPDATED,
                entity_type=AuditEntityType.CONTEST,
                entity_id=contest_id,
                contest_id=contest_id,
                before_state=_contest_snapshot(contest_row),
                after_state=_contest_snapshot(updated_contest_row),
                metadata={"changed_section": "champion_prediction"},
            )


def save_champion_prediction(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    predicted_team_id: int,
    now_utc: datetime | None = None,
) -> TeamSummary:
    normalized_team_id = _normalize_champion_team_id(
        predicted_team_id,
        field_name="Прогноз на чемпиона",
    )
    resolved_now_utc = _resolve_now_utc(now_utc)

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )

        configuration_row = _get_champion_prediction_configuration_row(
            connection,
            contest_id=contest_id,
        )
        if configuration_row is None:
            raise RuntimeError("Не удалось найти настройки прогноза чемпиона.")

        if not bool(configuration_row["champion_prediction_enabled"]):
            raise PredictionUnavailableError(
                "Прогноз на чемпиона в этом конкурсе выключен."
            )

        deadline_at = configuration_row["champion_prediction_deadline_at"]
        if deadline_at is None:
            raise PredictionUnavailableError(
                "Для прогноза на чемпиона не задан дедлайн."
            )

        if not _is_champion_prediction_open(
            str(deadline_at),
            now_utc=resolved_now_utc,
        ):
            raise PredictionUnavailableError("Прогноз на чемпиона уже закрыт.")

        team_row = _get_champion_candidate_team_row(
            connection,
            contest_id=contest_id,
            team_id=normalized_team_id,
        )
        if team_row is None:
            raise ValueError("Выбранная команда не участвует в этом конкурсе.")

        user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

        existing_prediction = connection.execute(
            """
            SELECT id
            FROM champion_predictions
            WHERE contest_id = ?
                AND user_id = ?
            """,
            (contest_id, user_id),
        ).fetchone()

        connection.execute(
            """
            INSERT INTO champion_predictions (
                contest_id,
                user_id,
                predicted_team_id
            )
            VALUES (?, ?, ?)
            ON CONFLICT(contest_id, user_id) DO UPDATE SET
                predicted_team_id = excluded.predicted_team_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (contest_id, user_id, normalized_team_id),
        )

        prediction_event_id = _write_champion_event(
            connection,
            contest_id=contest_id,
            actor_user_id=user_id,
            event_type=(
                "champion_prediction.created"
                if existing_prediction is None
                else "champion_prediction.updated"
            ),
            payload={"predicted_team_id": normalized_team_id},
        )
        revise_champion_publication_for_related_change(
            connection,
            contest_id=contest_id,
            event_id=prediction_event_id,
            now_utc=resolved_now_utc,
        )
        if bool(configuration_row["match_prediction_publication_enabled"]):
            create_or_revise_champion_predictions_publication(
                connection,
                contest_id=contest_id,
                event_id=prediction_event_id,
                now_utc=resolved_now_utc,
            )

        return _team_summary_from_row(team_row)


def save_contest_champion(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    champion_team_id: int,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> TeamSummary:
    normalized_team_id = _normalize_champion_team_id(
        champion_team_id,
        field_name="Фактический чемпион",
    )

    resolved_now_utc = _resolve_now_utc(now_utc)

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )

        configuration_row = _get_champion_prediction_configuration_row(
            connection,
            contest_id=contest_id,
        )
        if configuration_row is None:
            raise RuntimeError("Не удалось найти настройки прогноза чемпиона.")

        if not bool(configuration_row["champion_prediction_enabled"]):
            raise ChampionUnavailableError("Сначала включите прогноз на чемпиона.")

        if not _is_contest_completed(connection, contest_id=contest_id):
            raise ChampionUnavailableError(
                "Чемпиона можно указать после завершения всех матчей конкурса."
            )

        deadline_at = configuration_row["champion_prediction_deadline_at"]
        if deadline_at is None:
            raise ChampionUnavailableError("Для прогноза на чемпиона не задан дедлайн.")
        if _is_champion_prediction_open(
            str(deadline_at),
            now_utc=resolved_now_utc,
        ):
            raise ChampionUnavailableError(
                "Фактического чемпиона можно указать после закрытия прогнозов "
                "на чемпиона."
            )

        team_row = _get_champion_candidate_team_row(
            connection,
            contest_id=contest_id,
            team_id=normalized_team_id,
        )
        if team_row is None:
            raise ValueError("Выбранная команда не участвует в этом конкурсе.")

        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )

        previous_champion_team_id = configuration_row["champion_team_id"]
        if previous_champion_team_id == normalized_team_id:
            return _team_summary_from_row(team_row)

        connection.execute(
            """
            UPDATE contests
            SET champion_team_id = ?
            WHERE id = ?
            """,
            (normalized_team_id, contest_id),
        )

        champion_event_id = _write_champion_event(
            connection,
            contest_id=contest_id,
            actor_user_id=actor_user_id,
            event_type=(
                "contest.champion_recorded"
                if previous_champion_team_id is None
                else "contest.champion_corrected"
            ),
            payload={
                "champion_team_id": normalized_team_id,
                "previous_champion_team_id": previous_champion_team_id,
            },
        )
        create_or_revise_champion_publication(
            connection,
            contest_id=contest_id,
            event_id=champion_event_id,
            was_created=previous_champion_team_id is None,
            now_utc=resolved_now_utc,
        )
        updated_contest_row = _get_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=(
                AuditEventType.CONTEST_CHAMPION_SET
                if previous_champion_team_id is None
                else AuditEventType.CONTEST_CHAMPION_CHANGED
            ),
            entity_type=AuditEntityType.CONTEST,
            entity_id=contest_id,
            contest_id=contest_id,
            before_state=_contest_snapshot(contest_row),
            after_state=_contest_snapshot(updated_contest_row),
        )

        return _team_summary_from_row(team_row)


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
    audit_actor: AuditActor,
) -> ContestCreationResult:
    normalized_contest_name = _normalize_contest_name(contest_name)
    normalized_idempotency_key = _normalize_idempotency_key(idempotency_key)
    request_fingerprint = _build_request_fingerprint(normalized_contest_name)

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
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
        audit_contest_row = _get_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=AuditEventType.CONTEST_CREATED,
            entity_type=AuditEntityType.CONTEST,
            entity_id=contest_id,
            contest_id=contest_id,
            before_state=None,
            after_state=_contest_snapshot(audit_contest_row),
        )

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


def _get_contest_row(
    connection,
    *,
    telegram_chat_id: int,
    contest_id: int,
):
    contest_row = connection.execute(
        """
        SELECT
            contests.id,
            contests.name,
            contests.slug,
            contests.created_at,
            contests.is_active,
            contests.champion_prediction_enabled,
            contests.champion_prediction_deadline_at,
            contests.champion_prediction_points,
            contests.champion_team_id,
            contests.match_prediction_publication_enabled,
            contests.match_prediction_publication_enabled_at
        FROM contests
        JOIN chats ON chats.id = contests.chat_id
        WHERE chats.telegram_chat_id = ?
          AND contests.id = ?
        """,
        (telegram_chat_id, contest_id),
    ).fetchone()

    if contest_row is None:
        raise ContestNotFoundError("Конкурс не найден.")

    return contest_row


def _record_audit(
    connection,
    *,
    audit_actor: AuditActor,
    telegram_chat_id: int,
    event_type: AuditEventType,
    entity_type: AuditEntityType,
    entity_id: int | None,
    contest_id: int | None,
    before_state: dict[str, object] | None,
    after_state: dict[str, object] | None,
    metadata: dict[str, object] | None = None,
) -> None:
    if audit_actor.telegram_chat_id != telegram_chat_id:
        raise ValueError("Audit actor chat does not match the administrative action.")
    record_audit_event(
        connection,
        actor=audit_actor,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        contest_id=contest_id,
        before_state=before_state,
        after_state=after_state,
        metadata=metadata,
    )


def _contest_snapshot(row) -> dict[str, object]:
    return {
        "champion_prediction_deadline_at": row["champion_prediction_deadline_at"],
        "champion_prediction_enabled": bool(row["champion_prediction_enabled"]),
        "champion_prediction_points": int(row["champion_prediction_points"]),
        "champion_team_id": (
            int(row["champion_team_id"])
            if row["champion_team_id"] is not None
            else None
        ),
        "created_at": str(row["created_at"]),
        "id": int(row["id"]),
        "is_active": bool(row["is_active"]),
        "match_prediction_publication_enabled": bool(
            row["match_prediction_publication_enabled"]
        ),
        "match_prediction_publication_enabled_at": row[
            "match_prediction_publication_enabled_at"
        ],
        "name": str(row["name"]),
        "slug": str(row["slug"]),
    }


def _match_snapshot(row) -> dict[str, object]:
    keys = set(row.keys())
    return {
        "advancing_team_id": (
            int(row["advancing_team_id"])
            if "advancing_team_id" in keys and row["advancing_team_id"] is not None
            else None
        ),
        "away_score": (
            int(row["away_score_final"])
            if "away_score_final" in keys and row["away_score_final"] is not None
            else None
        ),
        "away_team": {
            "id": int(row["away_team_id"]),
            "name": str(row["away_team_name"]),
        },
        "home_score": (
            int(row["home_score_final"])
            if "home_score_final" in keys and row["home_score_final"] is not None
            else None
        ),
        "home_team": {
            "id": int(row["home_team_id"]),
            "name": str(row["home_team_name"]),
        },
        "id": int(row["id"]),
        "starts_at_utc": str(row["starts_at_utc"]),
        "status": str(row["status"]),
        "tie_id": int(row["tie_id"]) if row["tie_id"] is not None else None,
    }


def _match_result_snapshot(result: MatchResult | None) -> dict[str, int] | None:
    if result is None:
        return None
    return {
        "advancing_team_id": result.advancing_team_id,
        "away_score": result.away_score,
        "home_score": result.home_score,
    }


def _get_active_contest_row(
    connection,
    *,
    telegram_chat_id: int,
    contest_id: int,
):
    contest_row = _get_contest_row(
        connection,
        telegram_chat_id=telegram_chat_id,
        contest_id=contest_id,
    )

    if not bool(contest_row["is_active"]):
        raise ContestCompletedError(
            "Конкурс завершён. Изменения в нём больше недоступны."
        )

    return contest_row


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


def _get_champion_prediction_details(
    connection,
    *,
    contest_id: int,
    telegram_user_id: int | None,
    now_utc: datetime,
) -> ChampionPredictionDetails:
    configuration_row = connection.execute(
        """
        SELECT
            contests.is_active,
            contests.champion_prediction_enabled,
            contests.champion_prediction_deadline_at,
            contests.champion_prediction_points,
            actual_team.id AS actual_champion_id,
            actual_team.name AS actual_champion_name,
            predicted_team.id AS predicted_team_id,
            predicted_team.name AS predicted_team_name
        FROM contests
        LEFT JOIN teams AS actual_team
            ON actual_team.id = contests.champion_team_id
        LEFT JOIN users AS prediction_user
            ON prediction_user.telegram_user_id = ?
        LEFT JOIN champion_predictions
            ON champion_predictions.contest_id = contests.id
            AND champion_predictions.user_id = prediction_user.id
        LEFT JOIN teams AS predicted_team
            ON predicted_team.id = champion_predictions.predicted_team_id
        WHERE contests.id = ?
        """,
        (telegram_user_id, contest_id),
    ).fetchone()

    if configuration_row is None:
        raise RuntimeError("Не удалось найти настройки прогноза чемпиона.")

    is_enabled = bool(configuration_row["champion_prediction_enabled"])
    contest_is_active = bool(configuration_row["is_active"])
    is_tournament_completed = _is_contest_completed(
        connection,
        contest_id=contest_id,
    )
    deadline_at = configuration_row["champion_prediction_deadline_at"]
    deadline_at_value = str(deadline_at) if deadline_at is not None else None

    actual_champion = (
        TeamSummary(
            id=int(configuration_row["actual_champion_id"]),
            name=str(configuration_row["actual_champion_name"]),
        )
        if configuration_row["actual_champion_id"] is not None
        else None
    )
    prediction = (
        TeamSummary(
            id=int(configuration_row["predicted_team_id"]),
            name=str(configuration_row["predicted_team_name"]),
        )
        if configuration_row["predicted_team_id"] is not None
        else None
    )

    awarded_points = None
    if is_enabled and actual_champion is not None and prediction is not None:
        awarded_points = (
            int(configuration_row["champion_prediction_points"])
            if actual_champion.id == prediction.id
            else 0
        )

    return ChampionPredictionDetails(
        is_enabled=is_enabled,
        deadline_at=deadline_at_value,
        points=int(configuration_row["champion_prediction_points"]),
        candidates=tuple(
            _team_summary_from_row(row)
            for row in _get_champion_candidate_team_rows(
                connection,
                contest_id=contest_id,
            )
        ),
        prediction=prediction,
        actual_champion=actual_champion,
        is_open=(
            is_enabled
            and contest_is_active
            and deadline_at_value is not None
            and _is_champion_prediction_open(
                deadline_at_value,
                now_utc=now_utc,
            )
        ),
        is_tournament_completed=is_tournament_completed,
        awarded_points=awarded_points,
    )


def _get_match_prediction_publication_settings(
    connection,
    *,
    contest_id: int,
) -> MatchPredictionPublicationSettings:
    row = connection.execute(
        """
        SELECT match_prediction_publication_enabled
        FROM contests
        WHERE id = ?
        """,
        (contest_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Не удалось найти настройки публикации прогнозов.")

    return MatchPredictionPublicationSettings(
        is_enabled=bool(row["match_prediction_publication_enabled"]),
    )


def _get_champion_prediction_configuration_row(
    connection,
    *,
    contest_id: int,
):
    return connection.execute(
        """
        SELECT
            champion_prediction_enabled,
            champion_prediction_deadline_at,
            champion_prediction_points,
            champion_team_id,
            match_prediction_publication_enabled
        FROM contests
        WHERE id = ?
        """,
        (contest_id,),
    ).fetchone()


def _get_champion_candidate_team_rows(
    connection,
    *,
    contest_id: int,
):
    return connection.execute(
        """
        SELECT
            teams.id,
            teams.name
        FROM teams
        JOIN (
            SELECT matches.home_team_id AS team_id
            FROM matches
            JOIN stages
                ON stages.id = matches.stage_id
            JOIN competitions
                ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?

            UNION

            SELECT matches.away_team_id AS team_id
            FROM matches
            JOIN stages
                ON stages.id = matches.stage_id
            JOIN competitions
                ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?
        ) AS contest_teams
            ON contest_teams.team_id = teams.id
        ORDER BY teams.name COLLATE NOCASE ASC, teams.id ASC
        """,
        (contest_id, contest_id),
    ).fetchall()


def _get_champion_candidate_team_row(
    connection,
    *,
    contest_id: int,
    team_id: int,
):
    return connection.execute(
        """
        SELECT
            teams.id,
            teams.name
        FROM teams
        JOIN (
            SELECT matches.home_team_id AS team_id
            FROM matches
            JOIN stages
                ON stages.id = matches.stage_id
            JOIN competitions
                ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?

            UNION

            SELECT matches.away_team_id AS team_id
            FROM matches
            JOIN stages
                ON stages.id = matches.stage_id
            JOIN competitions
                ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?
        ) AS contest_teams
            ON contest_teams.team_id = teams.id
        WHERE teams.id = ?
        """,
        (contest_id, contest_id, team_id),
    ).fetchone()


def _is_contest_completed(
    connection,
    *,
    contest_id: int,
) -> bool:
    completion_row = connection.execute(
        """
        SELECT
            COUNT(*) AS total_matches,
            SUM(
                CASE
                    WHEN matches.status IN ('finished', 'cancelled') THEN 1
                    ELSE 0
                END
            ) AS completed_matches
        FROM matches
        JOIN stages
            ON stages.id = matches.stage_id
        JOIN competitions
            ON competitions.id = stages.competition_id
        WHERE competitions.contest_id = ?
        """,
        (contest_id,),
    ).fetchone()

    if completion_row is None:
        return False

    total_matches = int(completion_row["total_matches"])
    completed_matches = int(completion_row["completed_matches"] or 0)

    return total_matches > 0 and total_matches == completed_matches


def _is_champion_prediction_open(
    deadline_at: str,
    *,
    now_utc: datetime,
) -> bool:
    try:
        deadline_utc = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(
            "У конкурса сохранён некорректный дедлайн прогноза чемпиона."
        ) from error

    if deadline_utc.tzinfo is None or deadline_utc.utcoffset() is None:
        raise RuntimeError(
            "У конкурса сохранён дедлайн прогноза чемпиона без часового пояса."
        )

    return deadline_utc.astimezone(timezone.utc) > now_utc


def _normalize_champion_prediction_enabled(value: bool) -> bool:
    if not isinstance(value, bool):
        raise ValueError(
            "Настройка прогноза на чемпиона должна быть логическим значением."
        )

    return value


def _normalize_champion_prediction_points(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Баллы за чемпиона должны быть целым неотрицательным числом.")

    if value < 0:
        raise ValueError("Баллы за чемпиона не могут быть отрицательными.")

    return value


def _normalize_champion_prediction_deadline_at(value: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError("Укажите, когда прогноз на чемпиона закрывается.")

    try:
        parsed_value = datetime.fromisoformat(normalized_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "Некорректная дата и время закрытия прогноза на чемпиона."
        ) from error

    if parsed_value.tzinfo is None or parsed_value.utcoffset() is None:
        raise ValueError(
            "Дата и время закрытия прогноза на чемпиона должны содержать часовой пояс."
        )

    return (
        parsed_value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_champion_team_id(
    value: int,
    *,
    field_name: str,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} должен быть идентификатором команды.")

    return value


def _team_summary_from_row(row) -> TeamSummary:
    return TeamSummary(
        id=int(row["id"]),
        name=str(row["name"]),
    )


def _write_match_prediction_publication_event(
    connection,
    *,
    contest_id: int,
    actor_user_id: int,
    enabled: bool,
    previous_enabled: bool,
) -> int:
    event_payload = json.dumps(
        {
            "enabled": enabled,
            "previous_enabled": previous_enabled,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    cursor = connection.execute(
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
            "contest.match_prediction_publication_settings_updated",
            "contest",
            contest_id,
            event_payload,
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Could not record publication settings event.")
    return int(cursor.lastrowid)


def _write_champion_event(
    connection,
    *,
    contest_id: int,
    actor_user_id: int,
    event_type: str,
    payload: dict[str, object],
) -> int:
    cursor = connection.execute(
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
            event_type,
            "contest",
            contest_id,
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Could not record champion event.")
    return int(cursor.lastrowid)


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


def _leaderboard_prediction_history_by_user(
    rows,
    *,
    now_utc: datetime,
) -> dict[int, tuple[MatchSummary, ...]]:
    prediction_history_by_user: dict[int, list[MatchSummary]] = {}

    for row in rows:
        if _is_prediction_open(row, now_utc=now_utc):
            continue

        match = _match_summary_from_row(row)
        if match.prediction is None:
            continue

        user_id = int(row["user_id"])
        prediction_history_by_user.setdefault(user_id, []).append(match)

    return {
        user_id: tuple(prediction_history)
        for user_id, prediction_history in prediction_history_by_user.items()
    }


def _leaderboard_champion_prediction_history_by_user(
    rows,
) -> dict[int, ChampionPredictionHistory]:
    history_by_user: dict[int, ChampionPredictionHistory] = {}

    for row in rows:
        actual_champion = (
            TeamSummary(
                id=int(row["actual_champion_id"]),
                name=str(row["actual_champion_name"]),
            )
            if row["actual_champion_id"] is not None
            else None
        )
        awarded_points = (
            int(row["awarded_points"]) if row["awarded_points"] is not None else None
        )
        history_by_user[int(row["user_id"])] = ChampionPredictionHistory(
            prediction=TeamSummary(
                id=int(row["predicted_team_id"]),
                name=str(row["predicted_team_name"]),
            ),
            actual_champion=actual_champion,
            awarded_points=awarded_points,
        )

    return history_by_user


def _contest_leaderboard_from_rows(
    rows,
    *,
    contest_slug: str,
    prediction_history_by_user: dict[int, tuple[MatchSummary, ...]] | None = None,
    champion_prediction_history_by_user: (
        dict[int, ChampionPredictionHistory] | None
    ) = None,
) -> tuple[ContestLeaderboardEntry, ...]:
    history_by_user = prediction_history_by_user or {}
    champion_history_by_user = champion_prediction_history_by_user or {}
    leaderboard: list[ContestLeaderboardEntry] = []

    sorted_rows = sorted(
        rows,
        key=lambda row: _leaderboard_sort_key(row, contest_slug=contest_slug),
    )

    for place, row in enumerate(sorted_rows, start=1):
        total_points = int(row["total_points"])

        participant_name = (
            " ".join(
                str(value) for value in (row["first_name"], row["last_name"]) if value
            )
            or "Участник"
        )
        leaderboard.append(
            ContestLeaderboardEntry(
                place=place,
                participant_name=participant_name,
                total_points=total_points,
                match_predictions_count=int(row["match_predictions_count"]),
                champion_prediction_count=int(row["champion_prediction_count"]),
                total_matches_count=int(row["total_matches_count"]),
                tiebreak_metrics=_leaderboard_tiebreak_metrics(row),
                prediction_history=history_by_user.get(int(row["user_id"]), ()),
                champion_prediction_history=champion_history_by_user.get(
                    int(row["user_id"]),
                ),
            )
        )

    return tuple(leaderboard)


def resolve_leaderboard_tiebreak_reason(
    winner: ContestLeaderboardEntry,
    runner_up: ContestLeaderboardEntry,
) -> LeaderboardTiebreakReason | None:
    if winner.total_points != runner_up.total_points:
        return None
    for reason, attribute in LEADERBOARD_SPORTING_TIEBREAKS:
        winner_value = getattr(winner.tiebreak_metrics, attribute)
        runner_up_value = getattr(runner_up.tiebreak_metrics, attribute)
        if winner_value > runner_up_value:
            return reason
        if winner_value < runner_up_value:
            raise RuntimeError(
                "Leaderboard order contradicts its sporting tiebreak metrics."
            )
    return "draw"


def _leaderboard_sort_key(row, *, contest_slug: str) -> tuple[object, ...]:
    metrics = _leaderboard_tiebreak_metrics(row)
    sporting_key = tuple(
        -int(getattr(metrics, attribute))
        for _, attribute in LEADERBOARD_SPORTING_TIEBREAKS
    )
    telegram_user_id = int(row["telegram_user_id"])
    return (
        -int(row["total_points"]),
        *sporting_key,
        _leaderboard_tiebreak_digest(
            contest_slug=contest_slug,
            telegram_user_id=telegram_user_id,
        ),
        telegram_user_id,
    )


def _leaderboard_tiebreak_metrics(row) -> LeaderboardTiebreakMetrics:
    return LeaderboardTiebreakMetrics(
        exact_score_count=int(row["exact_score_count"]),
        goal_difference_count=int(row["goal_difference_count"]),
        outcome_count=int(row["outcome_count"]),
        drawn_advancing_team_count=int(row["drawn_advancing_team_count"]),
        correct_champion_count=int(row["correct_champion_count"]),
    )


def _leaderboard_tiebreak_digest(
    *,
    contest_slug: str,
    telegram_user_id: int,
) -> bytes:
    value = (f"leaderboard-tiebreak:v1:{contest_slug}:{telegram_user_id}").encode(
        "utf-8"
    )
    return hashlib.sha256(value).digest()


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


def _serialize_datetime_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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

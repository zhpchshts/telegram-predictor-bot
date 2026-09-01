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
    create_or_revise_swiss_predictions_publication,
    create_or_revise_swiss_result_publication,
    handle_match_publication_deletion,
    revise_champion_publication_for_related_change,
    transition_contest_publications_for_master_switch,
)
from app.scoring_service import (
    TieResolutionMethod,
    TwoLeggedTieResolution,
    calculate_swiss_stage_selection_points,
    recalculate_match_prediction_scores,
    recalculate_tie_prediction_scores,
    resolve_two_legged_tie_result,
)
from app.shared_tournament_service import attach_shared_tournament


WORLD_CUP_2026_COMPETITION_NAME = "Чемпионат мира"
WORLD_CUP_2026_SEASON = "2026"
WORLD_CUP_2026_COMPETITION_TYPE = "world_cup"

DEFAULT_EXACT_SCORE_POINTS = 3
DEFAULT_GOAL_DIFFERENCE_POINTS = 2
DEFAULT_OUTCOME_POINTS = 1
DEFAULT_ADVANCING_TEAM_POINTS = 1

THE_INTERNATIONAL_2026_COMPETITION_NAME = "The International"
THE_INTERNATIONAL_2026_SEASON = "2026"
THE_INTERNATIONAL_2026_COMPETITION_TYPE = "the_international"

CHAMPIONS_LEAGUE_2026_27_COMPETITION_NAME = "Лига чемпионов"
CHAMPIONS_LEAGUE_2026_27_SEASON = "2026/27"
CHAMPIONS_LEAGUE_2026_27_COMPETITION_TYPE = "champions_league"
CHAMPIONS_LEAGUE_2026_27_DIRECT_COUNT = 8
CHAMPIONS_LEAGUE_2026_27_ELIMINATED_COUNT = 12
CHAMPIONS_LEAGUE_KNOCKOUT_ROUNDS: dict[str, tuple[str, int, str]] = {
    "playoff": ("Стыковые матчи", 10, "knockout"),
    "round_of_16": ("1/8 финала", 20, "knockout"),
    "quarterfinal": ("1/4 финала", 30, "knockout"),
    "semifinal": ("1/2 финала", 40, "knockout"),
    "final": ("Финал", 50, "final"),
}
CHAMPIONS_LEAGUE_KNOCKOUT_ROUND_CAPACITIES: dict[str, int] = {
    "playoff": 8,
    "round_of_16": 8,
    "quarterfinal": 4,
    "semifinal": 2,
    "final": 1,
}
SwissStageSelectionMode = Literal["exact", "up_to_limits"]


@dataclass(frozen=True, slots=True)
class _ContestTemplate:
    key: str
    slug_prefix: str
    competition_name: str
    competition_season: str
    competition_type: str
    exact_score_points: int
    goal_difference_points: int
    outcome_points: int
    advancing_team_points: int
    champion_prediction_points: int
    swiss_direct_qualifier_count: int
    swiss_elimination_qualifier_count: int
    swiss_selection_mode: SwissStageSelectionMode
    swiss_direct_correct_points: int
    swiss_elimination_correct_points: int
    swiss_cross_category_points: int


WORLD_CUP_2026_TEMPLATE = _ContestTemplate(
    key="world_cup_2026",
    slug_prefix="world-cup-2026",
    competition_name=WORLD_CUP_2026_COMPETITION_NAME,
    competition_season=WORLD_CUP_2026_SEASON,
    competition_type=WORLD_CUP_2026_COMPETITION_TYPE,
    exact_score_points=DEFAULT_EXACT_SCORE_POINTS,
    goal_difference_points=DEFAULT_GOAL_DIFFERENCE_POINTS,
    outcome_points=DEFAULT_OUTCOME_POINTS,
    advancing_team_points=DEFAULT_ADVANCING_TEAM_POINTS,
    champion_prediction_points=5,
    swiss_direct_qualifier_count=3,
    swiss_elimination_qualifier_count=5,
    swiss_selection_mode="exact",
    swiss_direct_correct_points=2,
    swiss_elimination_correct_points=2,
    swiss_cross_category_points=1,
)

THE_INTERNATIONAL_2026_TEMPLATE = _ContestTemplate(
    key="the_international_2026",
    slug_prefix="the-international-2026",
    competition_name=THE_INTERNATIONAL_2026_COMPETITION_NAME,
    competition_season=THE_INTERNATIONAL_2026_SEASON,
    competition_type=THE_INTERNATIONAL_2026_COMPETITION_TYPE,
    exact_score_points=2,
    goal_difference_points=0,
    outcome_points=1,
    advancing_team_points=0,
    champion_prediction_points=4,
    swiss_direct_qualifier_count=3,
    swiss_elimination_qualifier_count=5,
    swiss_selection_mode="exact",
    swiss_direct_correct_points=2,
    swiss_elimination_correct_points=2,
    swiss_cross_category_points=1,
)

CHAMPIONS_LEAGUE_2026_27_TEMPLATE = _ContestTemplate(
    key="champions_league_2026_27",
    slug_prefix="champions-league-2026-27",
    competition_name=CHAMPIONS_LEAGUE_2026_27_COMPETITION_NAME,
    competition_season=CHAMPIONS_LEAGUE_2026_27_SEASON,
    competition_type=CHAMPIONS_LEAGUE_2026_27_COMPETITION_TYPE,
    exact_score_points=DEFAULT_EXACT_SCORE_POINTS,
    goal_difference_points=DEFAULT_GOAL_DIFFERENCE_POINTS,
    outcome_points=DEFAULT_OUTCOME_POINTS,
    advancing_team_points=DEFAULT_ADVANCING_TEAM_POINTS,
    champion_prediction_points=5,
    swiss_direct_qualifier_count=CHAMPIONS_LEAGUE_2026_27_DIRECT_COUNT,
    swiss_elimination_qualifier_count=CHAMPIONS_LEAGUE_2026_27_ELIMINATED_COUNT,
    swiss_selection_mode="up_to_limits",
    swiss_direct_correct_points=2,
    swiss_elimination_correct_points=1,
    swiss_cross_category_points=0,
)


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


class TwoLeggedTieCreationConflictError(MatchCreationConflictError):
    """Raised when a two-legged tie idempotency key has conflicting data."""


class MatchNotFoundError(ValueError):
    """Raised when a match is unavailable in the current contest."""


class MatchUpdateUnavailableError(ValueError):
    """Raised when a match start time can no longer be changed."""


class PredictionUnavailableError(ValueError):
    """Raised when a prediction can no longer be changed."""


class MatchResultUnavailableError(ValueError):
    """Raised when a result cannot be saved for the match."""


class TwoLeggedTieNotFoundError(ValueError):
    """Raised when a two-legged tie is unavailable in the current contest."""


class TwoLeggedTiePredictionUnavailableError(PredictionUnavailableError):
    """Raised when the advancing-team prediction is already closed."""


class TwoLeggedTieResultUnavailableError(ValueError):
    """Raised when the two-legged tie result cannot be saved yet."""


class ChampionUnavailableError(ValueError):
    """Raised when the tournament champion cannot be saved yet."""


class ChampionPredictionSettingsLockedError(ValueError):
    """Raised when champion prediction settings are locked by a saved result."""


class SwissStagePredictionSettingsLockedError(ValueError):
    """Raised when Swiss-stage settings are locked by predictions or a result."""


class SwissStageResultUnavailableError(ValueError):
    """Raised when the Swiss-stage result cannot be saved yet."""


class TournamentTeamsLockedError(ValueError):
    """Raised when tournament teams are locked by dependent contest data."""


class SharedTournamentManagedError(ValueError):
    """Raised when a shared tournament must be edited through its global UI."""


@dataclass(frozen=True, slots=True)
class ActiveContestSummary:
    id: int
    name: str
    slug: str
    template_key: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ContestCreationResult:
    contest: ActiveContestSummary
    was_created: bool


@dataclass(frozen=True, slots=True)
class MatchPrediction:
    """A saved prediction.

    For football templates, ``home_score`` and ``away_score`` always mean the
    score after 90 minutes.  The team that advances or wins is stored
    separately, including when extra time or penalties are needed after a
    90-minute draw.  The field names are retained for API and database
    compatibility.  The International templates use the same fields for map
    wins in a series.
    """

    home_score: int
    away_score: int
    advancing_team_id: int | None


@dataclass(frozen=True, slots=True)
class MatchResult:
    """A saved result with football scores fixed to the 90-minute score.

    ``advancing_team_id`` is the separate outcome of the knockout tie.  The
    score fields represent map wins instead for The International templates.
    """

    home_score: int
    away_score: int
    advancing_team_id: int | None


@dataclass(frozen=True, slots=True)
class TwoLeggedTiePrediction:
    advancing_team_id: int


@dataclass(frozen=True, slots=True)
class TwoLeggedTieResult:
    aggregate_first_team_score: int
    aggregate_second_team_score: int
    advancing_team_id: int
    resolution_method: TieResolutionMethod
    second_leg_extra_time_home_score: int | None
    second_leg_extra_time_away_score: int | None
    second_leg_home_penalty_score: int | None
    second_leg_away_penalty_score: int | None


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


TournamentTeamLockReason = Literal[
    "match_exists",
    "champion_prediction_exists",
    "champion_result_exists",
    "swiss_prediction_exists",
    "swiss_result_exists",
]


@dataclass(frozen=True, slots=True)
class TournamentTeamsDetails:
    teams: tuple[TeamSummary, ...]
    is_locked: bool
    lock_reasons: tuple[TournamentTeamLockReason, ...]


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


SwissStageCategory = Literal["direct", "playoff", "elimination"]


@dataclass(frozen=True, slots=True)
class SwissStageSelection:
    direct_teams: tuple[TeamSummary, ...]
    playoff_teams: tuple[TeamSummary, ...]
    elimination_teams: tuple[TeamSummary, ...]
    is_complete: bool


@dataclass(frozen=True, slots=True)
class SwissStageTeamAward:
    team: TeamSummary
    predicted_category: SwissStageCategory
    actual_category: SwissStageCategory | None
    points: int | None


@dataclass(frozen=True, slots=True)
class SwissStageScoreBreakdown:
    correct_direct_count: int
    direct_points: int
    correct_elimination_count: int
    elimination_points: int
    total_points: int


@dataclass(frozen=True, slots=True)
class SwissStagePredictionDetails:
    is_enabled: bool
    deadline_at: str | None
    direct_qualifier_count: int
    elimination_qualifier_count: int
    selection_mode: SwissStageSelectionMode
    direct_correct_points: int
    elimination_correct_points: int
    cross_category_points: int
    maximum_points: int
    candidates: tuple[TeamSummary, ...]
    prediction: SwissStageSelection | None
    actual_result: SwissStageSelection | None
    is_open: bool
    settings_locked: bool
    awarded_points: int | None
    awards: tuple[SwissStageTeamAward, ...]
    score_breakdown: SwissStageScoreBreakdown | None


@dataclass(frozen=True, slots=True)
class SwissStagePredictionHistory:
    prediction: SwissStageSelection
    actual_result: SwissStageSelection | None
    awarded_points: int | None
    awards: tuple[SwissStageTeamAward, ...]
    score_breakdown: SwissStageScoreBreakdown | None


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
    participant_username: str | None
    total_points: int
    match_predictions_count: int
    two_legged_tie_predictions_count: int
    champion_prediction_count: int
    swiss_stage_prediction_count: int
    calculated_predictions_count: int
    tiebreak_metrics: LeaderboardTiebreakMetrics
    prediction_history: tuple[MatchSummary, ...] = ()
    champion_prediction_history: ChampionPredictionHistory | None = None
    swiss_stage_prediction_history: SwissStagePredictionHistory | None = None


@dataclass(frozen=True, slots=True)
class MatchSummary:
    id: int
    tie_id: int
    is_two_legged: bool
    leg_number: int | None
    best_of: int | None
    home_team_id: int
    home_team_name: str
    away_team_id: int
    away_team_name: str
    starts_at_utc: str
    status: str
    round_key: str | None
    round_name: str | None
    round_position: int | None
    bracket_position: int | None
    result: MatchResult | None
    prediction: MatchPrediction | None
    prediction_score: MatchPredictionScore | None


@dataclass(frozen=True, slots=True)
class TwoLeggedTieSummary:
    id: int
    name: str
    first_team_id: int
    first_team_name: str
    second_team_id: int
    second_team_name: str
    first_leg_match_id: int
    second_leg_match_id: int
    prediction_deadline_at: str
    is_prediction_open: bool
    round_key: str | None
    round_name: str | None
    round_position: int | None
    bracket_position: int | None
    result: TwoLeggedTieResult | None
    prediction: TwoLeggedTiePrediction | None
    awarded_points: int | None


@dataclass(frozen=True, slots=True)
class ContestDetails:
    id: int
    name: str
    slug: str
    template_key: str
    created_at: str
    is_active: bool
    shared_tournament_id: int | None
    shared_tournament_name: str | None
    tournament_teams: TournamentTeamsDetails
    match_prediction_publication: MatchPredictionPublicationSettings
    champion_prediction: ChampionPredictionDetails
    swiss_stage_prediction: SwissStagePredictionDetails
    leaderboard: tuple[ContestLeaderboardEntry, ...]
    two_legged_ties: tuple[TwoLeggedTieSummary, ...]
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


@dataclass(frozen=True, slots=True)
class TwoLeggedTieCreationResult:
    tie: TwoLeggedTieSummary
    was_created: bool


@dataclass(frozen=True, slots=True)
class TwoLeggedTiePredictionSaveResult:
    prediction: TwoLeggedTiePrediction
    was_created: bool


@dataclass(frozen=True, slots=True)
class TwoLeggedTieResultSaveResult:
    result: TwoLeggedTieResult
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
                contests.template_key,
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
                contests.template_key,
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
                ties.is_two_legged,
                matches.leg_number,
                matches.best_of,
                home_team.id AS home_team_id,
                home_team.name AS home_team_name,
                away_team.id AS away_team_id,
                away_team.name AS away_team_name,
                matches.starts_at_utc,
                matches.status,
                stages.stage_key AS round_key,
                stages.name AS round_name,
                stages.position AS round_position,
                ties.position AS bracket_position,
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

        two_legged_tie_rows = connection.execute(
            """
            SELECT
                ties.id,
                ties.name,
                stages.stage_key AS round_key,
                stages.name AS round_name,
                stages.position AS round_position,
                ties.position AS bracket_position,
                ties.first_team_id,
                first_team.name AS first_team_name,
                ties.second_team_id,
                second_team.name AS second_team_name,
                ties.advancing_team_id,
                ties.resolution_method,
                ties.second_leg_extra_time_home_score,
                ties.second_leg_extra_time_away_score,
                ties.second_leg_home_penalty_score,
                ties.second_leg_away_penalty_score,
                first_leg.id AS first_leg_match_id,
                first_leg.home_team_id AS first_leg_home_team_id,
                first_leg.away_team_id AS first_leg_away_team_id,
                first_leg.starts_at_utc AS prediction_deadline_at,
                first_leg.status AS first_leg_status,
                first_leg.home_score_final AS first_leg_home_score,
                first_leg.away_score_final AS first_leg_away_score,
                second_leg.id AS second_leg_match_id,
                second_leg.home_team_id AS second_leg_home_team_id,
                second_leg.away_team_id AS second_leg_away_team_id,
                second_leg.status AS second_leg_status,
                second_leg.home_score_final AS second_leg_home_score,
                second_leg.away_score_final AS second_leg_away_score,
                tie_predictions.predicted_advancing_team_id,
                tie_prediction_scores.points AS advancing_team_points
            FROM ties
            JOIN stages ON stages.id = ties.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            JOIN teams AS first_team ON first_team.id = ties.first_team_id
            JOIN teams AS second_team ON second_team.id = ties.second_team_id
            JOIN matches AS first_leg
                ON first_leg.tie_id = ties.id AND first_leg.leg_number = 1
            JOIN matches AS second_leg
                ON second_leg.tie_id = ties.id AND second_leg.leg_number = 2
            LEFT JOIN tie_predictions
                ON tie_predictions.tie_id = ties.id
                AND tie_predictions.user_id = (
                    SELECT users.id
                    FROM users
                    WHERE users.telegram_user_id = ?
                )
            LEFT JOIN tie_prediction_scores
                ON tie_prediction_scores.tie_prediction_id = tie_predictions.id
            WHERE competitions.contest_id = ?
              AND ties.is_two_legged = 1
            ORDER BY first_leg.starts_at_utc, ties.id
            """,
            (telegram_user_id, contest_id),
        ).fetchall()

        champion_prediction = _get_champion_prediction_details(
            connection,
            contest_id=contest_id,
            telegram_user_id=telegram_user_id,
            now_utc=resolved_now_utc,
        )
        swiss_stage_prediction = _get_swiss_stage_prediction_details(
            connection,
            contest_id=contest_id,
            telegram_user_id=telegram_user_id,
            now_utc=resolved_now_utc,
        )
        tournament_teams = _get_tournament_teams_details(
            connection,
            contest_id=contest_id,
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

                UNION

                SELECT swiss_stage_predictions.user_id
                FROM swiss_stage_predictions
                WHERE swiss_stage_predictions.contest_id = ?
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

                UNION ALL

                SELECT
                    swiss_stage_predictions.user_id,
                    CASE
                        WHEN swiss_stage_prediction_selections.category =
                            swiss_stage_result_selections.category
                            AND swiss_stage_prediction_selections.category =
                                'direct'
                        THEN swiss_stage_prediction_settings.direct_correct_points
                        WHEN swiss_stage_prediction_selections.category =
                            swiss_stage_result_selections.category
                        THEN
                            swiss_stage_prediction_settings.elimination_correct_points
                        ELSE swiss_stage_prediction_settings.cross_category_points
                    END AS points
                FROM swiss_stage_prediction_selections
                JOIN swiss_stage_predictions
                    ON swiss_stage_predictions.id =
                    swiss_stage_prediction_selections.prediction_id
                JOIN swiss_stage_prediction_settings
                    ON swiss_stage_prediction_settings.contest_id =
                    swiss_stage_predictions.contest_id
                JOIN swiss_stage_result_selections
                    ON swiss_stage_result_selections.contest_id =
                    swiss_stage_prediction_selections.contest_id
                    AND swiss_stage_result_selections.team_id =
                    swiss_stage_prediction_selections.team_id
                WHERE swiss_stage_predictions.contest_id = ?
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
                JOIN ties
                    ON ties.id = tie_predictions.tie_id
                WHERE ties.is_two_legged = 0
                  AND EXISTS (
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
                users.username,
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
                (
                    SELECT COUNT(*)
                    FROM tie_predictions
                    JOIN ties
                        ON ties.id = tie_predictions.tie_id
                    JOIN stages
                        ON stages.id = ties.stage_id
                    JOIN competitions
                        ON competitions.id = stages.competition_id
                    WHERE competitions.contest_id = ?
                        AND ties.is_two_legged = 1
                        AND tie_predictions.user_id = users.id
                ) AS two_legged_tie_predictions_count,
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
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM swiss_stage_predictions
                        JOIN swiss_stage_prediction_settings
                            ON swiss_stage_prediction_settings.contest_id =
                            swiss_stage_predictions.contest_id
                        WHERE swiss_stage_predictions.contest_id = ?
                            AND swiss_stage_predictions.user_id = users.id
                            AND swiss_stage_prediction_settings.enabled = 1
                            AND (
                                SELECT COUNT(*)
                                FROM swiss_stage_prediction_selections
                                WHERE swiss_stage_prediction_selections.prediction_id =
                                    swiss_stage_predictions.id
                                  AND swiss_stage_prediction_selections.category =
                                    'direct'
                            ) = swiss_stage_prediction_settings.direct_qualifier_count
                            AND (
                                SELECT COUNT(*)
                                FROM swiss_stage_prediction_selections
                                WHERE swiss_stage_prediction_selections.prediction_id =
                                    swiss_stage_predictions.id
                                  AND swiss_stage_prediction_selections.category =
                                    'elimination'
                            ) =
                                swiss_stage_prediction_settings.elimination_qualifier_count
                    ) THEN 1
                    ELSE 0
                END AS swiss_stage_prediction_count,
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
                        AND matches.status = 'finished'
                        AND match_predictions.user_id = users.id
                ) + (
                    SELECT COUNT(*)
                    FROM tie_predictions
                    JOIN ties
                        ON ties.id = tie_predictions.tie_id
                    JOIN stages
                        ON stages.id = ties.stage_id
                    JOIN competitions
                        ON competitions.id = stages.competition_id
                    WHERE competitions.contest_id = ?
                        AND ties.is_two_legged = 1
                        AND ties.advancing_team_id IS NOT NULL
                        AND tie_predictions.user_id = users.id
                ) + CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM champion_predictions
                        JOIN contests
                            ON contests.id = champion_predictions.contest_id
                        WHERE champion_predictions.contest_id = ?
                            AND champion_predictions.user_id = users.id
                            AND contests.champion_prediction_enabled = 1
                            AND contests.champion_team_id IS NOT NULL
                    ) THEN 1
                    ELSE 0
                END + CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM swiss_stage_predictions
                        JOIN swiss_stage_prediction_settings
                            ON swiss_stage_prediction_settings.contest_id =
                            swiss_stage_predictions.contest_id
                        JOIN swiss_stage_results
                            ON swiss_stage_results.contest_id =
                            swiss_stage_predictions.contest_id
                        WHERE swiss_stage_predictions.contest_id = ?
                            AND swiss_stage_predictions.user_id = users.id
                            AND swiss_stage_prediction_settings.enabled = 1
                            AND (
                                SELECT COUNT(*)
                                FROM swiss_stage_prediction_selections
                                WHERE swiss_stage_prediction_selections.prediction_id =
                                    swiss_stage_predictions.id
                                  AND swiss_stage_prediction_selections.category =
                                    'direct'
                            ) = swiss_stage_prediction_settings.direct_qualifier_count
                            AND (
                                SELECT COUNT(*)
                                FROM swiss_stage_prediction_selections
                                WHERE swiss_stage_prediction_selections.prediction_id =
                                    swiss_stage_predictions.id
                                  AND swiss_stage_prediction_selections.category =
                                    'elimination'
                            ) =
                                swiss_stage_prediction_settings.elimination_qualifier_count
                    ) THEN 1
                    ELSE 0
                END AS calculated_predictions_count
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
                ties.is_two_legged,
                matches.leg_number,
                matches.best_of,
                home_team.id AS home_team_id,
                home_team.name AS home_team_name,
                away_team.id AS away_team_id,
                away_team.name AS away_team_name,
                matches.starts_at_utc,
                matches.status,
                stages.stage_key AS round_key,
                stages.name AS round_name,
                stages.position AS round_position,
                ties.position AS bracket_position,
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
        leaderboard_swiss_stage_prediction_rows = (
            _get_swiss_stage_prediction_history_rows(
                connection,
                contest_id=contest_id,
            )
        )
        shared_tournament_row = connection.execute(
            """
            SELECT tournament.id, tournament.name
            FROM contest_shared_tournaments AS link
            JOIN shared_tournaments AS tournament
              ON tournament.id = link.shared_tournament_id
            WHERE link.contest_id = ?
            """,
            (contest_id,),
        ).fetchone()

        return ContestDetails(
            id=int(contest_row["id"]),
            name=str(contest_row["name"]),
            slug=str(contest_row["slug"]),
            template_key=str(contest_row["template_key"]),
            created_at=str(contest_row["created_at"]),
            is_active=bool(contest_row["is_active"]),
            shared_tournament_id=(
                int(shared_tournament_row["id"])
                if shared_tournament_row is not None
                else None
            ),
            shared_tournament_name=(
                str(shared_tournament_row["name"])
                if shared_tournament_row is not None
                else None
            ),
            tournament_teams=tournament_teams,
            match_prediction_publication=match_prediction_publication,
            champion_prediction=champion_prediction,
            swiss_stage_prediction=swiss_stage_prediction,
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
                swiss_stage_prediction_history_by_user=(
                    _leaderboard_swiss_stage_prediction_history_by_user(
                        leaderboard_swiss_stage_prediction_rows,
                        selection_mode=swiss_stage_prediction.selection_mode,
                        direct_qualifier_count=(
                            swiss_stage_prediction.direct_qualifier_count
                        ),
                        elimination_qualifier_count=(
                            swiss_stage_prediction.elimination_qualifier_count
                        ),
                        direct_correct_points=(
                            swiss_stage_prediction.direct_correct_points
                        ),
                        elimination_correct_points=(
                            swiss_stage_prediction.elimination_correct_points
                        ),
                        cross_category_points=(
                            swiss_stage_prediction.cross_category_points
                        ),
                    )
                    if not swiss_stage_prediction.is_open
                    else {}
                ),
            ),
            two_legged_ties=tuple(
                _two_legged_tie_summary_from_row(row, now_utc=resolved_now_utc)
                for row in two_legged_tie_rows
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
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
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

        swiss_stage_row = _get_swiss_stage_configuration_row(
            connection,
            contest_id=contest_id,
        )
        if swiss_stage_row is not None and bool(swiss_stage_row["enabled"]):
            stage_name, stage_genitive, _stage_prepositional = _swiss_stage_terms(
                str(contest_row["template_key"])
            )
            if swiss_stage_row["deadline_at"] is None:
                raise ContestCompletionUnavailableError(
                    f"Сначала укажите дедлайн прогноза на {stage_name}."
                )
            if _is_swiss_stage_prediction_open(
                str(swiss_stage_row["deadline_at"]),
                now_utc=resolved_now_utc,
            ):
                raise ContestCompletionUnavailableError(
                    f"Конкурс можно завершить после закрытия прогнозов на {stage_name}."
                )
            swiss_stage_result = connection.execute(
                """
                SELECT 1
                FROM swiss_stage_results
                WHERE contest_id = ?
                """,
                (contest_id,),
            ).fetchone()
            if swiss_stage_result is None:
                raise ContestCompletionUnavailableError(
                    f"Сначала укажите фактические итоги {stage_genitive}."
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


def save_tournament_teams(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    team_names: list[str],
    audit_actor: AuditActor,
) -> TournamentTeamsDetails:
    normalized_team_names = _normalize_tournament_team_names(team_names)

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)
        before_details = _get_tournament_teams_details(
            connection,
            contest_id=contest_id,
        )
        if before_details.is_locked:
            raise TournamentTeamsLockedError(
                "Список команд нельзя изменить после создания матчей, "
                "сохранения прогнозов или внесения результатов."
            )
        swiss_configuration = _get_swiss_stage_configuration_row(
            connection,
            contest_id=contest_id,
        )
        if (
            contest_row["template_key"] == CHAMPIONS_LEAGUE_2026_27_TEMPLATE.key
            and swiss_configuration is not None
            and bool(swiss_configuration["enabled"])
            and len(normalized_team_names) != 36
        ):
            raise ValueError(
                "Для включённого прогноза на общий этап Лиги чемпионов "
                "нужно сохранить ровно 36 команд."
            )
        if (
            swiss_configuration is not None
            and bool(swiss_configuration["enabled"])
            and int(swiss_configuration["direct_qualifier_count"])
            + int(swiss_configuration["elimination_qualifier_count"])
            > len(normalized_team_names)
        ):
            raise ValueError(
                "Сумма лимитов швейцарского этапа не может превышать "
                "количество команд турнира."
            )

        team_ids = tuple(
            _find_or_create_team(connection, team_name=team_name)[0]
            for team_name in normalized_team_names
        )
        before_team_ids = tuple(team.id for team in before_details.teams)
        if before_team_ids == team_ids:
            return before_details

        connection.execute(
            "DELETE FROM contest_teams WHERE contest_id = ?",
            (contest_id,),
        )
        connection.executemany(
            """
            INSERT INTO contest_teams (contest_id, team_id, position)
            VALUES (?, ?, ?)
            """,
            [
                (contest_id, team_id, position)
                for position, team_id in enumerate(team_ids)
            ],
        )
        after_details = _get_tournament_teams_details(
            connection,
            contest_id=contest_id,
        )
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=AuditEventType.TOURNAMENT_TEAMS_UPDATED,
            entity_type=AuditEntityType.CONTEST,
            entity_id=contest_id,
            contest_id=contest_id,
            before_state=_tournament_teams_snapshot(before_details.teams),
            after_state=_tournament_teams_snapshot(after_details.teams),
        )
        return after_details


def create_match(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    home_team_id: int,
    away_team_id: int,
    starts_at_utc: str,
    idempotency_key: str,
    audit_actor: AuditActor,
    best_of: int | None = None,
    round_key: str | None = None,
) -> MatchCreationResult:
    normalized_home_team_id = _normalize_team_id(
        home_team_id,
        field_name="Первая команда",
    )
    normalized_away_team_id = _normalize_team_id(
        away_team_id,
        field_name="Вторая команда",
    )
    if normalized_home_team_id == normalized_away_team_id:
        raise ValueError("В матче должны участвовать разные команды.")

    normalized_starts_at_utc = _normalize_starts_at_utc(starts_at_utc)
    normalized_idempotency_key = _normalize_match_idempotency_key(idempotency_key)

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)
        normalized_round_key = _normalize_knockout_round_key(
            template_key=str(contest_row["template_key"]),
            round_key=round_key,
            is_two_legged=False,
        )
        if contest_row["template_key"] == "the_international_2026":
            if isinstance(best_of, bool) or best_of not in (3, 5):
                raise ValueError("Для серии The International выберите Bo3 или Bo5.")
            normalized_best_of = best_of
        else:
            if best_of is not None:
                raise ValueError(
                    "Формат Bo3/Bo5 доступен только для The International."
                )
            normalized_best_of = None
        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        home_team_row = _get_contest_team_row(
            connection,
            contest_id=contest_id,
            team_id=normalized_home_team_id,
        )
        away_team_row = _get_contest_team_row(
            connection,
            contest_id=contest_id,
            team_id=normalized_away_team_id,
        )
        if home_team_row is None or away_team_row is None:
            raise ValueError(
                "Обе команды матча должны входить в список команд турнира."
            )
        resolved_home_team_id = int(home_team_row["id"])
        resolved_away_team_id = int(away_team_row["id"])
        resolved_home_team_name = str(home_team_row["name"])
        resolved_away_team_name = str(away_team_row["name"])

        request_fingerprint = _build_match_request_fingerprint(
            home_team_id=resolved_home_team_id,
            away_team_id=resolved_away_team_id,
            starts_at_utc=normalized_starts_at_utc,
            best_of=normalized_best_of,
            round_key=normalized_round_key,
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

        stage_id, stage_name, stage_type = _get_or_create_stage(
            connection,
            competition_id=int(competition_row["competition_id"]),
            round_key=normalized_round_key,
        )
        scoring_rule_set_id = int(competition_row["scoring_rule_set_id"])
        tie_position = _get_next_tie_position(
            connection,
            stage_id=stage_id,
            round_key=normalized_round_key,
            conflict_error_type=MatchCreationConflictError,
        )
        tie_name = f"{resolved_home_team_name} — {resolved_away_team_name}"

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
                    starts_at_utc,
                    best_of
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage_id,
                    tie_id,
                    scoring_rule_set_id,
                    resolved_home_team_id,
                    resolved_away_team_id,
                    normalized_starts_at_utc,
                    normalized_best_of,
                ),
            ).lastrowid
        )
        event_payload = json.dumps(
            {
                "away_team": {
                    "id": resolved_away_team_id,
                    "name": resolved_away_team_name,
                },
                "home_team": {
                    "id": resolved_home_team_id,
                    "name": resolved_home_team_name,
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
                **(
                    {"round_key": normalized_round_key}
                    if normalized_round_key is not None
                    else {}
                ),
                **(
                    {"best_of": normalized_best_of}
                    if normalized_best_of is not None
                    else {}
                ),
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


def create_two_legged_tie(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    first_team_id: int,
    second_team_id: int,
    first_leg_starts_at_utc: str,
    second_leg_starts_at_utc: str,
    idempotency_key: str,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
    round_key: str | None = None,
) -> TwoLeggedTieCreationResult:
    normalized_first_team_id = _normalize_team_id(
        first_team_id,
        field_name="Первая команда",
    )
    normalized_second_team_id = _normalize_team_id(
        second_team_id,
        field_name="Вторая команда",
    )
    if normalized_first_team_id == normalized_second_team_id:
        raise ValueError("В противостоянии должны участвовать разные команды.")

    normalized_first_start = _normalize_starts_at_utc(first_leg_starts_at_utc)
    normalized_second_start = _normalize_starts_at_utc(second_leg_starts_at_utc)
    if _parse_stored_datetime(normalized_first_start) >= _parse_stored_datetime(
        normalized_second_start
    ):
        raise ValueError("Ответный матч должен начинаться позже первого.")
    normalized_idempotency_key = _normalize_match_idempotency_key(idempotency_key)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)
        if contest_row["template_key"] == THE_INTERNATIONAL_2026_TEMPLATE.key:
            raise ValueError(
                "Двухматчевые футбольные противостояния недоступны для "
                "The International."
            )
        normalized_round_key = _normalize_knockout_round_key(
            template_key=str(contest_row["template_key"]),
            round_key=round_key,
            is_two_legged=True,
        )
        request_fingerprint = _build_two_legged_tie_request_fingerprint(
            first_team_id=normalized_first_team_id,
            second_team_id=normalized_second_team_id,
            first_leg_starts_at_utc=normalized_first_start,
            second_leg_starts_at_utc=normalized_second_start,
            round_key=normalized_round_key,
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
            if str(existing_request["request_fingerprint"]) != request_fingerprint:
                raise TwoLeggedTieCreationConflictError(
                    "Этот запрос на создание противостояния уже использован с "
                    "другими данными."
                )
            first_leg_row = _get_match_row(
                connection,
                contest_id=contest_id,
                match_id=int(existing_request["match_id"]),
            )
            if first_leg_row is None or not bool(first_leg_row["is_two_legged"]):
                raise RuntimeError(
                    "Не удалось найти противостояние, созданное предыдущим запросом."
                )
            tie_row = _get_two_legged_tie_row(
                connection,
                contest_id=contest_id,
                tie_id=int(first_leg_row["tie_id"]),
                user_id=actor_user_id,
            )
            if tie_row is None:
                raise RuntimeError(
                    "Не удалось найти противостояние, созданное предыдущим запросом."
                )
            return TwoLeggedTieCreationResult(
                tie=_two_legged_tie_summary_from_row(
                    tie_row,
                    now_utc=_resolve_now_utc(now_utc),
                ),
                was_created=False,
            )

        first_team_row = _get_contest_team_row(
            connection,
            contest_id=contest_id,
            team_id=normalized_first_team_id,
        )
        second_team_row = _get_contest_team_row(
            connection,
            contest_id=contest_id,
            team_id=normalized_second_team_id,
        )
        if first_team_row is None or second_team_row is None:
            raise ValueError(
                "Обе команды противостояния должны входить в список команд турнира."
            )

        competition_row = connection.execute(
            """
            SELECT competitions.id AS competition_id,
                   scoring_rule_sets.id AS scoring_rule_set_id
            FROM competitions
            JOIN scoring_rule_sets
                ON scoring_rule_sets.competition_id = competitions.id
            WHERE competitions.contest_id = ?
              AND competitions.is_active = 1
              AND scoring_rule_sets.is_active = 1
            ORDER BY competitions.id, scoring_rule_sets.version DESC
            LIMIT 1
            """,
            (contest_id,),
        ).fetchone()
        if competition_row is None:
            raise RuntimeError("Не удалось найти активные правила конкурса.")
        stage_id, _, _ = _get_or_create_stage(
            connection,
            competition_id=int(competition_row["competition_id"]),
            round_key=normalized_round_key,
        )
        scoring_rule_set_id = int(competition_row["scoring_rule_set_id"])
        tie_position = _get_next_tie_position(
            connection,
            stage_id=stage_id,
            round_key=normalized_round_key,
            conflict_error_type=TwoLeggedTieCreationConflictError,
        )
        tie_name = f"{first_team_row['name']} — {second_team_row['name']}"
        tie_id = int(
            connection.execute(
                """
                INSERT INTO ties (
                    stage_id, scoring_rule_set_id, name, position,
                    is_two_legged, first_team_id, second_team_id
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    stage_id,
                    scoring_rule_set_id,
                    tie_name,
                    tie_position,
                    normalized_first_team_id,
                    normalized_second_team_id,
                ),
            ).lastrowid
        )
        first_leg_match_id = int(
            connection.execute(
                """
                INSERT INTO matches (
                    stage_id, tie_id, scoring_rule_set_id, home_team_id,
                    away_team_id, starts_at_utc, leg_number
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    stage_id,
                    tie_id,
                    scoring_rule_set_id,
                    normalized_first_team_id,
                    normalized_second_team_id,
                    normalized_first_start,
                ),
            ).lastrowid
        )
        second_leg_match_id = int(
            connection.execute(
                """
                INSERT INTO matches (
                    stage_id, tie_id, scoring_rule_set_id, home_team_id,
                    away_team_id, starts_at_utc, leg_number
                )
                VALUES (?, ?, ?, ?, ?, ?, 2)
                """,
                (
                    stage_id,
                    tie_id,
                    scoring_rule_set_id,
                    normalized_second_team_id,
                    normalized_first_team_id,
                    normalized_second_start,
                ),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO match_creation_requests (
                contest_id, actor_user_id, idempotency_key,
                request_fingerprint, match_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                contest_id,
                actor_user_id,
                normalized_idempotency_key,
                request_fingerprint,
                first_leg_match_id,
            ),
        )
        event_payload = json.dumps(
            {
                "first_leg_match_id": first_leg_match_id,
                "first_leg_starts_at_utc": normalized_first_start,
                "first_team_id": normalized_first_team_id,
                "second_leg_match_id": second_leg_match_id,
                "second_leg_starts_at_utc": normalized_second_start,
                "second_team_id": normalized_second_team_id,
                **(
                    {"round_key": normalized_round_key}
                    if normalized_round_key is not None
                    else {}
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO event_log (
                contest_id, actor_user_id, event_type, entity_type,
                entity_id, payload_json
            )
            VALUES (?, ?, 'tie.created', 'tie', ?, ?)
            """,
            (contest_id, actor_user_id, tie_id, event_payload),
        )

        for match_id in (first_leg_match_id, second_leg_match_id):
            match_row = _get_match_row(
                connection,
                contest_id=contest_id,
                match_id=match_id,
            )
            if match_row is None:
                raise RuntimeError("Не удалось повторно прочитать созданный матч.")
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

        tie_row = _get_two_legged_tie_row(
            connection,
            contest_id=contest_id,
            tie_id=tie_id,
            user_id=actor_user_id,
        )
        if tie_row is None:
            raise RuntimeError("Не удалось повторно прочитать противостояние.")

    return TwoLeggedTieCreationResult(
        tie=_two_legged_tie_summary_from_row(
            tie_row,
            now_utc=_resolve_now_utc(now_utc),
        ),
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
        _ensure_contest_is_independent(connection, contest_id=contest_id)

        match_row = connection.execute(
            """
            SELECT
                matches.id,
                matches.tie_id,
                ties.is_two_legged,
                matches.starts_at_utc,
                matches.status,
                home_team.id AS home_team_id,
                home_team.name AS home_team_name,
                away_team.id AS away_team_id,
                away_team.name AS away_team_name
            FROM matches
            JOIN ties ON ties.id = matches.tie_id
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

        if bool(match_row["is_two_legged"]):
            raise MatchUpdateUnavailableError(
                "Матч двухматчевого противостояния можно удалить только вместе "
                "со всей парой."
            )

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


def delete_two_legged_tie(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    tie_id: int,
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
        _ensure_contest_is_independent(connection, contest_id=contest_id)
        tie_row = _get_two_legged_tie_row(
            connection,
            contest_id=contest_id,
            tie_id=tie_id,
            user_id=None,
        )
        if tie_row is None:
            raise TwoLeggedTieNotFoundError("Двухматчевое противостояние не найдено.")
        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        match_rows = connection.execute(
            """
            SELECT matches.id
            FROM matches
            WHERE matches.tie_id = ?
            ORDER BY matches.leg_number
            """,
            (tie_id,),
        ).fetchall()
        for row in match_rows:
            match_id = int(row["id"])
            match_row = _get_match_row(
                connection,
                contest_id=contest_id,
                match_id=match_id,
            )
            if match_row is None:
                raise RuntimeError("Не удалось прочитать удаляемый матч.")
            event = connection.execute(
                """
                INSERT INTO event_log (
                    contest_id, actor_user_id, event_type, entity_type,
                    entity_id, payload_json
                )
                VALUES (?, ?, 'match.deleted', 'match', ?, ?)
                """,
                (
                    contest_id,
                    actor_user_id,
                    match_id,
                    json.dumps(
                        {"tie_id": tie_id, "leg_number": match_row["leg_number"]},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            if event.lastrowid is None:
                raise RuntimeError("Не удалось записать событие удаления матча.")
            handle_match_publication_deletion(
                connection,
                contest_id=contest_id,
                match_id=match_id,
                event_id=int(event.lastrowid),
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
        connection.execute("DELETE FROM matches WHERE tie_id = ?", (tie_id,))
        deleted = connection.execute("DELETE FROM ties WHERE id = ?", (tie_id,))
        if deleted.rowcount != 1:
            raise TwoLeggedTieNotFoundError("Двухматчевое противостояние не найдено.")


def update_match_start(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    match_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    starts_at_utc: str,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> MatchSummary:
    normalized_starts_at_utc = _normalize_starts_at_utc(starts_at_utc)
    new_starts_at_utc = datetime.fromisoformat(
        normalized_starts_at_utc.replace("Z", "+00:00")
    )

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)
        match_row = _get_match_row(
            connection,
            contest_id=contest_id,
            match_id=match_id,
        )
        if match_row is None:
            raise MatchNotFoundError("Матч не найден.")
        if not _is_prediction_open(match_row, now_utc=resolved_now_utc):
            raise MatchUpdateUnavailableError(
                "Дата и время начала уже начавшегося матча недоступны для изменения."
            )
        if new_starts_at_utc <= resolved_now_utc:
            raise MatchUpdateUnavailableError(
                "Новое время начала матча должно быть в будущем."
            )
        if bool(match_row["is_two_legged"]):
            other_leg = connection.execute(
                """
                SELECT starts_at_utc
                FROM matches
                WHERE tie_id = ? AND leg_number != ?
                """,
                (int(match_row["tie_id"]), int(match_row["leg_number"])),
            ).fetchone()
            if other_leg is None:
                raise RuntimeError("У двухматчевого противостояния отсутствует матч.")
            other_start = _parse_stored_datetime(str(other_leg["starts_at_utc"]))
            if (
                int(match_row["leg_number"]) == 1 and new_starts_at_utc >= other_start
            ) or (
                int(match_row["leg_number"]) == 2 and new_starts_at_utc <= other_start
            ):
                raise MatchUpdateUnavailableError(
                    "Ответный матч должен начинаться позже первого."
                )
        if normalized_starts_at_utc == str(match_row["starts_at_utc"]):
            return _match_summary_from_row(match_row)

        before_state = _match_snapshot(match_row)
        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        update = connection.execute(
            """
            UPDATE matches
            SET starts_at_utc = ?
            WHERE id = ?
              AND status = 'scheduled'
            """,
            (normalized_starts_at_utc, match_id),
        )
        if update.rowcount != 1:
            raise MatchUpdateUnavailableError(
                "Дата и время начала уже начавшегося матча недоступны для изменения."
            )

        updated_match_row = _get_match_row(
            connection,
            contest_id=contest_id,
            match_id=match_id,
        )
        if updated_match_row is None:
            raise RuntimeError("Не удалось повторно прочитать изменённый матч.")

        event_payload = json.dumps(
            {
                "before": {"starts_at_utc": str(match_row["starts_at_utc"])},
                "after": {"starts_at_utc": normalized_starts_at_utc},
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        event = connection.execute(
            """
            INSERT INTO event_log (
                contest_id,
                actor_user_id,
                event_type,
                entity_type,
                entity_id,
                payload_json
            )
            VALUES (?, ?, 'match.updated', 'match', ?, ?)
            """,
            (contest_id, actor_user_id, match_id, event_payload),
        )
        if event.lastrowid is None:
            raise RuntimeError("Не удалось записать событие изменения матча.")

        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=AuditEventType.MATCH_UPDATED,
            entity_type=AuditEntityType.MATCH,
            entity_id=match_id,
            contest_id=contest_id,
            before_state=before_state,
            after_state=_match_snapshot(updated_match_row),
        )

    return _match_summary_from_row(updated_match_row)


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
    predicted_advancing_team_id: int | None,
    now_utc: datetime | None = None,
) -> MatchPredictionSaveResult:
    """Save a match prediction.

    Football score arguments are strictly the score after 90 minutes.  A
    knockout winner or advancing team is resolved and persisted independently
    through ``predicted_advancing_team_id``.
    """

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
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
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

        tie_id = int(match_row["tie_id"])
        is_two_legged = bool(match_row["is_two_legged"])
        if is_two_legged:
            if predicted_advancing_team_id is not None:
                raise ValueError(
                    "Для матча двухматчевой пары сохраните прогноз прохода отдельно."
                )
            resolved_advancing_team_id: int | None = None
        else:
            resolved_advancing_team_id = _resolve_advancing_team_for_score(
                match_row,
                advancing_team_id=normalized_advancing_team_id,
                home_score=normalized_home_score,
                away_score=normalized_away_score,
                field_name="Прогноз победителя противостояния",
            )
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

        existing_tie_prediction = (
            connection.execute(
                """
                SELECT id
                FROM tie_predictions
                WHERE tie_id = ? AND user_id = ?
                """,
                (tie_id, user_id),
            ).fetchone()
            if not is_two_legged
            else None
        )

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

        if not is_two_legged:
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
                    resolved_advancing_team_id,
                ),
            )

        return MatchPredictionSaveResult(
            prediction=MatchPrediction(
                home_score=normalized_home_score,
                away_score=normalized_away_score,
                advancing_team_id=resolved_advancing_team_id,
            ),
            was_created=(
                existing_match_prediction is None
                and (is_two_legged or existing_tie_prediction is None)
            ),
        )


def save_two_legged_tie_prediction(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    tie_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    predicted_advancing_team_id: int,
    now_utc: datetime | None = None,
) -> TwoLeggedTiePredictionSaveResult:
    normalized_team_id = _normalize_team_id(
        predicted_advancing_team_id,
        field_name="Прогноз команды, которая пройдёт дальше",
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        tie_row = _get_two_legged_tie_row(
            connection,
            contest_id=contest_id,
            tie_id=tie_id,
            user_id=None,
        )
        if tie_row is None:
            raise TwoLeggedTieNotFoundError("Двухматчевое противостояние не найдено.")
        if not _is_two_legged_tie_prediction_open(
            tie_row,
            now_utc=resolved_now_utc,
        ):
            raise TwoLeggedTiePredictionUnavailableError(
                "Прогноз на проход уже закрыт."
            )
        if normalized_team_id not in {
            int(tie_row["first_team_id"]),
            int(tie_row["second_team_id"]),
        }:
            raise ValueError(
                "Прогнозируемая команда должна участвовать в противостоянии."
            )
        user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        existing = connection.execute(
            """
            SELECT id
            FROM tie_predictions
            WHERE tie_id = ? AND user_id = ?
            """,
            (tie_id, user_id),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO tie_predictions (
                tie_id, user_id, predicted_advancing_team_id
            )
            VALUES (?, ?, ?)
            ON CONFLICT(tie_id, user_id) DO UPDATE SET
                predicted_advancing_team_id = excluded.predicted_advancing_team_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (tie_id, user_id, normalized_team_id),
        )
        return TwoLeggedTiePredictionSaveResult(
            prediction=TwoLeggedTiePrediction(
                advancing_team_id=normalized_team_id,
            ),
            was_created=existing is None,
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
    advancing_team_id: int | None,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> MatchResultSaveResult:
    """Save a match result.

    For football templates, ``home_score`` and ``away_score`` are strictly the
    score after 90 minutes; extra-time goals and shootout goals do not belong
    in these values.  The eventual winner is stored separately on the tie.
    """

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
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)
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

        tie_id = int(match_row["tie_id"])
        is_two_legged = bool(match_row["is_two_legged"])
        if is_two_legged:
            if advancing_team_id is not None:
                raise ValueError(
                    "Для матча двухматчевой пары укажите только счёт после 90 "
                    "минут. Результат противостояния сохраняется отдельно."
                )
            resolved_advancing_team_id: int | None = None
        else:
            resolved_advancing_team_id = _resolve_advancing_team_for_score(
                match_row,
                advancing_team_id=normalized_advancing_team_id,
                home_score=normalized_home_score,
                away_score=normalized_away_score,
                field_name="Победитель противостояния",
            )
        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        previous_result = _match_result_from_row(match_row)
        saved_result = MatchResult(
            home_score=normalized_home_score,
            away_score=normalized_away_score,
            advancing_team_id=resolved_advancing_team_id,
        )
        if str(match_row["status"]) == "finished" and previous_result == saved_result:
            return MatchResultSaveResult(
                result=saved_result,
                was_created=False,
            )

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

        if not is_two_legged:
            connection.execute(
                """
                UPDATE ties
                SET advancing_team_id = ?
                WHERE id = ?
                """,
                (
                    resolved_advancing_team_id,
                    tie_id,
                ),
            )

        recalculate_match_prediction_scores(
            connection,
            match_id=match_id,
        )
        if is_two_legged:
            _reconcile_two_legged_tie_after_match_result(
                connection,
                tie_id=tie_id,
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
                    "advancing_team_id": resolved_advancing_team_id,
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
        saved_result_state = _match_result_snapshot(saved_result)
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
            result=saved_result,
            was_created=previous_result is None,
        )


def save_two_legged_tie_result(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    tie_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    advancing_team_id: int | None,
    second_leg_extra_time_home_score: int | None = None,
    second_leg_extra_time_away_score: int | None = None,
    second_leg_home_penalty_score: int | None = None,
    second_leg_away_penalty_score: int | None = None,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> TwoLeggedTieResultSaveResult:
    del now_utc
    normalized_advancing_team_id = (
        _normalize_team_id(
            advancing_team_id,
            field_name="Прошедшая команда",
        )
        if advancing_team_id is not None
        else None
    )
    extra_time_home_score, extra_time_away_score = _normalize_optional_result_pair(
        home_score=second_leg_extra_time_home_score,
        away_score=second_leg_extra_time_away_score,
        field_name="Счёт дополнительного времени",
    )
    penalty_home_score, penalty_away_score = _normalize_optional_result_pair(
        home_score=second_leg_home_penalty_score,
        away_score=second_leg_away_penalty_score,
        field_name="Счёт серии пенальти",
    )

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)
        tie_row = _get_two_legged_tie_row(
            connection,
            contest_id=contest_id,
            tie_id=tie_id,
            user_id=None,
        )
        if tie_row is None:
            raise TwoLeggedTieNotFoundError("Двухматчевое противостояние не найдено.")
        second_leg_match_id = int(tie_row["second_leg_match_id"])
        before_match_row = _get_match_row(
            connection,
            contest_id=contest_id,
            match_id=second_leg_match_id,
        )
        if before_match_row is None:
            raise RuntimeError("Не удалось прочитать ответный матч противостояния.")
        if not _two_legged_tie_has_both_match_results(tie_row):
            raise TwoLeggedTieResultUnavailableError(
                "Сначала сохраните результаты обоих матчей после 90 минут."
            )
        resolution = _resolve_two_legged_tie_row(
            tie_row,
            second_leg_extra_time_home_score=extra_time_home_score,
            second_leg_extra_time_away_score=extra_time_away_score,
            second_leg_home_penalty_score=penalty_home_score,
            second_leg_away_penalty_score=penalty_away_score,
            advancing_team_id=normalized_advancing_team_id,
        )
        previous_result = _two_legged_tie_result_from_row(tie_row)
        saved_result = _two_legged_tie_result_from_resolution(
            resolution,
            second_leg_extra_time_home_score=extra_time_home_score,
            second_leg_extra_time_away_score=extra_time_away_score,
            second_leg_home_penalty_score=penalty_home_score,
            second_leg_away_penalty_score=penalty_away_score,
        )
        if previous_result == saved_result:
            return TwoLeggedTieResultSaveResult(
                result=saved_result,
                was_created=False,
            )

        actor_user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        connection.execute(
            """
            UPDATE ties
            SET advancing_team_id = ?,
                resolution_method = ?,
                second_leg_extra_time_home_score = ?,
                second_leg_extra_time_away_score = ?,
                second_leg_home_penalty_score = ?,
                second_leg_away_penalty_score = ?
            WHERE id = ?
            """,
            (
                saved_result.advancing_team_id,
                saved_result.resolution_method,
                saved_result.second_leg_extra_time_home_score,
                saved_result.second_leg_extra_time_away_score,
                saved_result.second_leg_home_penalty_score,
                saved_result.second_leg_away_penalty_score,
                tie_id,
            ),
        )
        recalculate_tie_prediction_scores(connection, tie_id=tie_id)
        connection.execute(
            """
            INSERT INTO event_log (
                contest_id, actor_user_id, event_type, entity_type,
                entity_id, payload_json
            )
            VALUES (?, ?, ?, 'tie', ?, ?)
            """,
            (
                contest_id,
                actor_user_id,
                (
                    "tie.result_recorded"
                    if previous_result is None
                    else "tie.result_corrected"
                ),
                tie_id,
                json.dumps(
                    {
                        "previous_result": (
                            _two_legged_tie_result_snapshot(previous_result)
                            if previous_result is not None
                            else None
                        ),
                        "result": _two_legged_tie_result_snapshot(saved_result),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
        after_match_row = _get_match_row(
            connection,
            contest_id=contest_id,
            match_id=second_leg_match_id,
        )
        if after_match_row is None:
            raise RuntimeError("Не удалось повторно прочитать ответный матч.")
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
            entity_id=second_leg_match_id,
            contest_id=contest_id,
            before_state=_match_snapshot(before_match_row),
            after_state=_match_snapshot(after_match_row),
            metadata={
                "result_scope": "two_legged_tie",
                "two_legged_tie_id": tie_id,
            },
        )
        return TwoLeggedTieResultSaveResult(
            result=saved_result,
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

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
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
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)

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
            if not _get_tournament_team_rows(
                connection,
                contest_id=contest_id,
            ):
                raise ValueError("Сначала добавьте команды турнира.")
            if deadline_at is None:
                raise ValueError("Укажите, когда прогноз на чемпиона закрывается.")

            normalized_deadline_at = _normalize_champion_prediction_deadline_at(
                deadline_at
            )
        else:
            normalized_deadline_at = None

        previous_deadline_at = previous_row["champion_prediction_deadline_at"]
        previous_deadline_at_value = (
            str(previous_deadline_at) if previous_deadline_at is not None else None
        )
        if normalized_deadline_at != previous_deadline_at_value:
            _validate_existing_deadline_change(
                previous_deadline_at=previous_deadline_at_value,
                new_deadline_at=normalized_deadline_at,
                now_utc=resolved_now_utc,
                locked_error=ChampionPredictionSettingsLockedError(
                    "Дедлайн прогноза на чемпиона нельзя изменить после его наступления."
                ),
                past_deadline_message=(
                    "Новый дедлайн прогноза на чемпиона должен быть в будущем."
                ),
            )

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
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
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
            SELECT id, predicted_team_id
            FROM champion_predictions
            WHERE contest_id = ?
                AND user_id = ?
            """,
            (contest_id, user_id),
        ).fetchone()
        if (
            existing_prediction is not None
            and int(existing_prediction["predicted_team_id"]) == normalized_team_id
        ):
            return _team_summary_from_row(team_row)

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

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)

        configuration_row = _get_champion_prediction_configuration_row(
            connection,
            contest_id=contest_id,
        )
        if configuration_row is None:
            raise RuntimeError("Не удалось найти настройки прогноза чемпиона.")

        if not bool(configuration_row["champion_prediction_enabled"]):
            raise ChampionUnavailableError("Сначала включите прогноз на чемпиона.")

        if not _is_contest_completed(
            connection,
            contest_id=contest_id,
            allow_no_matches=True,
        ):
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


def save_swiss_stage_prediction_settings(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    enabled: bool,
    deadline_at: str | None,
    direct_qualifier_count: int,
    elimination_qualifier_count: int,
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> None:
    normalized_enabled = _normalize_swiss_stage_enabled(enabled)
    normalized_direct_count = _normalize_swiss_stage_limit(
        direct_qualifier_count,
        field_name="Количество прямых проходов",
    )
    normalized_elimination_count = _normalize_swiss_stage_limit(
        elimination_qualifier_count,
        field_name="Количество команд второй категории",
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)
        stage_name, _stage_genitive, _stage_prepositional = _swiss_stage_terms(
            str(contest_row["template_key"])
        )
        if normalized_enabled:
            if deadline_at is None:
                raise ValueError(f"Укажите, когда прогноз на {stage_name} закрывается.")
            normalized_deadline_at = _normalize_swiss_stage_deadline_at(
                deadline_at,
                stage_name=stage_name,
            )
        else:
            normalized_deadline_at = (
                _normalize_swiss_stage_deadline_at(
                    deadline_at,
                    stage_name=stage_name,
                )
                if deadline_at is not None
                else None
            )
        if contest_row["template_key"] == CHAMPIONS_LEAGUE_2026_27_TEMPLATE.key and (
            normalized_direct_count != CHAMPIONS_LEAGUE_2026_27_DIRECT_COUNT
            or normalized_elimination_count != CHAMPIONS_LEAGUE_2026_27_ELIMINATED_COUNT
        ):
            raise ValueError(
                "Для Лиги чемпионов выберите 8 команд напрямую в 1/8 "
                "и 12 команд, которые вылетят после общего этапа."
            )
        previous_row = _get_swiss_stage_configuration_row(
            connection,
            contest_id=contest_id,
        )
        settings_locked = _are_swiss_stage_settings_locked(
            connection,
            contest_id=contest_id,
        )
        previous_deadline_at = (
            str(previous_row["deadline_at"])
            if previous_row is not None and previous_row["deadline_at"] is not None
            else None
        )
        deadline_changed = normalized_deadline_at != previous_deadline_at
        if deadline_changed:
            _validate_existing_deadline_change(
                previous_deadline_at=previous_deadline_at,
                new_deadline_at=normalized_deadline_at,
                now_utc=resolved_now_utc,
                locked_error=SwissStagePredictionSettingsLockedError(
                    f"Дедлайн прогноза на {stage_name} нельзя изменить "
                    "после его наступления."
                ),
                past_deadline_message=(
                    f"Новый дедлайн прогноза на {stage_name} должен быть в будущем."
                ),
            )

        non_deadline_settings_changed = previous_row is None or any(
            (
                normalized_enabled != bool(previous_row["enabled"]),
                normalized_direct_count != int(previous_row["direct_qualifier_count"]),
                normalized_elimination_count
                != int(previous_row["elimination_qualifier_count"]),
            )
        )
        if settings_locked and non_deadline_settings_changed:
            raise SwissStagePredictionSettingsLockedError(
                f"Настройки прогноза на {stage_name} нельзя изменить "
                "после сохранения первого прогноза или результата. "
                "До дедлайна можно изменить только сам дедлайн."
            )

        existing_tournament_teams = _get_tournament_team_rows(
            connection,
            contest_id=contest_id,
        )
        tournament_team_count = len(existing_tournament_teams)
        if normalized_enabled and tournament_team_count == 0:
            raise ValueError("Сначала добавьте команды турнира.")
        if (
            normalized_enabled
            and contest_row["template_key"] == CHAMPIONS_LEAGUE_2026_27_TEMPLATE.key
            and tournament_team_count != 36
        ):
            raise ValueError(
                "Для общего этапа Лиги чемпионов добавьте ровно 36 команд."
            )
        if (
            normalized_enabled
            and tournament_team_count > 0
            and normalized_direct_count + normalized_elimination_count
            > tournament_team_count
        ):
            raise ValueError(
                "Сумма лимитов прохода не может превышать количество команд турнира."
            )

        before_state = _swiss_stage_snapshot(connection, contest_id=contest_id)
        connection.execute(
            """
            INSERT INTO swiss_stage_prediction_settings (
                contest_id,
                enabled,
                deadline_at,
                direct_qualifier_count,
                elimination_qualifier_count
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(contest_id) DO UPDATE SET
                enabled = excluded.enabled,
                deadline_at = excluded.deadline_at,
                direct_qualifier_count = excluded.direct_qualifier_count,
                elimination_qualifier_count =
                    excluded.elimination_qualifier_count,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                contest_id,
                int(normalized_enabled),
                normalized_deadline_at,
                normalized_direct_count,
                normalized_elimination_count,
            ),
        )
        after_state = _swiss_stage_snapshot(connection, contest_id=contest_id)
        if before_state != after_state:
            _record_audit(
                connection,
                audit_actor=audit_actor,
                telegram_chat_id=telegram_chat_id,
                event_type=AuditEventType.SWISS_STAGE_SETTINGS_UPDATED,
                entity_type=AuditEntityType.SWISS_STAGE_PREDICTION,
                entity_id=contest_id,
                contest_id=contest_id,
                before_state=before_state,
                after_state=after_state,
            )
            settings_event_id = _write_swiss_publication_event(
                connection,
                contest_id=contest_id,
                event_type="swiss.prediction_settings_updated",
            )
            if bool(contest_row["match_prediction_publication_enabled"]):
                create_or_revise_swiss_predictions_publication(
                    connection,
                    contest_id=contest_id,
                    event_id=settings_event_id,
                )


def save_swiss_stage_prediction(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
    direct_team_ids: list[int],
    elimination_team_ids: list[int],
    now_utc: datetime | None = None,
) -> SwissStageSelection:
    normalized_direct_ids, normalized_elimination_ids = (
        _normalize_swiss_stage_team_id_sets(
            direct_team_ids,
            elimination_team_ids,
        )
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        configuration_row = _require_enabled_swiss_stage_configuration(
            connection,
            contest_id=contest_id,
            template_key=str(contest_row["template_key"]),
        )
        stage_name, _stage_genitive, _stage_prepositional = _swiss_stage_terms(
            str(contest_row["template_key"])
        )
        deadline_at = configuration_row["deadline_at"]
        if deadline_at is None:
            raise PredictionUnavailableError(
                f"Для прогноза на {stage_name} не задан дедлайн."
            )
        if not _is_swiss_stage_prediction_open(
            str(deadline_at),
            now_utc=resolved_now_utc,
        ):
            raise PredictionUnavailableError(f"Прогноз на {stage_name} уже закрыт.")
        _validate_swiss_stage_selection(
            connection,
            contest_id=contest_id,
            direct_team_ids=normalized_direct_ids,
            elimination_team_ids=normalized_elimination_ids,
            direct_qualifier_count=int(configuration_row["direct_qualifier_count"]),
            elimination_qualifier_count=int(
                configuration_row["elimination_qualifier_count"]
            ),
            selection_mode=str(configuration_row["selection_mode"]),
            require_complete=False,
            template_key=str(contest_row["template_key"]),
        )

        user_id = _upsert_user(
            connection,
            telegram_user_id=telegram_user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
        )
        prediction_row = connection.execute(
            """
            SELECT id
            FROM swiss_stage_predictions
            WHERE contest_id = ? AND user_id = ?
            """,
            (contest_id, user_id),
        ).fetchone()
        if prediction_row is not None:
            existing_selection_rows = connection.execute(
                """
                SELECT team_id, category
                FROM swiss_stage_prediction_selections
                WHERE prediction_id = ?
                ORDER BY category, team_id
                """,
                (int(prediction_row["id"]),),
            ).fetchall()
            existing_direct_ids = {
                int(row["team_id"])
                for row in existing_selection_rows
                if row["category"] == "direct"
            }
            existing_elimination_ids = {
                int(row["team_id"])
                for row in existing_selection_rows
                if row["category"] == "elimination"
            }
            if existing_direct_ids == set(
                normalized_direct_ids
            ) and existing_elimination_ids == set(normalized_elimination_ids):
                return _swiss_stage_selection_from_ids(
                    connection,
                    contest_id=contest_id,
                    selection_mode=str(configuration_row["selection_mode"]),
                    direct_qualifier_count=int(
                        configuration_row["direct_qualifier_count"]
                    ),
                    elimination_qualifier_count=int(
                        configuration_row["elimination_qualifier_count"]
                    ),
                    direct_team_ids=normalized_direct_ids,
                    elimination_team_ids=normalized_elimination_ids,
                )
            connection.execute(
                """
                UPDATE swiss_stage_predictions
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(prediction_row["id"]),),
            )
        else:
            connection.execute(
                """
                INSERT INTO swiss_stage_predictions (contest_id, user_id)
                VALUES (?, ?)
                """,
                (contest_id, user_id),
            )
            prediction_row = connection.execute(
                """
                SELECT id
                FROM swiss_stage_predictions
                WHERE contest_id = ? AND user_id = ?
                """,
                (contest_id, user_id),
            ).fetchone()
        if prediction_row is None:
            raise RuntimeError(f"Не удалось сохранить прогноз на {stage_name}.")
        prediction_id = int(prediction_row["id"])
        connection.execute(
            """
            DELETE FROM swiss_stage_prediction_selections
            WHERE prediction_id = ?
            """,
            (prediction_id,),
        )
        _insert_swiss_stage_selections(
            connection,
            table_name="swiss_stage_prediction_selections",
            owner_column="prediction_id",
            owner_id=prediction_id,
            contest_id=contest_id,
            direct_team_ids=normalized_direct_ids,
            elimination_team_ids=normalized_elimination_ids,
        )
        return _swiss_stage_selection_from_ids(
            connection,
            contest_id=contest_id,
            selection_mode=str(configuration_row["selection_mode"]),
            direct_qualifier_count=int(configuration_row["direct_qualifier_count"]),
            elimination_qualifier_count=int(
                configuration_row["elimination_qualifier_count"]
            ),
            direct_team_ids=normalized_direct_ids,
            elimination_team_ids=normalized_elimination_ids,
        )


def save_swiss_stage_result(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    direct_team_ids: list[int],
    elimination_team_ids: list[int],
    audit_actor: AuditActor,
    now_utc: datetime | None = None,
) -> SwissStageSelection:
    normalized_direct_ids, normalized_elimination_ids = (
        _normalize_swiss_stage_team_id_sets(
            direct_team_ids,
            elimination_team_ids,
        )
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        resolved_now_utc = _resolve_now_utc(now_utc)
        contest_row = _get_active_contest_row(
            connection,
            telegram_chat_id=telegram_chat_id,
            contest_id=contest_id,
        )
        _ensure_contest_is_independent(connection, contest_id=contest_id)
        configuration_row = _require_enabled_swiss_stage_configuration(
            connection,
            contest_id=contest_id,
            result_operation=True,
            template_key=str(contest_row["template_key"]),
        )
        stage_name, stage_genitive, _stage_prepositional = _swiss_stage_terms(
            str(contest_row["template_key"])
        )
        deadline_at = configuration_row["deadline_at"]
        if deadline_at is None:
            raise SwissStageResultUnavailableError(
                f"Для прогноза на {stage_name} не задан дедлайн."
            )
        if _is_swiss_stage_prediction_open(
            str(deadline_at),
            now_utc=resolved_now_utc,
        ):
            raise SwissStageResultUnavailableError(
                f"Итоги {stage_genitive} можно указать после дедлайна."
            )
        _validate_swiss_stage_selection(
            connection,
            contest_id=contest_id,
            direct_team_ids=normalized_direct_ids,
            elimination_team_ids=normalized_elimination_ids,
            direct_qualifier_count=int(configuration_row["direct_qualifier_count"]),
            elimination_qualifier_count=int(
                configuration_row["elimination_qualifier_count"]
            ),
            selection_mode=str(configuration_row["selection_mode"]),
            require_complete=True,
            template_key=str(contest_row["template_key"]),
        )

        existing_result = connection.execute(
            """
            SELECT 1
            FROM swiss_stage_results
            WHERE contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
        existing_selection_rows = connection.execute(
            """
            SELECT team_id, category
            FROM swiss_stage_result_selections
            WHERE contest_id = ?
            ORDER BY category, team_id
            """,
            (contest_id,),
        ).fetchall()
        existing_direct_ids = tuple(
            int(row["team_id"])
            for row in existing_selection_rows
            if row["category"] == "direct"
        )
        existing_elimination_ids = tuple(
            int(row["team_id"])
            for row in existing_selection_rows
            if row["category"] == "elimination"
        )
        if (
            existing_result is not None
            and set(existing_direct_ids) == set(normalized_direct_ids)
            and set(existing_elimination_ids) == set(normalized_elimination_ids)
        ):
            return _swiss_stage_selection_from_ids(
                connection,
                contest_id=contest_id,
                selection_mode=str(configuration_row["selection_mode"]),
                direct_qualifier_count=int(configuration_row["direct_qualifier_count"]),
                elimination_qualifier_count=int(
                    configuration_row["elimination_qualifier_count"]
                ),
                direct_team_ids=existing_direct_ids,
                elimination_team_ids=existing_elimination_ids,
            )

        before_state = _swiss_stage_snapshot(connection, contest_id=contest_id)
        connection.execute(
            """
            INSERT INTO swiss_stage_results (contest_id)
            VALUES (?)
            ON CONFLICT(contest_id) DO UPDATE SET
                updated_at = CURRENT_TIMESTAMP
            """,
            (contest_id,),
        )
        connection.execute(
            """
            DELETE FROM swiss_stage_result_selections
            WHERE contest_id = ?
            """,
            (contest_id,),
        )
        _insert_swiss_stage_selections(
            connection,
            table_name="swiss_stage_result_selections",
            owner_column=None,
            owner_id=None,
            contest_id=contest_id,
            direct_team_ids=normalized_direct_ids,
            elimination_team_ids=normalized_elimination_ids,
        )
        after_state = _swiss_stage_snapshot(connection, contest_id=contest_id)
        _record_audit(
            connection,
            audit_actor=audit_actor,
            telegram_chat_id=telegram_chat_id,
            event_type=(
                AuditEventType.SWISS_STAGE_RESULT_SET
                if existing_result is None
                else AuditEventType.SWISS_STAGE_RESULT_CHANGED
            ),
            entity_type=AuditEntityType.SWISS_STAGE_PREDICTION,
            entity_id=contest_id,
            contest_id=contest_id,
            before_state=before_state,
            after_state=after_state,
        )
        result_event_id = _write_swiss_publication_event(
            connection,
            contest_id=contest_id,
            event_type=(
                "swiss.result_recorded"
                if existing_result is None
                else "swiss.result_corrected"
            ),
        )
        create_or_revise_swiss_result_publication(
            connection,
            contest_id=contest_id,
            event_id=result_event_id,
            was_created=existing_result is None,
            now_utc=resolved_now_utc,
        )
        return _swiss_stage_selection_from_ids(
            connection,
            contest_id=contest_id,
            selection_mode=str(configuration_row["selection_mode"]),
            direct_qualifier_count=int(configuration_row["direct_qualifier_count"]),
            elimination_qualifier_count=int(
                configuration_row["elimination_qualifier_count"]
            ),
            direct_team_ids=normalized_direct_ids,
            elimination_team_ids=normalized_elimination_ids,
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
    audit_actor: AuditActor,
    shared_tournament_id: int | None = None,
) -> ContestCreationResult:
    return _create_contest_from_template(
        template=WORLD_CUP_2026_TEMPLATE,
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        chat_title=chat_title,
        telegram_user_id=telegram_user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        contest_name=contest_name,
        idempotency_key=idempotency_key,
        audit_actor=audit_actor,
        shared_tournament_id=shared_tournament_id,
    )


def create_the_international_2026_contest(
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
    shared_tournament_id: int | None = None,
) -> ContestCreationResult:
    return _create_contest_from_template(
        template=THE_INTERNATIONAL_2026_TEMPLATE,
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        chat_title=chat_title,
        telegram_user_id=telegram_user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        contest_name=contest_name,
        idempotency_key=idempotency_key,
        audit_actor=audit_actor,
        shared_tournament_id=shared_tournament_id,
    )


def create_champions_league_2026_27_contest(
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
    shared_tournament_id: int | None = None,
) -> ContestCreationResult:
    return _create_contest_from_template(
        template=CHAMPIONS_LEAGUE_2026_27_TEMPLATE,
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        chat_title=chat_title,
        telegram_user_id=telegram_user_id,
        first_name=first_name,
        last_name=last_name,
        username=username,
        contest_name=contest_name,
        idempotency_key=idempotency_key,
        audit_actor=audit_actor,
        shared_tournament_id=shared_tournament_id,
    )


def _create_contest_from_template(
    *,
    template: _ContestTemplate,
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
    shared_tournament_id: int | None = None,
) -> ContestCreationResult:
    normalized_contest_name = _normalize_contest_name(contest_name)
    normalized_idempotency_key = _normalize_idempotency_key(idempotency_key)
    request_fingerprint = _build_request_fingerprint(
        normalized_contest_name,
        template=template,
        shared_tournament_id=shared_tournament_id,
    )

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
                SELECT id, name, slug, template_key, created_at
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

        slug = _build_contest_slug(template.slug_prefix)

        contest_id = int(
            connection.execute(
                """
                INSERT INTO contests (
                    chat_id,
                    name,
                    slug,
                    template_key,
                    champion_prediction_points
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    normalized_contest_name,
                    slug,
                    template.key,
                    template.champion_prediction_points,
                ),
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
                    template.competition_name,
                    template.competition_season,
                    template.competition_type,
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
                    template.exact_score_points,
                    template.goal_difference_points,
                    template.outcome_points,
                    template.advancing_team_points,
                ),
            ).lastrowid
        )

        if (
            shared_tournament_id is None
            and template.key == CHAMPIONS_LEAGUE_2026_27_TEMPLATE.key
        ):
            connection.execute(
                """
                INSERT INTO swiss_stage_prediction_settings (
                    contest_id,
                    direct_qualifier_count,
                    elimination_qualifier_count,
                    selection_mode,
                    direct_correct_points,
                    elimination_correct_points,
                    cross_category_points
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contest_id,
                    template.swiss_direct_qualifier_count,
                    template.swiss_elimination_qualifier_count,
                    template.swiss_selection_mode,
                    template.swiss_direct_correct_points,
                    template.swiss_elimination_correct_points,
                    template.swiss_cross_category_points,
                ),
            )

        if shared_tournament_id is not None:
            attach_shared_tournament(
                connection,
                contest_id=contest_id,
                shared_tournament_id=shared_tournament_id,
            )

        event_payload = json.dumps(
            {
                "competition": {
                    "id": competition_id,
                    "name": template.competition_name,
                    "season": template.competition_season,
                    "type": template.competition_type,
                },
                "contest_name": normalized_contest_name,
                "scoring_rule_set": {
                    "advancing_team_points": template.advancing_team_points,
                    "exact_score_points": template.exact_score_points,
                    "goal_difference_points": template.goal_difference_points,
                    "id": scoring_rule_set_id,
                    "outcome_points": template.outcome_points,
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
            SELECT id, name, slug, template_key, created_at
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
            contests.template_key,
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
) -> int:
    if audit_actor.telegram_chat_id != telegram_chat_id:
        raise ValueError("Audit actor chat does not match the administrative action.")
    return record_audit_event(
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


def _write_swiss_publication_event(
    connection,
    *,
    contest_id: int,
    event_type: str,
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
        VALUES (?, NULL, ?, 'contest', ?, '{}')
        """,
        (contest_id, event_type, contest_id),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Не удалось записать событие публикации швейцарского этапа.")
    return int(cursor.lastrowid)


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
        "template_key": str(row["template_key"]),
    }


def _match_snapshot(row) -> dict[str, object]:
    keys = set(row.keys())
    snapshot: dict[str, object] = {
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
        **(
            {"best_of": int(row["best_of"])}
            if "best_of" in keys and row["best_of"] is not None
            else {}
        ),
        "starts_at_utc": str(row["starts_at_utc"]),
        "status": str(row["status"]),
        "tie_id": int(row["tie_id"]) if row["tie_id"] is not None else None,
    }
    if "is_two_legged" in keys and bool(row["is_two_legged"]):
        snapshot["is_two_legged"] = True
        snapshot["leg_number"] = (
            int(row["leg_number"]) if row["leg_number"] is not None else None
        )
        snapshot["two_legged_tie_result"] = {
            "resolution_method": row["resolution_method"],
            "second_leg_extra_time_home_score": row["second_leg_extra_time_home_score"],
            "second_leg_extra_time_away_score": row["second_leg_extra_time_away_score"],
            "second_leg_home_penalty_score": row["second_leg_home_penalty_score"],
            "second_leg_away_penalty_score": row["second_leg_away_penalty_score"],
        }
    return snapshot


def _match_result_snapshot(
    result: MatchResult | None,
) -> dict[str, int | None] | None:
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


def _ensure_contest_is_independent(connection, *, contest_id: int) -> None:
    shared_link = connection.execute(
        """
        SELECT 1
        FROM contest_shared_tournaments
        WHERE contest_id = ?
        """,
        (contest_id,),
    ).fetchone()
    if shared_link is not None:
        raise SharedTournamentManagedError(
            "Этот конкурс использует общий турнир. Измените команды, дедлайны "
            "или результаты в разделе «Общие турниры»."
        )


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
            ties.is_two_legged,
            matches.leg_number,
            matches.best_of,
            contests.template_key,
            home_team.id AS home_team_id,
            home_team.name AS home_team_name,
            away_team.id AS away_team_id,
            away_team.name AS away_team_name,
            matches.starts_at_utc,
            matches.status,
            stages.stage_key AS round_key,
            stages.name AS round_name,
            stages.position AS round_position,
            ties.position AS bracket_position,
            matches.home_score_final,
            matches.away_score_final,
            ties.first_team_id,
            ties.second_team_id,
            ties.advancing_team_id,
            ties.resolution_method,
            ties.second_leg_extra_time_home_score,
            ties.second_leg_extra_time_away_score,
            ties.second_leg_home_penalty_score,
            ties.second_leg_away_penalty_score
        FROM matches
        JOIN ties
            ON ties.id = matches.tie_id
        JOIN stages
            ON stages.id = matches.stage_id
        JOIN competitions
            ON competitions.id = stages.competition_id
        JOIN contests
            ON contests.id = competitions.contest_id
        JOIN teams AS home_team
            ON home_team.id = matches.home_team_id
        JOIN teams AS away_team
            ON away_team.id = matches.away_team_id
        WHERE competitions.contest_id = ?
            AND matches.id = ?
        """,
        (contest_id, match_id),
    ).fetchone()


def _get_two_legged_tie_row(
    connection,
    *,
    contest_id: int | None,
    tie_id: int,
    user_id: int | None,
):
    return connection.execute(
        """
        SELECT
            ties.id,
            ties.name,
            stages.stage_key AS round_key,
            stages.name AS round_name,
            stages.position AS round_position,
            ties.position AS bracket_position,
            ties.first_team_id,
            first_team.name AS first_team_name,
            ties.second_team_id,
            second_team.name AS second_team_name,
            ties.advancing_team_id,
            ties.resolution_method,
            ties.second_leg_extra_time_home_score,
            ties.second_leg_extra_time_away_score,
            ties.second_leg_home_penalty_score,
            ties.second_leg_away_penalty_score,
            first_leg.id AS first_leg_match_id,
            first_leg.home_team_id AS first_leg_home_team_id,
            first_leg.away_team_id AS first_leg_away_team_id,
            first_leg.starts_at_utc AS prediction_deadline_at,
            first_leg.status AS first_leg_status,
            first_leg.home_score_final AS first_leg_home_score,
            first_leg.away_score_final AS first_leg_away_score,
            second_leg.id AS second_leg_match_id,
            second_leg.home_team_id AS second_leg_home_team_id,
            second_leg.away_team_id AS second_leg_away_team_id,
            second_leg.status AS second_leg_status,
            second_leg.home_score_final AS second_leg_home_score,
            second_leg.away_score_final AS second_leg_away_score,
            tie_predictions.predicted_advancing_team_id,
            tie_prediction_scores.points AS advancing_team_points
        FROM ties
        JOIN stages ON stages.id = ties.stage_id
        JOIN competitions ON competitions.id = stages.competition_id
        JOIN teams AS first_team ON first_team.id = ties.first_team_id
        JOIN teams AS second_team ON second_team.id = ties.second_team_id
        JOIN matches AS first_leg
            ON first_leg.tie_id = ties.id AND first_leg.leg_number = 1
        JOIN matches AS second_leg
            ON second_leg.tie_id = ties.id AND second_leg.leg_number = 2
        LEFT JOIN tie_predictions
            ON tie_predictions.tie_id = ties.id
            AND tie_predictions.user_id = ?
        LEFT JOIN tie_prediction_scores
            ON tie_prediction_scores.tie_prediction_id = tie_predictions.id
        WHERE ties.id = ?
          AND ties.is_two_legged = 1
          AND (? IS NULL OR competitions.contest_id = ?)
        """,
        (user_id, tie_id, contest_id, contest_id),
    ).fetchone()


def _get_tournament_team_rows(connection, *, contest_id: int):
    return connection.execute(
        """
        SELECT teams.id, teams.name
        FROM contest_teams
        JOIN teams ON teams.id = contest_teams.team_id
        WHERE contest_teams.contest_id = ?
        ORDER BY contest_teams.position, teams.id
        """,
        (contest_id,),
    ).fetchall()


def _get_contest_team_row(connection, *, contest_id: int, team_id: int):
    return connection.execute(
        """
        SELECT teams.id, teams.name
        FROM contest_teams
        JOIN teams ON teams.id = contest_teams.team_id
        WHERE contest_teams.contest_id = ?
          AND contest_teams.team_id = ?
        """,
        (contest_id, team_id),
    ).fetchone()


def _get_tournament_team_lock_reasons(
    connection,
    *,
    contest_id: int,
) -> tuple[TournamentTeamLockReason, ...]:
    row = connection.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM matches
                JOIN stages ON stages.id = matches.stage_id
                JOIN competitions
                    ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?
            ) AS match_exists,
            EXISTS (
                SELECT 1
                FROM champion_predictions
                WHERE contest_id = ?
            ) AS champion_prediction_exists,
            EXISTS (
                SELECT 1
                FROM contests
                WHERE id = ? AND champion_team_id IS NOT NULL
            ) AS champion_result_exists,
            EXISTS (
                SELECT 1
                FROM swiss_stage_predictions
                WHERE contest_id = ?
            ) AS swiss_prediction_exists,
            EXISTS (
                SELECT 1
                FROM swiss_stage_results
                WHERE contest_id = ?
            ) AS swiss_result_exists
        """,
        (contest_id, contest_id, contest_id, contest_id, contest_id),
    ).fetchone()
    if row is None:
        return ()
    ordered_reasons: tuple[TournamentTeamLockReason, ...] = (
        "match_exists",
        "champion_prediction_exists",
        "champion_result_exists",
        "swiss_prediction_exists",
        "swiss_result_exists",
    )
    return tuple(reason for reason in ordered_reasons if bool(row[reason]))


def _get_tournament_teams_details(
    connection,
    *,
    contest_id: int,
) -> TournamentTeamsDetails:
    teams = tuple(
        _team_summary_from_row(row)
        for row in _get_tournament_team_rows(connection, contest_id=contest_id)
    )
    lock_reasons = _get_tournament_team_lock_reasons(
        connection,
        contest_id=contest_id,
    )
    return TournamentTeamsDetails(
        teams=teams,
        is_locked=bool(lock_reasons),
        lock_reasons=lock_reasons,
    )


def _tournament_teams_snapshot(
    teams: tuple[TeamSummary, ...],
) -> dict[str, object]:
    return {
        "teams": [{"id": team.id, "name": team.name} for team in teams],
    }


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
        allow_no_matches=is_enabled,
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


def _get_swiss_stage_prediction_details(
    connection,
    *,
    contest_id: int,
    telegram_user_id: int | None,
    now_utc: datetime,
) -> SwissStagePredictionDetails:
    configuration_row = connection.execute(
        """
        SELECT
            contests.is_active,
            contests.template_key,
            COALESCE(swiss_stage_prediction_settings.enabled, 0) AS enabled,
            swiss_stage_prediction_settings.deadline_at,
            COALESCE(
                swiss_stage_prediction_settings.direct_qualifier_count,
                3
            ) AS direct_qualifier_count,
            COALESCE(
                swiss_stage_prediction_settings.elimination_qualifier_count,
                5
            ) AS elimination_qualifier_count,
            COALESCE(
                swiss_stage_prediction_settings.selection_mode,
                'exact'
            ) AS selection_mode,
            COALESCE(
                swiss_stage_prediction_settings.direct_correct_points,
                2
            ) AS direct_correct_points,
            COALESCE(
                swiss_stage_prediction_settings.elimination_correct_points,
                2
            ) AS elimination_correct_points,
            COALESCE(
                swiss_stage_prediction_settings.cross_category_points,
                1
            ) AS cross_category_points
        FROM contests
        LEFT JOIN swiss_stage_prediction_settings
            ON swiss_stage_prediction_settings.contest_id = contests.id
        WHERE contests.id = ?
        """,
        (contest_id,),
    ).fetchone()
    if configuration_row is None:
        raise RuntimeError("Не удалось найти настройки прогноза на швейцарский этап.")

    candidates = tuple(
        _team_summary_from_row(row)
        for row in _get_swiss_stage_candidate_rows(
            connection,
            contest_id=contest_id,
        )
    )
    prediction_row = connection.execute(
        """
        SELECT swiss_stage_predictions.id
        FROM swiss_stage_predictions
        JOIN users ON users.id = swiss_stage_predictions.user_id
        WHERE swiss_stage_predictions.contest_id = ?
            AND users.telegram_user_id = ?
        """,
        (contest_id, telegram_user_id),
    ).fetchone()
    prediction_rows = connection.execute(
        """
        SELECT
            teams.id,
            teams.name,
            swiss_stage_prediction_selections.category
        FROM swiss_stage_predictions
        JOIN users
            ON users.id = swiss_stage_predictions.user_id
        JOIN swiss_stage_prediction_selections
            ON swiss_stage_prediction_selections.prediction_id =
            swiss_stage_predictions.id
        JOIN teams
            ON teams.id = swiss_stage_prediction_selections.team_id
        WHERE swiss_stage_predictions.contest_id = ?
            AND users.telegram_user_id = ?
        ORDER BY
            swiss_stage_prediction_selections.category,
            teams.name COLLATE NOCASE,
            teams.id
        """,
        (contest_id, telegram_user_id),
    ).fetchall()
    result_rows = connection.execute(
        """
        SELECT
            teams.id,
            teams.name,
            swiss_stage_result_selections.category
        FROM swiss_stage_result_selections
        JOIN teams
            ON teams.id = swiss_stage_result_selections.team_id
        WHERE swiss_stage_result_selections.contest_id = ?
        ORDER BY
            swiss_stage_result_selections.category,
            teams.name COLLATE NOCASE,
            teams.id
        """,
        (contest_id,),
    ).fetchall()
    result_exists = (
        connection.execute(
            "SELECT 1 FROM swiss_stage_results WHERE contest_id = ?",
            (contest_id,),
        ).fetchone()
        is not None
    )
    selection_mode = str(configuration_row["selection_mode"])
    direct_qualifier_count = int(configuration_row["direct_qualifier_count"])
    elimination_qualifier_count = int(configuration_row["elimination_qualifier_count"])
    direct_correct_points = int(configuration_row["direct_correct_points"])
    elimination_correct_points = int(configuration_row["elimination_correct_points"])
    cross_category_points = int(configuration_row["cross_category_points"])
    prediction = _swiss_stage_selection_from_rows(
        prediction_rows,
        candidates=candidates,
        selection_exists=prediction_row is not None,
        selection_mode=selection_mode,
        direct_qualifier_count=direct_qualifier_count,
        elimination_qualifier_count=elimination_qualifier_count,
    )
    actual_result = _swiss_stage_selection_from_rows(
        result_rows,
        candidates=candidates,
        selection_exists=result_exists,
        selection_mode=selection_mode,
        direct_qualifier_count=direct_qualifier_count,
        elimination_qualifier_count=elimination_qualifier_count,
    )
    awards = _swiss_stage_awards_from_rows(
        prediction_rows,
        result_rows=result_rows,
        selection_mode=selection_mode,
        direct_correct_points=direct_correct_points,
        elimination_correct_points=elimination_correct_points,
        cross_category_points=cross_category_points,
    )
    score_breakdown = (
        _swiss_stage_score_breakdown(awards)
        if prediction is not None and actual_result is not None
        else None
    )
    deadline_at = configuration_row["deadline_at"]
    deadline_at_value = str(deadline_at) if deadline_at is not None else None
    is_enabled = bool(configuration_row["enabled"])

    return SwissStagePredictionDetails(
        is_enabled=is_enabled,
        deadline_at=deadline_at_value,
        direct_qualifier_count=direct_qualifier_count,
        elimination_qualifier_count=elimination_qualifier_count,
        selection_mode=selection_mode,
        direct_correct_points=direct_correct_points,
        elimination_correct_points=elimination_correct_points,
        cross_category_points=cross_category_points,
        maximum_points=(
            direct_qualifier_count * direct_correct_points
            + elimination_qualifier_count * elimination_correct_points
        ),
        candidates=candidates,
        prediction=prediction,
        actual_result=actual_result,
        is_open=(
            is_enabled
            and bool(configuration_row["is_active"])
            and deadline_at_value is not None
            and _is_swiss_stage_prediction_open(
                deadline_at_value,
                now_utc=now_utc,
            )
        ),
        settings_locked=_are_swiss_stage_settings_locked(
            connection,
            contest_id=contest_id,
        ),
        awarded_points=(score_breakdown.total_points if score_breakdown else None),
        awards=awards if actual_result is not None else (),
        score_breakdown=score_breakdown,
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
        FROM contest_teams
        JOIN teams ON teams.id = contest_teams.team_id
        WHERE contest_teams.contest_id = ?
        ORDER BY contest_teams.position, teams.id
        """,
        (contest_id,),
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
        FROM contest_teams
        JOIN teams ON teams.id = contest_teams.team_id
        WHERE contest_teams.contest_id = ?
          AND teams.id = ?
        """,
        (contest_id, team_id),
    ).fetchone()


def _get_swiss_stage_configuration_row(
    connection,
    *,
    contest_id: int,
):
    return connection.execute(
        """
        SELECT
            contest_id,
            enabled,
            deadline_at,
            direct_qualifier_count,
            elimination_qualifier_count,
            selection_mode,
            direct_correct_points,
            elimination_correct_points,
            cross_category_points
        FROM swiss_stage_prediction_settings
        WHERE contest_id = ?
        """,
        (contest_id,),
    ).fetchone()


def _require_enabled_swiss_stage_configuration(
    connection,
    *,
    contest_id: int,
    result_operation: bool = False,
    template_key: str,
):
    row = _get_swiss_stage_configuration_row(
        connection,
        contest_id=contest_id,
    )
    if row is None or not bool(row["enabled"]):
        stage_name, _stage_genitive, _stage_prepositional = _swiss_stage_terms(
            template_key
        )
        if result_operation:
            raise SwissStageResultUnavailableError(
                f"Сначала включите прогноз на {stage_name}."
            )
        raise PredictionUnavailableError(
            f"Прогноз на {stage_name} в этом конкурсе выключен."
        )
    return row


def _get_swiss_stage_candidate_rows(connection, *, contest_id: int):
    return connection.execute(
        """
        SELECT teams.id, teams.name
        FROM contest_teams
        JOIN teams ON teams.id = contest_teams.team_id
        WHERE contest_teams.contest_id = ?
        ORDER BY
            contest_teams.position,
            teams.id
        """,
        (contest_id,),
    ).fetchall()


def _are_swiss_stage_settings_locked(connection, *, contest_id: int) -> bool:
    row = connection.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM swiss_stage_predictions
                WHERE contest_id = ?
            )
            OR EXISTS (
                SELECT 1
                FROM swiss_stage_results
                WHERE contest_id = ?
            ) AS is_locked
        """,
        (contest_id, contest_id),
    ).fetchone()
    return bool(row["is_locked"]) if row is not None else False


def _swiss_stage_snapshot(
    connection,
    *,
    contest_id: int,
) -> dict[str, object] | None:
    configuration_row = _get_swiss_stage_configuration_row(
        connection,
        contest_id=contest_id,
    )
    if configuration_row is None:
        return None
    result_rows = connection.execute(
        """
        SELECT team_id, category
        FROM swiss_stage_result_selections
        WHERE contest_id = ?
        ORDER BY category, team_id
        """,
        (contest_id,),
    ).fetchall()
    return {
        "enabled": bool(configuration_row["enabled"]),
        "deadline_at": configuration_row["deadline_at"],
        "direct_qualifier_count": int(configuration_row["direct_qualifier_count"]),
        "elimination_qualifier_count": int(
            configuration_row["elimination_qualifier_count"]
        ),
        "selection_mode": str(configuration_row["selection_mode"]),
        "direct_correct_points": int(configuration_row["direct_correct_points"]),
        "elimination_correct_points": int(
            configuration_row["elimination_correct_points"]
        ),
        "cross_category_points": int(configuration_row["cross_category_points"]),
        "teams": [
            {"id": team.id, "name": team.name}
            for team in (
                _team_summary_from_row(row)
                for row in _get_swiss_stage_candidate_rows(
                    connection,
                    contest_id=contest_id,
                )
            )
        ],
        "actual_result": {
            "direct_team_ids": [
                int(row["team_id"])
                for row in result_rows
                if row["category"] == "direct"
            ],
            "elimination_team_ids": [
                int(row["team_id"])
                for row in result_rows
                if row["category"] == "elimination"
            ],
        }
        if result_rows
        else None,
    }


def _validate_swiss_stage_selection(
    connection,
    *,
    contest_id: int,
    direct_team_ids: tuple[int, ...],
    elimination_team_ids: tuple[int, ...],
    direct_qualifier_count: int,
    elimination_qualifier_count: int,
    selection_mode: SwissStageSelectionMode,
    require_complete: bool,
    template_key: str,
) -> None:
    _stage_name, _stage_genitive, stage_prepositional = _swiss_stage_terms(template_key)
    exact_selection_required = require_complete or selection_mode == "exact"
    direct_count_is_invalid = (
        len(direct_team_ids) != direct_qualifier_count
        if exact_selection_required
        else len(direct_team_ids) > direct_qualifier_count
    )
    if direct_count_is_invalid:
        direct_category = (
            "выхода напрямую в 1/8"
            if template_key == CHAMPIONS_LEAGUE_2026_27_TEMPLATE.key
            else "прямого прохода"
        )
        quantity = "ровно" if exact_selection_required else "не более"
        raise ValueError(
            f"Выберите {quantity} {direct_qualifier_count} команд для {direct_category}."
        )
    elimination_count_is_invalid = (
        len(elimination_team_ids) != elimination_qualifier_count
        if exact_selection_required
        else len(elimination_team_ids) > elimination_qualifier_count
    )
    if elimination_count_is_invalid:
        elimination_category = (
            "вылета после общего этапа"
            if template_key == CHAMPIONS_LEAGUE_2026_27_TEMPLATE.key
            else "элиминейшн-раунда"
        )
        quantity = "ровно" if exact_selection_required else "не более"
        raise ValueError(
            f"Выберите {quantity} {elimination_qualifier_count} команд "
            f"для {elimination_category}."
        )
    selected_ids = (*direct_team_ids, *elimination_team_ids)
    if not selected_ids:
        return
    placeholders = ", ".join("?" for _ in selected_ids)
    candidate_count = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM contest_teams
        WHERE contest_id = ?
            AND team_id IN ({placeholders})
        """,
        (contest_id, *selected_ids),
    ).fetchone()[0]
    if int(candidate_count) != len(selected_ids):
        raise ValueError(
            f"Все выбранные команды должны участвовать в {stage_prepositional}."
        )


def _insert_swiss_stage_selections(
    connection,
    *,
    table_name: str,
    owner_column: str | None,
    owner_id: int | None,
    contest_id: int,
    direct_team_ids: tuple[int, ...],
    elimination_team_ids: tuple[int, ...],
) -> None:
    selections = (
        *((team_id, "direct") for team_id in direct_team_ids),
        *((team_id, "elimination") for team_id in elimination_team_ids),
    )
    if table_name == "swiss_stage_prediction_selections":
        if owner_column != "prediction_id" or owner_id is None:
            raise RuntimeError("Некорректная запись прогноза швейцарского этапа.")
        connection.executemany(
            """
            INSERT INTO swiss_stage_prediction_selections (
                prediction_id,
                contest_id,
                team_id,
                category
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                (owner_id, contest_id, team_id, category)
                for team_id, category in selections
            ],
        )
        return
    if table_name != "swiss_stage_result_selections" or owner_column is not None:
        raise RuntimeError("Некорректная запись результата швейцарского этапа.")
    connection.executemany(
        """
        INSERT INTO swiss_stage_result_selections (
            contest_id,
            team_id,
            category
        )
        VALUES (?, ?, ?)
        """,
        [(contest_id, team_id, category) for team_id, category in selections],
    )


def _swiss_stage_selection_from_ids(
    connection,
    *,
    contest_id: int,
    selection_mode: str,
    direct_qualifier_count: int,
    elimination_qualifier_count: int,
    direct_team_ids: tuple[int, ...],
    elimination_team_ids: tuple[int, ...],
) -> SwissStageSelection:
    selected_ids = (*direct_team_ids, *elimination_team_ids)
    if selected_ids:
        placeholders = ", ".join("?" for _ in selected_ids)
        rows = connection.execute(
            f"""
            SELECT id, name
            FROM teams
            WHERE id IN ({placeholders})
            """,
            selected_ids,
        ).fetchall()
    else:
        rows = ()
    teams_by_id = {int(row["id"]): _team_summary_from_row(row) for row in rows}
    is_complete = _is_swiss_stage_selection_complete(
        direct_team_ids=direct_team_ids,
        elimination_team_ids=elimination_team_ids,
        direct_qualifier_count=direct_qualifier_count,
        elimination_qualifier_count=elimination_qualifier_count,
    )
    playoff_teams = _swiss_stage_playoff_teams(
        tuple(
            _team_summary_from_row(row)
            for row in _get_swiss_stage_candidate_rows(
                connection,
                contest_id=contest_id,
            )
        ),
        direct_team_ids=direct_team_ids,
        elimination_team_ids=elimination_team_ids,
        selection_mode=selection_mode,
        is_complete=is_complete,
    )
    return SwissStageSelection(
        direct_teams=tuple(teams_by_id[team_id] for team_id in direct_team_ids),
        playoff_teams=playoff_teams,
        elimination_teams=tuple(
            teams_by_id[team_id] for team_id in elimination_team_ids
        ),
        is_complete=is_complete,
    )


def _swiss_stage_selection_from_rows(
    rows,
    *,
    candidates: tuple[TeamSummary, ...],
    selection_exists: bool,
    selection_mode: str,
    direct_qualifier_count: int,
    elimination_qualifier_count: int,
) -> SwissStageSelection | None:
    if not selection_exists:
        return None
    direct_teams: list[TeamSummary] = []
    elimination_teams: list[TeamSummary] = []
    for row in rows:
        team = _team_summary_from_row(row)
        if row["category"] == "direct":
            direct_teams.append(team)
        else:
            elimination_teams.append(team)
    direct_team_ids = tuple(team.id for team in direct_teams)
    elimination_team_ids = tuple(team.id for team in elimination_teams)
    is_complete = _is_swiss_stage_selection_complete(
        direct_team_ids=direct_team_ids,
        elimination_team_ids=elimination_team_ids,
        direct_qualifier_count=direct_qualifier_count,
        elimination_qualifier_count=elimination_qualifier_count,
    )
    return SwissStageSelection(
        direct_teams=tuple(direct_teams),
        playoff_teams=_swiss_stage_playoff_teams(
            candidates,
            direct_team_ids=direct_team_ids,
            elimination_team_ids=elimination_team_ids,
            selection_mode=selection_mode,
            is_complete=is_complete,
        ),
        elimination_teams=tuple(elimination_teams),
        is_complete=is_complete,
    )


def _is_swiss_stage_selection_complete(
    *,
    direct_team_ids: tuple[int, ...],
    elimination_team_ids: tuple[int, ...],
    direct_qualifier_count: int,
    elimination_qualifier_count: int,
) -> bool:
    return (
        len(direct_team_ids) == direct_qualifier_count
        and len(elimination_team_ids) == elimination_qualifier_count
    )


def _swiss_stage_playoff_teams(
    candidates: tuple[TeamSummary, ...],
    *,
    direct_team_ids: tuple[int, ...],
    elimination_team_ids: tuple[int, ...],
    selection_mode: str,
    is_complete: bool,
) -> tuple[TeamSummary, ...]:
    if selection_mode != "up_to_limits" or not is_complete:
        return ()
    selected_ids = {*direct_team_ids, *elimination_team_ids}
    return tuple(team for team in candidates if team.id not in selected_ids)


def _swiss_stage_awards_from_rows(
    prediction_rows,
    *,
    result_rows,
    selection_mode: str,
    direct_correct_points: int,
    elimination_correct_points: int,
    cross_category_points: int,
) -> tuple[SwissStageTeamAward, ...]:
    actual_categories = {int(row["id"]): str(row["category"]) for row in result_rows}
    has_result = bool(result_rows)
    awards: list[SwissStageTeamAward] = []
    for row in prediction_rows:
        predicted_category = str(row["category"])
        actual_category = actual_categories.get(int(row["id"]))
        if has_result and actual_category is None and selection_mode == "up_to_limits":
            actual_category = "playoff"
        points = None
        if has_result:
            points = calculate_swiss_stage_selection_points(
                predicted_category=predicted_category,
                actual_category=actual_category,
                direct_correct_points=direct_correct_points,
                elimination_correct_points=elimination_correct_points,
                cross_category_points=cross_category_points,
            )
        awards.append(
            SwissStageTeamAward(
                team=_team_summary_from_row(row),
                predicted_category=predicted_category,
                actual_category=actual_category,
                points=points,
            )
        )
    return tuple(awards)


def _swiss_stage_score_breakdown(
    awards: tuple[SwissStageTeamAward, ...],
) -> SwissStageScoreBreakdown:
    correct_direct_count = sum(
        award.predicted_category == "direct" and award.actual_category == "direct"
        for award in awards
    )
    direct_points = sum(
        award.points or 0 for award in awards if award.predicted_category == "direct"
    )
    correct_elimination_count = sum(
        award.predicted_category == "elimination"
        and award.actual_category == "elimination"
        for award in awards
    )
    elimination_points = sum(
        award.points or 0
        for award in awards
        if award.predicted_category == "elimination"
    )
    return SwissStageScoreBreakdown(
        correct_direct_count=correct_direct_count,
        direct_points=direct_points,
        correct_elimination_count=correct_elimination_count,
        elimination_points=elimination_points,
        total_points=direct_points + elimination_points,
    )


def _is_contest_completed(
    connection,
    *,
    contest_id: int,
    allow_no_matches: bool = False,
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

    return (
        allow_no_matches or total_matches > 0
    ) and total_matches == completed_matches


def _validate_existing_deadline_change(
    *,
    previous_deadline_at: str | None,
    new_deadline_at: str | None,
    now_utc: datetime,
    locked_error: ValueError,
    past_deadline_message: str,
) -> None:
    if previous_deadline_at is None:
        return

    previous_deadline_utc = datetime.fromisoformat(
        previous_deadline_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if previous_deadline_utc <= now_utc:
        raise locked_error

    if new_deadline_at is None:
        return

    new_deadline_utc = datetime.fromisoformat(
        new_deadline_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if new_deadline_utc <= now_utc:
        raise ValueError(past_deadline_message)


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


def _normalize_swiss_stage_enabled(value: bool) -> bool:
    if not isinstance(value, bool):
        raise ValueError(
            "Настройка прогноза на швейцарский этап должна быть логическим значением."
        )
    return value


def _normalize_swiss_stage_limit(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} должно быть положительным целым числом.")
    return value


def _normalize_swiss_stage_team_id_sets(
    direct_team_ids: list[int],
    elimination_team_ids: list[int],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    direct_ids = _normalize_swiss_stage_team_ids(
        direct_team_ids,
        field_name="Команды прямого прохода",
    )
    elimination_ids = _normalize_swiss_stage_team_ids(
        elimination_team_ids,
        field_name="Команды второй категории",
    )
    if set(direct_ids) & set(elimination_ids):
        raise ValueError("Одна команда не может находиться в обеих категориях.")
    return direct_ids, elimination_ids


def _normalize_swiss_stage_team_ids(
    values: list[int],
    *,
    field_name: str,
) -> tuple[int, ...]:
    if not isinstance(values, list):
        raise ValueError(f"{field_name} должны быть массивом идентификаторов.")
    normalized_ids: list[int] = []
    seen_ids: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} должны содержать идентификаторы команд.")
        if value in seen_ids:
            raise ValueError("Команда не должна повторяться в одной категории.")
        seen_ids.add(value)
        normalized_ids.append(value)
    return tuple(normalized_ids)


def _swiss_stage_terms(template_key: str) -> tuple[str, str, str]:
    if template_key == CHAMPIONS_LEAGUE_2026_27_TEMPLATE.key:
        return "общий этап", "общего этапа", "общем этапе"
    return "швейцарский этап", "швейцарского этапа", "швейцарском этапе"


def _normalize_swiss_stage_deadline_at(
    value: str,
    *,
    stage_name: str = "швейцарский этап",
) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"Укажите, когда прогноз на {stage_name} закрывается.")
    try:
        parsed_value = datetime.fromisoformat(normalized_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"Некорректная дата и время закрытия прогноза на {stage_name}."
        ) from error
    if parsed_value.tzinfo is None or parsed_value.utcoffset() is None:
        raise ValueError(
            f"Дата и время закрытия прогноза на {stage_name} должны "
            "содержать часовой пояс."
        )
    return (
        parsed_value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _is_swiss_stage_prediction_open(
    deadline_at: str,
    *,
    now_utc: datetime,
) -> bool:
    try:
        deadline_utc = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(
            "У конкурса сохранён некорректный дедлайн прогноза на швейцарский этап."
        ) from error
    if deadline_utc.tzinfo is None or deadline_utc.utcoffset() is None:
        raise RuntimeError(
            "У конкурса сохранён дедлайн прогноза на швейцарский этап "
            "без часового пояса."
        )
    return deadline_utc.astimezone(timezone.utc) > now_utc


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


def _get_or_create_stage(
    connection,
    *,
    competition_id: int,
    round_key: str | None,
) -> tuple[int, str, str]:
    if round_key is None:
        return _get_or_create_first_stage(
            connection,
            competition_id=competition_id,
        )

    round_definition = CHAMPIONS_LEAGUE_KNOCKOUT_ROUNDS.get(round_key)
    if round_definition is None:  # pragma: no cover - normalized by the caller
        raise ValueError("Неизвестный раунд плей-офф Лиги чемпионов.")
    round_name, round_position, stage_type = round_definition
    row = connection.execute(
        """
        SELECT id, name, stage_type
        FROM stages
        WHERE competition_id = ? AND stage_key = ?
        """,
        (competition_id, round_key),
    ).fetchone()
    if row is not None:
        return int(row["id"]), str(row["name"]), str(row["stage_type"])

    stage_id = int(
        connection.execute(
            """
            INSERT INTO stages (
                competition_id, name, position, stage_type, stage_key
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                competition_id,
                round_name,
                round_position,
                stage_type,
                round_key,
            ),
        ).lastrowid
    )
    return stage_id, round_name, stage_type


def _normalize_knockout_round_key(
    *,
    template_key: str,
    round_key: str | None,
    is_two_legged: bool,
) -> str | None:
    if template_key != CHAMPIONS_LEAGUE_2026_27_TEMPLATE.key:
        if round_key is not None:
            raise ValueError(
                "Названия раундов доступны только для Лиги чемпионов 2026/27."
            )
        return None

    normalized = round_key.strip() if isinstance(round_key, str) else ""
    if not normalized:
        normalized = "playoff" if is_two_legged else "final"
    if normalized not in CHAMPIONS_LEAGUE_KNOCKOUT_ROUNDS:
        raise ValueError("Неизвестный раунд плей-офф Лиги чемпионов.")
    if is_two_legged and normalized == "final":
        raise ValueError("Финал Лиги чемпионов состоит из одного матча.")
    if not is_two_legged and normalized != "final":
        raise ValueError("Стыки, 1/8, 1/4 и 1/2 финала создаются двухматчевыми парами.")
    return normalized


def _get_next_tie_position(
    connection,
    *,
    stage_id: int,
    round_key: str | None = None,
    conflict_error_type: type[MatchCreationConflictError] = MatchCreationConflictError,
) -> int:
    capacity = CHAMPIONS_LEAGUE_KNOCKOUT_ROUND_CAPACITIES.get(round_key or "")
    if capacity is not None:
        occupied_rows = connection.execute(
            "SELECT position FROM ties WHERE stage_id = ?",
            (stage_id,),
        ).fetchall()
        occupied_positions = {int(row["position"]) for row in occupied_rows}
        for position in range(1, capacity + 1):
            if position not in occupied_positions:
                return position
        round_name = CHAMPIONS_LEAGUE_KNOCKOUT_ROUNDS[round_key][0]
        raise conflict_error_type(f"В раунде «{round_name}» уже заполнены все позиции.")

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


def _normalize_tournament_team_names(values: list[str]) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("Список команд должен быть массивом.")
    normalized_names: list[str] = []
    normalized_keys: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Название команды должно быть строкой.")
        if not value.strip():
            continue
        name = _normalize_team_name(value, field_name="Название команды")
        key = name.casefold()
        if key in normalized_keys:
            raise ValueError("Команда не должна повторяться без учёта регистра.")
        normalized_keys.add(key)
        normalized_names.append(name)
    if not normalized_names:
        raise ValueError("Добавьте хотя бы одну команду турнира.")
    return tuple(normalized_names)


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


def _normalize_team_id(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} должна быть идентификатором команды.")
    return value


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
    home_team_id: int,
    away_team_id: int,
    starts_at_utc: str,
    best_of: int | None,
    round_key: str | None,
) -> str:
    payload = json.dumps(
        {
            "away_team_id": away_team_id,
            "best_of": best_of,
            "home_team_id": home_team_id,
            "starts_at_utc": starts_at_utc,
            "round_key": round_key,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_two_legged_tie_request_fingerprint(
    *,
    first_team_id: int,
    second_team_id: int,
    first_leg_starts_at_utc: str,
    second_leg_starts_at_utc: str,
    round_key: str | None,
) -> str:
    payload = json.dumps(
        {
            "kind": "two_legged_tie",
            "first_leg_starts_at_utc": first_leg_starts_at_utc,
            "first_team_id": first_team_id,
            "second_leg_starts_at_utc": second_leg_starts_at_utc,
            "second_team_id": second_team_id,
            "round_key": round_key,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _two_legged_tie_summary_from_row(
    row,
    *,
    now_utc: datetime,
) -> TwoLeggedTieSummary:
    result = _two_legged_tie_result_from_row(row)
    prediction = (
        TwoLeggedTiePrediction(
            advancing_team_id=int(row["predicted_advancing_team_id"]),
        )
        if row["predicted_advancing_team_id"] is not None
        else None
    )
    awarded_points = None
    if result is not None and prediction is not None:
        awarded_points = (
            int(row["advancing_team_points"])
            if row["advancing_team_points"] is not None
            else 0
        )
    return TwoLeggedTieSummary(
        id=int(row["id"]),
        name=str(row["name"]),
        first_team_id=int(row["first_team_id"]),
        first_team_name=str(row["first_team_name"]),
        second_team_id=int(row["second_team_id"]),
        second_team_name=str(row["second_team_name"]),
        first_leg_match_id=int(row["first_leg_match_id"]),
        second_leg_match_id=int(row["second_leg_match_id"]),
        prediction_deadline_at=str(row["prediction_deadline_at"]),
        is_prediction_open=_is_two_legged_tie_prediction_open(
            row,
            now_utc=now_utc,
        ),
        round_key=_optional_row_string(row, "round_key"),
        round_name=_optional_row_string(row, "round_name"),
        round_position=_optional_row_integer(row, "round_position"),
        bracket_position=_optional_row_integer(row, "bracket_position"),
        result=result,
        prediction=prediction,
        awarded_points=awarded_points,
    )


def _two_legged_tie_result_from_row(row) -> TwoLeggedTieResult | None:
    if row["advancing_team_id"] is None or row["resolution_method"] is None:
        return None
    if not _two_legged_tie_has_both_match_results(row):
        return None
    resolution = _resolve_two_legged_tie_row(
        row,
        second_leg_extra_time_home_score=row["second_leg_extra_time_home_score"],
        second_leg_extra_time_away_score=row["second_leg_extra_time_away_score"],
        second_leg_home_penalty_score=row["second_leg_home_penalty_score"],
        second_leg_away_penalty_score=row["second_leg_away_penalty_score"],
        advancing_team_id=int(row["advancing_team_id"]),
    )
    if resolution.resolution_method != str(row["resolution_method"]):
        raise RuntimeError("Способ определения победителя противостояния повреждён.")
    return _two_legged_tie_result_from_resolution(
        resolution,
        second_leg_extra_time_home_score=row["second_leg_extra_time_home_score"],
        second_leg_extra_time_away_score=row["second_leg_extra_time_away_score"],
        second_leg_home_penalty_score=row["second_leg_home_penalty_score"],
        second_leg_away_penalty_score=row["second_leg_away_penalty_score"],
    )


def _resolve_two_legged_tie_row(
    row,
    *,
    second_leg_extra_time_home_score: int | None,
    second_leg_extra_time_away_score: int | None,
    second_leg_home_penalty_score: int | None,
    second_leg_away_penalty_score: int | None,
    advancing_team_id: int | None,
) -> TwoLeggedTieResolution:
    return resolve_two_legged_tie_result(
        first_team_id=int(row["first_team_id"]),
        second_team_id=int(row["second_team_id"]),
        first_leg_home_team_id=int(row["first_leg_home_team_id"]),
        first_leg_away_team_id=int(row["first_leg_away_team_id"]),
        first_leg_home_score=int(row["first_leg_home_score"]),
        first_leg_away_score=int(row["first_leg_away_score"]),
        second_leg_home_team_id=int(row["second_leg_home_team_id"]),
        second_leg_away_team_id=int(row["second_leg_away_team_id"]),
        second_leg_home_score=int(row["second_leg_home_score"]),
        second_leg_away_score=int(row["second_leg_away_score"]),
        second_leg_extra_time_home_score=second_leg_extra_time_home_score,
        second_leg_extra_time_away_score=second_leg_extra_time_away_score,
        second_leg_home_penalty_score=second_leg_home_penalty_score,
        second_leg_away_penalty_score=second_leg_away_penalty_score,
        advancing_team_id=advancing_team_id,
    )


def _two_legged_tie_result_from_resolution(
    resolution: TwoLeggedTieResolution,
    *,
    second_leg_extra_time_home_score: int | None,
    second_leg_extra_time_away_score: int | None,
    second_leg_home_penalty_score: int | None,
    second_leg_away_penalty_score: int | None,
) -> TwoLeggedTieResult:
    return TwoLeggedTieResult(
        aggregate_first_team_score=resolution.aggregate_first_team_score,
        aggregate_second_team_score=resolution.aggregate_second_team_score,
        advancing_team_id=resolution.advancing_team_id,
        resolution_method=resolution.resolution_method,
        second_leg_extra_time_home_score=second_leg_extra_time_home_score,
        second_leg_extra_time_away_score=second_leg_extra_time_away_score,
        second_leg_home_penalty_score=second_leg_home_penalty_score,
        second_leg_away_penalty_score=second_leg_away_penalty_score,
    )


def _two_legged_tie_has_both_match_results(row) -> bool:
    return all(
        row[key] is not None
        for key in (
            "first_leg_home_score",
            "first_leg_away_score",
            "second_leg_home_score",
            "second_leg_away_score",
        )
    )


def _reconcile_two_legged_tie_after_match_result(
    connection,
    *,
    tie_id: int,
) -> None:
    row = _get_two_legged_tie_row(
        connection,
        contest_id=None,
        tie_id=tie_id,
        user_id=None,
    )
    if row is None:
        raise RuntimeError("Не удалось найти двухматчевое противостояние.")
    if not _two_legged_tie_has_both_match_results(row):
        _clear_two_legged_tie_result(connection, tie_id=tie_id)
        return

    aggregate_first, aggregate_second = _two_legged_aggregate_scores(row)
    if aggregate_first == aggregate_second:
        if row["second_leg_extra_time_home_score"] is None:
            _clear_two_legged_tie_result(connection, tie_id=tie_id)
            return
        resolution = _resolve_two_legged_tie_row(
            row,
            second_leg_extra_time_home_score=row["second_leg_extra_time_home_score"],
            second_leg_extra_time_away_score=row["second_leg_extra_time_away_score"],
            second_leg_home_penalty_score=row["second_leg_home_penalty_score"],
            second_leg_away_penalty_score=row["second_leg_away_penalty_score"],
            advancing_team_id=None,
        )
        connection.execute(
            """
            UPDATE ties
            SET advancing_team_id = ?, resolution_method = ?
            WHERE id = ?
            """,
            (resolution.advancing_team_id, resolution.resolution_method, tie_id),
        )
        return

    resolution = _resolve_two_legged_tie_row(
        row,
        second_leg_extra_time_home_score=None,
        second_leg_extra_time_away_score=None,
        second_leg_home_penalty_score=None,
        second_leg_away_penalty_score=None,
        advancing_team_id=None,
    )
    connection.execute(
        """
        UPDATE ties
        SET advancing_team_id = ?, resolution_method = 'aggregate',
            second_leg_extra_time_home_score = NULL,
            second_leg_extra_time_away_score = NULL,
            second_leg_home_penalty_score = NULL,
            second_leg_away_penalty_score = NULL
        WHERE id = ?
        """,
        (resolution.advancing_team_id, tie_id),
    )


def _clear_two_legged_tie_result(connection, *, tie_id: int) -> None:
    connection.execute(
        """
        UPDATE ties
        SET advancing_team_id = NULL, resolution_method = NULL,
            second_leg_extra_time_home_score = NULL,
            second_leg_extra_time_away_score = NULL,
            second_leg_home_penalty_score = NULL,
            second_leg_away_penalty_score = NULL
        WHERE id = ?
        """,
        (tie_id,),
    )


def _two_legged_aggregate_scores(row) -> tuple[int, int]:
    first_team_id = int(row["first_team_id"])
    second_team_id = int(row["second_team_id"])

    def score_for(team_id: int, *, leg_prefix: str) -> int:
        return int(
            row[f"{leg_prefix}_home_score"]
            if int(row[f"{leg_prefix}_home_team_id"]) == team_id
            else row[f"{leg_prefix}_away_score"]
        )

    return (
        score_for(first_team_id, leg_prefix="first_leg")
        + score_for(first_team_id, leg_prefix="second_leg"),
        score_for(second_team_id, leg_prefix="first_leg")
        + score_for(second_team_id, leg_prefix="second_leg"),
    )


def _two_legged_tie_result_snapshot(result: TwoLeggedTieResult) -> dict[str, object]:
    return {
        "aggregate_first_team_score": result.aggregate_first_team_score,
        "aggregate_second_team_score": result.aggregate_second_team_score,
        "advancing_team_id": result.advancing_team_id,
        "resolution_method": result.resolution_method,
        "second_leg_extra_time_home_score": (result.second_leg_extra_time_home_score),
        "second_leg_extra_time_away_score": (result.second_leg_extra_time_away_score),
        "second_leg_home_penalty_score": result.second_leg_home_penalty_score,
        "second_leg_away_penalty_score": result.second_leg_away_penalty_score,
    }


def _match_result_from_row(row) -> MatchResult | None:
    if (
        "home_score_final" not in row.keys()
        or "away_score_final" not in row.keys()
        or row["home_score_final"] is None
        or row["away_score_final"] is None
    ):
        return None

    is_two_legged = "is_two_legged" in row.keys() and bool(row["is_two_legged"])
    if not is_two_legged and (
        "advancing_team_id" not in row.keys() or row["advancing_team_id"] is None
    ):
        return None

    return MatchResult(
        home_score=int(row["home_score_final"]),
        away_score=int(row["away_score_final"]),
        advancing_team_id=(None if is_two_legged else int(row["advancing_team_id"])),
    )


def _match_summary_from_row(row) -> MatchSummary:
    result = _match_result_from_row(row)
    prediction = None

    if (
        "predicted_home_score" in row.keys()
        and "predicted_away_score" in row.keys()
        and row["predicted_home_score"] is not None
        and row["predicted_away_score"] is not None
    ):
        is_two_legged = "is_two_legged" in row.keys() and bool(row["is_two_legged"])
        if is_two_legged or (
            "predicted_advancing_team_id" in row.keys()
            and row["predicted_advancing_team_id"] is not None
        ):
            prediction = MatchPrediction(
                home_score=int(row["predicted_home_score"]),
                away_score=int(row["predicted_away_score"]),
                advancing_team_id=(
                    None if is_two_legged else int(row["predicted_advancing_team_id"])
                ),
            )

    prediction_score = _match_prediction_score_from_row(
        row,
        result=result,
        prediction=prediction,
    )

    return MatchSummary(
        id=int(row["id"]),
        tie_id=int(row["tie_id"]),
        is_two_legged=(
            bool(row["is_two_legged"]) if "is_two_legged" in row.keys() else False
        ),
        leg_number=(
            int(row["leg_number"])
            if "leg_number" in row.keys() and row["leg_number"] is not None
            else None
        ),
        best_of=(
            int(row["best_of"])
            if "best_of" in row.keys() and row["best_of"] is not None
            else None
        ),
        home_team_id=int(row["home_team_id"]),
        home_team_name=str(row["home_team_name"]),
        away_team_id=int(row["away_team_id"]),
        away_team_name=str(row["away_team_name"]),
        starts_at_utc=str(row["starts_at_utc"]),
        status=str(row["status"]),
        round_key=_optional_row_string(row, "round_key"),
        round_name=_optional_row_string(row, "round_name"),
        round_position=_optional_row_integer(row, "round_position"),
        bracket_position=_optional_row_integer(row, "bracket_position"),
        result=result,
        prediction=prediction,
        prediction_score=prediction_score,
    )


def _optional_row_string(row, key: str) -> str | None:
    return str(row[key]) if key in row.keys() and row[key] is not None else None


def _optional_row_integer(row, key: str) -> int | None:
    return int(row[key]) if key in row.keys() and row[key] is not None else None


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
        not ("is_two_legged" in row.keys() and bool(row["is_two_legged"]))
        and "advancing_team_points" in row.keys()
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


def _get_swiss_stage_prediction_history_rows(
    connection,
    *,
    contest_id: int,
):
    prediction_rows = connection.execute(
        """
        SELECT
            swiss_stage_predictions.user_id,
            teams.id,
            teams.name,
            swiss_stage_prediction_selections.category
        FROM swiss_stage_predictions
        LEFT JOIN swiss_stage_prediction_selections
            ON swiss_stage_prediction_selections.prediction_id =
            swiss_stage_predictions.id
        LEFT JOIN teams
            ON teams.id = swiss_stage_prediction_selections.team_id
        WHERE swiss_stage_predictions.contest_id = ?
        ORDER BY
            swiss_stage_predictions.user_id,
            swiss_stage_prediction_selections.category,
            teams.name COLLATE NOCASE,
            teams.id
        """,
        (contest_id,),
    ).fetchall()
    result_rows = connection.execute(
        """
        SELECT
            teams.id,
            teams.name,
            swiss_stage_result_selections.category
        FROM swiss_stage_result_selections
        JOIN teams
            ON teams.id = swiss_stage_result_selections.team_id
        WHERE swiss_stage_result_selections.contest_id = ?
        ORDER BY
            swiss_stage_result_selections.category,
            teams.name COLLATE NOCASE,
            teams.id
        """,
        (contest_id,),
    ).fetchall()
    candidate_rows = _get_swiss_stage_candidate_rows(
        connection,
        contest_id=contest_id,
    )
    return prediction_rows, result_rows, candidate_rows


def _leaderboard_swiss_stage_prediction_history_by_user(
    rows,
    *,
    selection_mode: str,
    direct_qualifier_count: int,
    elimination_qualifier_count: int,
    direct_correct_points: int,
    elimination_correct_points: int,
    cross_category_points: int,
) -> dict[int, SwissStagePredictionHistory]:
    prediction_rows, result_rows, candidate_rows = rows
    candidates = tuple(_team_summary_from_row(row) for row in candidate_rows)
    rows_by_user: dict[int, list[object]] = {}
    for row in prediction_rows:
        user_rows = rows_by_user.setdefault(int(row["user_id"]), [])
        if row["id"] is not None:
            user_rows.append(row)
    actual_result = _swiss_stage_selection_from_rows(
        result_rows,
        candidates=candidates,
        selection_exists=bool(result_rows),
        selection_mode=selection_mode,
        direct_qualifier_count=direct_qualifier_count,
        elimination_qualifier_count=elimination_qualifier_count,
    )
    history_by_user: dict[int, SwissStagePredictionHistory] = {}
    for user_id, user_rows in rows_by_user.items():
        prediction = _swiss_stage_selection_from_rows(
            user_rows,
            candidates=candidates,
            selection_exists=True,
            selection_mode=selection_mode,
            direct_qualifier_count=direct_qualifier_count,
            elimination_qualifier_count=elimination_qualifier_count,
        )
        if prediction is None:
            continue
        awards = _swiss_stage_awards_from_rows(
            user_rows,
            result_rows=result_rows,
            selection_mode=selection_mode,
            direct_correct_points=direct_correct_points,
            elimination_correct_points=elimination_correct_points,
            cross_category_points=cross_category_points,
        )
        score_breakdown = (
            _swiss_stage_score_breakdown(awards) if actual_result is not None else None
        )
        history_by_user[user_id] = SwissStagePredictionHistory(
            prediction=prediction,
            actual_result=actual_result,
            awarded_points=(score_breakdown.total_points if score_breakdown else None),
            awards=awards if actual_result is not None else (),
            score_breakdown=score_breakdown,
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
    swiss_stage_prediction_history_by_user: (
        dict[int, SwissStagePredictionHistory] | None
    ) = None,
) -> tuple[ContestLeaderboardEntry, ...]:
    history_by_user = prediction_history_by_user or {}
    champion_history_by_user = champion_prediction_history_by_user or {}
    swiss_stage_history_by_user = swiss_stage_prediction_history_by_user or {}
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
                participant_username=(
                    str(row["username"]).strip() or None
                    if row["username"] is not None
                    else None
                ),
                total_points=total_points,
                match_predictions_count=int(row["match_predictions_count"]),
                two_legged_tie_predictions_count=(
                    int(row["two_legged_tie_predictions_count"])
                    if "two_legged_tie_predictions_count" in row.keys()
                    else 0
                ),
                champion_prediction_count=int(row["champion_prediction_count"]),
                swiss_stage_prediction_count=int(row["swiss_stage_prediction_count"]),
                calculated_predictions_count=int(row["calculated_predictions_count"]),
                tiebreak_metrics=_leaderboard_tiebreak_metrics(row),
                prediction_history=history_by_user.get(int(row["user_id"]), ()),
                champion_prediction_history=champion_history_by_user.get(
                    int(row["user_id"]),
                ),
                swiss_stage_prediction_history=swiss_stage_history_by_user.get(
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


def _normalize_optional_result_pair(
    *,
    home_score: int | None,
    away_score: int | None,
    field_name: str,
) -> tuple[int | None, int | None]:
    if home_score is None and away_score is None:
        return None, None
    if home_score is None or away_score is None:
        raise ValueError(f"{field_name} нужно указать полностью.")
    return (
        _normalize_match_result_score(
            home_score,
            field_name=f"{field_name} первой команды",
        ),
        _normalize_match_result_score(
            away_score,
            field_name=f"{field_name} второй команды",
        ),
    )


def _normalize_advancing_team_id(
    value: int | None,
    *,
    field_name: str,
) -> int:
    if value is None:
        return 0

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} должен быть идентификатором команды.")

    if value <= 0:
        raise ValueError(f"{field_name} должен быть идентификатором команды.")

    return value


def _resolve_advancing_team_for_score(
    match_row,
    *,
    advancing_team_id: int,
    home_score: int,
    away_score: int,
    field_name: str,
) -> int:
    """Validate the separate series/tie winner against its score.

    For football, ``home_score`` and ``away_score`` are the 90-minute score.
    A draw therefore leaves either participating team eligible to advance;
    otherwise the selected team must be the 90-minute winner.
    """

    home_team_id = int(match_row["home_team_id"])
    away_team_id = int(match_row["away_team_id"])

    if match_row["template_key"] == "the_international_2026":
        best_of = int(match_row["best_of"])
        wins_required = best_of // 2 + 1
        is_valid_home_win = (
            home_score == wins_required and 0 <= away_score < wins_required
        )
        is_valid_away_win = (
            away_score == wins_required and 0 <= home_score < wins_required
        )
        if not (is_valid_home_win or is_valid_away_win):
            raise ValueError(
                f"Для Bo{best_of} укажите завершённый счёт серии: "
                f"победитель должен выиграть {wins_required} карты."
            )
        expected_advancing_team_id = home_team_id if is_valid_home_win else away_team_id
        if advancing_team_id not in (0, expected_advancing_team_id):
            raise ValueError(f"{field_name} должен совпадать с победителем по счёту.")
        return expected_advancing_team_id

    if advancing_team_id not in {home_team_id, away_team_id}:
        raise ValueError(f"{field_name} должен быть одной из команд матча.")

    if home_score == away_score:
        return advancing_team_id

    expected_advancing_team_id = (
        home_team_id if home_score > away_score else away_team_id
    )

    if advancing_team_id != expected_advancing_team_id:
        raise ValueError(f"{field_name} должен совпадать с победителем по счёту.")

    return advancing_team_id


def _resolve_now_utc(now_utc: datetime | None) -> datetime:
    if now_utc is None:
        return datetime.now(timezone.utc)

    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")

    return now_utc.astimezone(timezone.utc)


def _serialize_datetime_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_stored_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("Сохранена некорректная дата начала.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("Сохранена дата начала без часового пояса.")
    return parsed.astimezone(timezone.utc)


def _is_two_legged_tie_prediction_open(
    tie_row,
    *,
    now_utc: datetime,
) -> bool:
    if str(tie_row["first_leg_status"]) != "scheduled":
        return False
    return _parse_stored_datetime(str(tie_row["prediction_deadline_at"])) > now_utc


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


def _build_request_fingerprint(
    contest_name: str,
    *,
    template: _ContestTemplate,
    shared_tournament_id: int | None = None,
) -> str:
    payload = json.dumps(
        {
            "competition_name": template.competition_name,
            "competition_season": template.competition_season,
            "competition_type": template.competition_type,
            "contest_name": contest_name,
            "template_key": template.key,
            "shared_tournament_id": shared_tournament_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_contest_slug(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def _active_contest_summary_from_row(row) -> ActiveContestSummary:
    return ActiveContestSummary(
        id=int(row["id"]),
        name=str(row["name"]),
        slug=str(row["slug"]),
        template_key=str(row["template_key"]),
        created_at=str(row["created_at"]),
    )

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from app.tournament_catalog import THE_INTERNATIONAL_2026_TEMPLATE_KEY

MatchScoreType = Literal["exact_score", "goal_difference", "outcome"]
TieResolutionMethod = Literal["aggregate", "extra_time", "penalties"]


@dataclass(frozen=True, slots=True)
class MatchScoreAward:
    score_type: MatchScoreType
    points: int


@dataclass(frozen=True, slots=True)
class TwoLeggedTieResolution:
    aggregate_first_team_score: int
    aggregate_second_team_score: int
    advancing_team_id: int
    resolution_method: TieResolutionMethod


def calculate_swiss_stage_selection_points(
    *,
    predicted_category: str,
    actual_category: str | None,
    direct_correct_points: int,
    elimination_correct_points: int,
    cross_category_points: int,
) -> int:
    """Return the points for one explicitly selected stage category.

    Only the two explicitly predicted categories are scoreable. A team in the
    implicit middle category therefore always awards zero points. The separate
    cross-category value preserves the legacy Swiss-stage rule while allowing
    general-stage contests to configure mismatches as zero.
    """

    if predicted_category not in {"direct", "elimination"}:
        return 0
    if actual_category not in {"direct", "elimination"}:
        return 0
    if predicted_category != actual_category:
        return cross_category_points
    if predicted_category == "direct":
        return direct_correct_points
    return elimination_correct_points


def resolve_two_legged_tie_result(
    *,
    first_team_id: int,
    second_team_id: int,
    first_leg_home_team_id: int,
    first_leg_away_team_id: int,
    first_leg_home_score: int,
    first_leg_away_score: int,
    second_leg_home_team_id: int,
    second_leg_away_team_id: int,
    second_leg_home_score: int,
    second_leg_away_score: int,
    second_leg_extra_time_home_score: int | None = None,
    second_leg_extra_time_away_score: int | None = None,
    second_leg_home_penalty_score: int | None = None,
    second_leg_away_penalty_score: int | None = None,
    advancing_team_id: int | None = None,
) -> TwoLeggedTieResolution:
    """Resolve a two-legged football tie without applying the away-goals rule.

    Match scores are the scores after 90 minutes. Extra-time values are goals
    scored during extra time in the second leg, while penalty values are the
    shootout score. The optional advancing team is validation-only; the winner
    is always derived from the supplied results.
    """

    if first_team_id == second_team_id:
        raise ValueError("В противостоянии должны участвовать разные команды.")

    expected_teams = {first_team_id, second_team_id}
    if {
        first_leg_home_team_id,
        first_leg_away_team_id,
    } != expected_teams or {
        second_leg_home_team_id,
        second_leg_away_team_id,
    } != expected_teams:
        raise ValueError(
            "Оба матча противостояния должны состоять из одних и тех же команд."
        )

    score_values = (
        first_leg_home_score,
        first_leg_away_score,
        second_leg_home_score,
        second_leg_away_score,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in score_values
    ):
        raise ValueError("Счёт каждого матча должен быть неотрицательным целым числом.")

    extra_time_scores = _normalize_optional_score_pair(
        home_score=second_leg_extra_time_home_score,
        away_score=second_leg_extra_time_away_score,
        label="Счёт дополнительного времени",
    )
    penalty_scores = _normalize_optional_score_pair(
        home_score=second_leg_home_penalty_score,
        away_score=second_leg_away_penalty_score,
        label="Счёт серии пенальти",
    )

    aggregate_first_team_score = _team_score_from_leg(
        team_id=first_team_id,
        home_team_id=first_leg_home_team_id,
        home_score=first_leg_home_score,
        away_score=first_leg_away_score,
    ) + _team_score_from_leg(
        team_id=first_team_id,
        home_team_id=second_leg_home_team_id,
        home_score=second_leg_home_score,
        away_score=second_leg_away_score,
    )
    aggregate_second_team_score = _team_score_from_leg(
        team_id=second_team_id,
        home_team_id=first_leg_home_team_id,
        home_score=first_leg_home_score,
        away_score=first_leg_away_score,
    ) + _team_score_from_leg(
        team_id=second_team_id,
        home_team_id=second_leg_home_team_id,
        home_score=second_leg_home_score,
        away_score=second_leg_away_score,
    )

    if aggregate_first_team_score != aggregate_second_team_score:
        if extra_time_scores is not None or penalty_scores is not None:
            raise ValueError(
                "Дополнительное время и пенальти недоступны при неравном общем счёте."
            )
        resolved_advancing_team_id = (
            first_team_id
            if aggregate_first_team_score > aggregate_second_team_score
            else second_team_id
        )
        resolution_method: TieResolutionMethod = "aggregate"
    else:
        if extra_time_scores is None:
            raise ValueError(
                "При равном общем счёте укажите счёт дополнительного времени."
            )

        extra_time_home_score, extra_time_away_score = extra_time_scores
        extra_time_first_team_score = _team_score_from_leg(
            team_id=first_team_id,
            home_team_id=second_leg_home_team_id,
            home_score=extra_time_home_score,
            away_score=extra_time_away_score,
        )
        extra_time_second_team_score = _team_score_from_leg(
            team_id=second_team_id,
            home_team_id=second_leg_home_team_id,
            home_score=extra_time_home_score,
            away_score=extra_time_away_score,
        )

        if extra_time_first_team_score != extra_time_second_team_score:
            if penalty_scores is not None:
                raise ValueError(
                    "Серия пенальти недоступна, если дополнительное время выявило "
                    "победителя."
                )
            resolved_advancing_team_id = (
                first_team_id
                if extra_time_first_team_score > extra_time_second_team_score
                else second_team_id
            )
            resolution_method = "extra_time"
        else:
            if penalty_scores is None:
                raise ValueError(
                    "Если дополнительное время не выявило победителя, укажите счёт "
                    "серии пенальти."
                )
            penalty_home_score, penalty_away_score = penalty_scores
            if penalty_home_score == penalty_away_score:
                raise ValueError("Серия пенальти не может завершиться вничью.")
            resolved_advancing_team_id = (
                second_leg_home_team_id
                if penalty_home_score > penalty_away_score
                else second_leg_away_team_id
            )
            resolution_method = "penalties"

    if advancing_team_id is not None:
        if advancing_team_id not in expected_teams:
            raise ValueError("Прошедшая команда должна участвовать в противостоянии.")
        if advancing_team_id != resolved_advancing_team_id:
            raise ValueError(
                "Прошедшая команда не совпадает с победителем по результатам "
                "противостояния."
            )

    return TwoLeggedTieResolution(
        aggregate_first_team_score=aggregate_first_team_score,
        aggregate_second_team_score=aggregate_second_team_score,
        advancing_team_id=resolved_advancing_team_id,
        resolution_method=resolution_method,
    )


def calculate_match_score_award(
    *,
    predicted_home_score: int,
    predicted_away_score: int,
    actual_home_score: int,
    actual_away_score: int,
    exact_score_points: int,
    goal_difference_points: int,
    outcome_points: int,
) -> MatchScoreAward | None:
    """Calculate football score points from the two 90-minute scores.

    Whether the team later advances after extra time or penalties is scored
    independently by ``recalculate_tie_prediction_scores`` and must not alter
    either score passed here.
    """

    if (
        predicted_home_score == actual_home_score
        and predicted_away_score == actual_away_score
    ):
        return MatchScoreAward(
            score_type="exact_score",
            points=exact_score_points,
        )

    predicted_goal_difference = predicted_home_score - predicted_away_score
    actual_goal_difference = actual_home_score - actual_away_score

    if predicted_goal_difference == actual_goal_difference:
        return MatchScoreAward(
            score_type="goal_difference",
            points=goal_difference_points,
        )

    if _match_outcome(
        home_score=predicted_home_score,
        away_score=predicted_away_score,
    ) == _match_outcome(
        home_score=actual_home_score,
        away_score=actual_away_score,
    ):
        return MatchScoreAward(
            score_type="outcome",
            points=outcome_points,
        )

    return None


def calculate_series_score_award(
    *,
    predicted_home_score: int,
    predicted_away_score: int,
    actual_home_score: int,
    actual_away_score: int,
) -> MatchScoreAward | None:
    if (
        predicted_home_score == actual_home_score
        and predicted_away_score == actual_away_score
    ):
        return MatchScoreAward(score_type="exact_score", points=2)

    if _match_outcome(
        home_score=predicted_home_score,
        away_score=predicted_away_score,
    ) == _match_outcome(
        home_score=actual_home_score,
        away_score=actual_away_score,
    ):
        return MatchScoreAward(score_type="outcome", points=1)

    return None


def recalculate_match_prediction_scores(
    connection: sqlite3.Connection,
    *,
    match_id: int,
) -> None:
    match_row = connection.execute(
        """
        SELECT
            matches.home_score_final,
            matches.away_score_final,
            matches.scoring_rule_set_id,
            contests.template_key,
            scoring_rule_sets.exact_score_points,
            scoring_rule_sets.goal_difference_points,
            scoring_rule_sets.outcome_points
        FROM matches
        JOIN scoring_rule_sets
            ON scoring_rule_sets.id = matches.scoring_rule_set_id
        JOIN competitions
            ON competitions.id = scoring_rule_sets.competition_id
        JOIN contests
            ON contests.id = competitions.contest_id
        WHERE matches.id = ?
        """,
        (match_id,),
    ).fetchone()

    if match_row is None:
        raise RuntimeError("Не удалось найти матч для пересчёта баллов.")

    connection.execute(
        """
        DELETE FROM match_prediction_scores
        WHERE match_prediction_id IN (
            SELECT id
            FROM match_predictions
            WHERE match_id = ?
        )
        """,
        (match_id,),
    )

    if match_row["home_score_final"] is None or match_row["away_score_final"] is None:
        return

    prediction_rows = connection.execute(
        """
        SELECT
            id,
            predicted_home_score,
            predicted_away_score
        FROM match_predictions
        WHERE match_id = ?
        ORDER BY id ASC
        """,
        (match_id,),
    ).fetchall()

    score_rows: list[tuple[int, int, MatchScoreType, int]] = []

    for prediction_row in prediction_rows:
        if match_row["template_key"] == THE_INTERNATIONAL_2026_TEMPLATE_KEY:
            award = calculate_series_score_award(
                predicted_home_score=int(prediction_row["predicted_home_score"]),
                predicted_away_score=int(prediction_row["predicted_away_score"]),
                actual_home_score=int(match_row["home_score_final"]),
                actual_away_score=int(match_row["away_score_final"]),
            )
        else:
            award = calculate_match_score_award(
                predicted_home_score=int(prediction_row["predicted_home_score"]),
                predicted_away_score=int(prediction_row["predicted_away_score"]),
                actual_home_score=int(match_row["home_score_final"]),
                actual_away_score=int(match_row["away_score_final"]),
                exact_score_points=int(match_row["exact_score_points"]),
                goal_difference_points=int(match_row["goal_difference_points"]),
                outcome_points=int(match_row["outcome_points"]),
            )

        if award is None or award.points == 0:
            continue

        score_rows.append(
            (
                int(prediction_row["id"]),
                int(match_row["scoring_rule_set_id"]),
                award.score_type,
                award.points,
            )
        )

    connection.executemany(
        """
        INSERT INTO match_prediction_scores (
            match_prediction_id,
            scoring_rule_set_id,
            score_type,
            points
        )
        VALUES (?, ?, ?, ?)
        """,
        score_rows,
    )


def recalculate_tie_prediction_scores(
    connection: sqlite3.Connection,
    *,
    tie_id: int,
) -> None:
    tie_row = connection.execute(
        """
        SELECT
            ties.advancing_team_id,
            ties.scoring_rule_set_id,
            scoring_rule_sets.advancing_team_points
        FROM ties
        JOIN scoring_rule_sets
            ON scoring_rule_sets.id = ties.scoring_rule_set_id
        WHERE ties.id = ?
        """,
        (tie_id,),
    ).fetchone()

    if tie_row is None:
        raise RuntimeError("Не удалось найти противостояние для пересчёта баллов.")

    connection.execute(
        """
        DELETE FROM tie_prediction_scores
        WHERE tie_prediction_id IN (
            SELECT id
            FROM tie_predictions
            WHERE tie_id = ?
        )
        """,
        (tie_id,),
    )

    if tie_row["advancing_team_id"] is None:
        return

    prediction_rows = connection.execute(
        """
        SELECT
            id,
            predicted_advancing_team_id
        FROM tie_predictions
        WHERE tie_id = ?
        ORDER BY id ASC
        """,
        (tie_id,),
    ).fetchall()

    advancing_team_id = int(tie_row["advancing_team_id"])
    advancing_team_points = int(tie_row["advancing_team_points"])

    if advancing_team_points == 0:
        return

    score_rows = [
        (
            int(prediction_row["id"]),
            int(tie_row["scoring_rule_set_id"]),
            advancing_team_points,
        )
        for prediction_row in prediction_rows
        if int(prediction_row["predicted_advancing_team_id"]) == advancing_team_id
    ]

    connection.executemany(
        """
        INSERT INTO tie_prediction_scores (
            tie_prediction_id,
            scoring_rule_set_id,
            points
        )
        VALUES (?, ?, ?)
        """,
        score_rows,
    )


def _normalize_optional_score_pair(
    *,
    home_score: int | None,
    away_score: int | None,
    label: str,
) -> tuple[int, int] | None:
    if home_score is None and away_score is None:
        return None
    if home_score is None or away_score is None:
        raise ValueError(f"{label} нужно указать полностью.")
    if (
        isinstance(home_score, bool)
        or not isinstance(home_score, int)
        or home_score < 0
        or isinstance(away_score, bool)
        or not isinstance(away_score, int)
        or away_score < 0
    ):
        raise ValueError(f"{label} должен состоять из неотрицательных целых чисел.")
    return home_score, away_score


def _team_score_from_leg(
    *,
    team_id: int,
    home_team_id: int,
    home_score: int,
    away_score: int,
) -> int:
    return home_score if team_id == home_team_id else away_score


def _match_outcome(*, home_score: int, away_score: int) -> int:
    if home_score > away_score:
        return 1

    if home_score < away_score:
        return -1

    return 0

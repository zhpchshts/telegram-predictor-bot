from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

MatchScoreType = Literal["exact_score", "goal_difference", "outcome"]


@dataclass(frozen=True, slots=True)
class MatchScoreAward:
    score_type: MatchScoreType
    points: int


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
        if match_row["template_key"] == "the_international_2026":
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


def _match_outcome(*, home_score: int, away_score: int) -> int:
    if home_score > away_score:
        return 1

    if home_score < away_score:
        return -1

    return 0

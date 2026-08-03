from pathlib import Path

from app.database import create_connection, initialize_database
from app.scoring_service import (
    calculate_match_score_award,
    calculate_series_score_award,
    recalculate_match_prediction_scores,
)


def test_calculate_match_score_award_returns_exact_score_points() -> None:
    award = calculate_match_score_award(
        predicted_home_score=2,
        predicted_away_score=1,
        actual_home_score=2,
        actual_away_score=1,
        exact_score_points=3,
        goal_difference_points=2,
        outcome_points=1,
    )

    assert award is not None
    assert award.score_type == "exact_score"
    assert award.points == 3


def test_calculate_match_score_award_returns_goal_difference_points() -> None:
    award = calculate_match_score_award(
        predicted_home_score=3,
        predicted_away_score=2,
        actual_home_score=2,
        actual_away_score=1,
        exact_score_points=3,
        goal_difference_points=2,
        outcome_points=1,
    )

    assert award is not None
    assert award.score_type == "goal_difference"
    assert award.points == 2


def test_calculate_match_score_award_returns_goal_difference_points_for_draw() -> None:
    award = calculate_match_score_award(
        predicted_home_score=2,
        predicted_away_score=2,
        actual_home_score=1,
        actual_away_score=1,
        exact_score_points=3,
        goal_difference_points=2,
        outcome_points=1,
    )

    assert award is not None
    assert award.score_type == "goal_difference"
    assert award.points == 2


def test_calculate_match_score_award_returns_outcome_points() -> None:
    award = calculate_match_score_award(
        predicted_home_score=3,
        predicted_away_score=1,
        actual_home_score=1,
        actual_away_score=0,
        exact_score_points=3,
        goal_difference_points=2,
        outcome_points=1,
    )

    assert award is not None
    assert award.score_type == "outcome"
    assert award.points == 1


def test_calculate_match_score_award_returns_none_for_incorrect_prediction() -> None:
    award = calculate_match_score_award(
        predicted_home_score=1,
        predicted_away_score=0,
        actual_home_score=1,
        actual_away_score=2,
        exact_score_points=3,
        goal_difference_points=2,
        outcome_points=1,
    )

    assert award is None


def test_calculate_series_score_award_returns_two_points_for_exact_score() -> None:
    award = calculate_series_score_award(
        predicted_home_score=2,
        predicted_away_score=1,
        actual_home_score=2,
        actual_away_score=1,
    )

    assert award is not None
    assert award.score_type == "exact_score"
    assert award.points == 2


def test_calculate_series_score_award_returns_one_point_for_correct_winner() -> None:
    award = calculate_series_score_award(
        predicted_home_score=2,
        predicted_away_score=0,
        actual_home_score=2,
        actual_away_score=1,
    )

    assert award is not None
    assert award.score_type == "outcome"
    assert award.points == 1


def test_calculate_series_score_award_returns_none_for_wrong_winner() -> None:
    award = calculate_series_score_award(
        predicted_home_score=2,
        predicted_away_score=1,
        actual_home_score=1,
        actual_away_score=2,
    )

    assert award is None


def test_recalculate_uses_series_rules_for_ti_template(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        chat_id = connection.execute(
            "INSERT INTO chats (telegram_chat_id, title) VALUES (-1, 'Test')"
        ).lastrowid
        contest_id = connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, template_key)
            VALUES (?, 'TI', 'ti', 'the_international_2026')
            """,
            (chat_id,),
        ).lastrowid
        competition_id = connection.execute(
            """
            INSERT INTO competitions (
                contest_id,
                name,
                season,
                competition_type
            )
            VALUES (?, 'The International', '2026', 'the_international')
            """,
            (contest_id,),
        ).lastrowid
        rule_set_id = connection.execute(
            """
            INSERT INTO scoring_rule_sets (
                competition_id,
                version,
                exact_score_points,
                goal_difference_points,
                outcome_points,
                advancing_team_points
            )
            VALUES (?, 1, 2, 0, 1, 0)
            """,
            (competition_id,),
        ).lastrowid
        stage_id = connection.execute(
            """
            INSERT INTO stages (competition_id, name, position, stage_type)
            VALUES (?, 'Playoffs', 0, 'knockout')
            """,
            (competition_id,),
        ).lastrowid
        home_team_id = connection.execute(
            "INSERT INTO teams (name) VALUES ('Home')"
        ).lastrowid
        away_team_id = connection.execute(
            "INSERT INTO teams (name) VALUES ('Away')"
        ).lastrowid
        match_id = connection.execute(
            """
            INSERT INTO matches (
                stage_id,
                scoring_rule_set_id,
                home_team_id,
                away_team_id,
                starts_at_utc,
                best_of,
                status,
                home_score_final,
                away_score_final
            )
            VALUES (?, ?, ?, ?, '2026-08-01T12:00:00Z', 3, 'finished', 2, 1)
            """,
            (stage_id, rule_set_id, home_team_id, away_team_id),
        ).lastrowid
        prediction_rows = (
            (101, "Exact", 2, 1),
            (102, "Winner", 2, 0),
            (103, "Same difference", 3, 2),
            (104, "Wrong", 1, 2),
        )
        for telegram_user_id, first_name, home_score, away_score in prediction_rows:
            user_id = connection.execute(
                """
                INSERT INTO users (telegram_user_id, first_name)
                VALUES (?, ?)
                """,
                (telegram_user_id, first_name),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO match_predictions (
                    match_id,
                    user_id,
                    predicted_home_score,
                    predicted_away_score
                )
                VALUES (?, ?, ?, ?)
                """,
                (match_id, user_id, home_score, away_score),
            )

        recalculate_match_prediction_scores(connection, match_id=match_id)
        scores = connection.execute(
            """
            SELECT users.telegram_user_id, scores.score_type, scores.points
            FROM match_prediction_scores AS scores
            JOIN match_predictions ON match_predictions.id = scores.match_prediction_id
            JOIN users ON users.id = match_predictions.user_id
            ORDER BY users.telegram_user_id
            """
        ).fetchall()

    assert [tuple(row) for row in scores] == [
        (101, "exact_score", 2),
        (102, "outcome", 1),
        (103, "outcome", 1),
    ]

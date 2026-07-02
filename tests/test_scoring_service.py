from app.scoring_service import calculate_match_score_award


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

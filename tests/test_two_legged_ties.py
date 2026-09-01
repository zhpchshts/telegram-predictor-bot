from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    PredictionUnavailableError,
    TwoLeggedTiePredictionUnavailableError,
    create_two_legged_tie,
    create_world_cup_2026_contest,
    get_contest_details,
    save_match_prediction,
    save_match_result,
    save_two_legged_tie_prediction,
    save_two_legged_tie_result,
)
from app.database import create_connection, initialize_database
from tests.support import ensure_contest_teams


TELEGRAM_CHAT_ID = -1007654321000
ADMIN_USER_ID = 7001
FIRST_LEG_START = "2026-06-11T18:00:00Z"
SECOND_LEG_START = "2026-06-18T18:00:00Z"
BEFORE_FIRST_LEG = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
AT_FIRST_LEG = datetime(2026, 6, 11, 18, tzinfo=timezone.utc)
BETWEEN_LEGS = datetime(2026, 6, 12, 12, tzinfo=timezone.utc)
AT_SECOND_LEG = datetime(2026, 6, 18, 18, tzinfo=timezone.utc)
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=TELEGRAM_CHAT_ID,
    telegram_user_id=ADMIN_USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


def _create_tie(database_path: Path):
    initialize_database(database_path)
    contest = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        chat_title="Двухматчевые пары",
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        contest_name="Плей-офф",
        idempotency_key="create-two-legged-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest
    first_team_id, second_team_id = ensure_contest_teams(
        database_path,
        contest_id=contest.id,
        names=("Реал", "Интер"),
    )
    tie = create_two_legged_tie(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        first_team_id=first_team_id,
        second_team_id=second_team_id,
        first_leg_starts_at_utc=FIRST_LEG_START,
        second_leg_starts_at_utc=SECOND_LEG_START,
        idempotency_key="create-two-legged-tie",
        audit_actor=AUDIT_ACTOR,
        now_utc=BEFORE_FIRST_LEG,
    ).tie
    return contest, tie, first_team_id, second_team_id


def _save_match_prediction(
    database_path: Path,
    *,
    contest_id: int,
    match_id: int,
    home_score: int,
    away_score: int,
    user_id: int = ADMIN_USER_ID,
    now_utc: datetime = BEFORE_FIRST_LEG,
):
    return save_match_prediction(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        match_id=match_id,
        telegram_user_id=user_id,
        first_name=f"User {user_id}",
        last_name=None,
        username=f"user{user_id}",
        predicted_home_score=home_score,
        predicted_away_score=away_score,
        predicted_advancing_team_id=None,
        now_utc=now_utc,
    )


def _save_match_result(
    database_path: Path,
    *,
    contest_id: int,
    match_id: int,
    home_score: int,
    away_score: int,
    now_utc: datetime,
):
    return save_match_result(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        match_id=match_id,
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        home_score=home_score,
        away_score=away_score,
        advancing_team_id=None,
        audit_actor=AUDIT_ACTOR,
        now_utc=now_utc,
    )


def _save_tie_prediction(
    database_path: Path,
    *,
    contest_id: int,
    tie_id: int,
    team_id: int,
    user_id: int = ADMIN_USER_ID,
    now_utc: datetime = BEFORE_FIRST_LEG,
):
    return save_two_legged_tie_prediction(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        tie_id=tie_id,
        telegram_user_id=user_id,
        first_name=f"User {user_id}",
        last_name=None,
        username=f"user{user_id}",
        predicted_advancing_team_id=team_id,
        now_utc=now_utc,
    )


def test_create_two_legged_tie_is_atomic_and_reverses_home_team(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest, tie, first_team_id, second_team_id = _create_tie(database_path)

    assert tie.first_team_id == first_team_id
    assert tie.second_team_id == second_team_id
    assert tie.prediction_deadline_at == FIRST_LEG_START
    assert tie.is_prediction_open is True

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ADMIN_USER_ID,
        now_utc=BEFORE_FIRST_LEG,
    )
    assert details.two_legged_ties == (tie,)
    assert [match.leg_number for match in details.matches] == [1, 2]
    assert all(match.is_two_legged for match in details.matches)
    assert (
        details.matches[0].home_team_id,
        details.matches[0].away_team_id,
    ) == (first_team_id, second_team_id)
    assert (
        details.matches[1].home_team_id,
        details.matches[1].away_team_id,
    ) == (second_team_id, first_team_id)


@pytest.mark.parametrize("leg_number", [1, 2])
@pytest.mark.parametrize(
    ("predicted_score", "expected_score_type", "expected_points"),
    [
        ((2, 1), "exact_score", 3),
        ((3, 2), "goal_difference", 2),
        ((3, 1), "outcome", 1),
        ((0, 1), None, 0),
    ],
)
def test_each_leg_reuses_normal_match_scoring_for_90_minute_score(
    tmp_path: Path,
    leg_number: int,
    predicted_score: tuple[int, int],
    expected_score_type: str | None,
    expected_points: int,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest, tie, _, _ = _create_tie(database_path)
    match_id = tie.first_leg_match_id if leg_number == 1 else tie.second_leg_match_id
    result_time = AT_FIRST_LEG if leg_number == 1 else AT_SECOND_LEG
    _save_match_prediction(
        database_path,
        contest_id=contest.id,
        match_id=match_id,
        home_score=predicted_score[0],
        away_score=predicted_score[1],
    )

    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=match_id,
        home_score=2,
        away_score=1,
        now_utc=result_time,
    )

    with create_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT match_prediction_scores.score_type,
                   match_prediction_scores.points
            FROM match_predictions
            LEFT JOIN match_prediction_scores
                ON match_prediction_scores.match_prediction_id =
                   match_predictions.id
            JOIN users ON users.id = match_predictions.user_id
            WHERE match_predictions.match_id = ?
              AND users.telegram_user_id = ?
            """,
            (match_id, ADMIN_USER_ID),
        ).fetchone()

    assert row is not None
    assert row["score_type"] == expected_score_type
    assert (int(row["points"]) if row["points"] is not None else 0) == expected_points


def test_tie_prediction_closes_at_first_kickoff_but_second_leg_stays_open(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest, tie, first_team_id, second_team_id = _create_tie(database_path)

    created = _save_tie_prediction(
        database_path,
        contest_id=contest.id,
        tie_id=tie.id,
        team_id=first_team_id,
    )
    changed = _save_tie_prediction(
        database_path,
        contest_id=contest.id,
        tie_id=tie.id,
        team_id=second_team_id,
        now_utc=datetime(2026, 6, 11, 17, 59, tzinfo=timezone.utc),
    )
    assert created.was_created is True
    assert changed.was_created is False
    assert changed.prediction.advancing_team_id == second_team_id

    with pytest.raises(TwoLeggedTiePredictionUnavailableError):
        _save_tie_prediction(
            database_path,
            contest_id=contest.id,
            tie_id=tie.id,
            team_id=first_team_id,
            now_utc=AT_FIRST_LEG,
        )

    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=1,
        away_score=0,
        now_utc=AT_FIRST_LEG,
    )
    saved_second_leg = _save_match_prediction(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=2,
        away_score=1,
        now_utc=BETWEEN_LEGS,
    )
    assert saved_second_leg.prediction.home_score == 2

    with pytest.raises(PredictionUnavailableError):
        _save_match_prediction(
            database_path,
            contest_id=contest.id,
            match_id=tie.second_leg_match_id,
            home_score=1,
            away_score=0,
            now_utc=AT_SECOND_LEG,
        )


def test_extra_time_decides_tie_without_changing_second_leg_score_points(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest, tie, first_team_id, _ = _create_tie(database_path)
    _save_tie_prediction(
        database_path,
        contest_id=contest.id,
        tie_id=tie.id,
        team_id=first_team_id,
    )
    _save_match_prediction(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=2,
        away_score=1,
    )
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=1,
        away_score=0,
        now_utc=AT_FIRST_LEG,
    )
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=2,
        away_score=1,
        now_utc=AT_SECOND_LEG,
    )

    saved = save_two_legged_tie_result(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        tie_id=tie.id,
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        advancing_team_id=first_team_id,
        second_leg_extra_time_home_score=0,
        second_leg_extra_time_away_score=1,
        audit_actor=AUDIT_ACTOR,
        now_utc=AT_SECOND_LEG,
    )
    assert saved.result.resolution_method == "extra_time"
    assert saved.result.aggregate_first_team_score == 2
    assert saved.result.aggregate_second_team_score == 2

    with create_connection(database_path) as connection:
        points = connection.execute(
            """
            SELECT points
            FROM match_prediction_scores
            JOIN match_predictions
                ON match_predictions.id =
                   match_prediction_scores.match_prediction_id
            WHERE match_predictions.match_id = ?
            """,
            (tie.second_leg_match_id,),
        ).fetchone()["points"]
    assert int(points) == 3


def test_penalties_resolve_level_aggregate_and_do_not_affect_match_score(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest, tie, _, second_team_id = _create_tie(database_path)
    _save_tie_prediction(
        database_path,
        contest_id=contest.id,
        tie_id=tie.id,
        team_id=second_team_id,
    )
    _save_match_prediction(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=1,
        away_score=0,
    )
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=1,
        away_score=0,
        now_utc=AT_FIRST_LEG,
    )
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=1,
        away_score=0,
        now_utc=AT_SECOND_LEG,
    )

    saved = save_two_legged_tie_result(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        tie_id=tie.id,
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        advancing_team_id=second_team_id,
        second_leg_extra_time_home_score=0,
        second_leg_extra_time_away_score=0,
        second_leg_home_penalty_score=5,
        second_leg_away_penalty_score=4,
        audit_actor=AUDIT_ACTOR,
    )
    assert saved.result.resolution_method == "penalties"
    assert saved.result.aggregate_first_team_score == 1
    assert saved.result.aggregate_second_team_score == 1

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ADMIN_USER_ID,
        now_utc=AT_SECOND_LEG,
    )
    second_leg = next(match for match in details.matches if match.leg_number == 2)
    assert (second_leg.result.home_score, second_leg.result.away_score) == (1, 0)
    assert second_leg.prediction_score.total_points == 3
    assert details.two_legged_ties[0].awarded_points == 1


def test_away_goals_are_ignored_and_level_aggregate_requires_supplemental_result(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest, tie, first_team_id, _ = _create_tie(database_path)
    # Интер has two away goals in the first leg and Реал only one in the return.
    # The 2:2 aggregate must still go to extra time.
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=1,
        away_score=2,
        now_utc=AT_FIRST_LEG,
    )
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=0,
        away_score=1,
        now_utc=AT_SECOND_LEG,
    )

    with pytest.raises(ValueError, match="дополнительного времени"):
        save_two_legged_tie_result(
            database_path=database_path,
            telegram_chat_id=TELEGRAM_CHAT_ID,
            contest_id=contest.id,
            tie_id=tie.id,
            telegram_user_id=ADMIN_USER_ID,
            first_name="Admin",
            last_name=None,
            username="admin",
            advancing_team_id=None,
            audit_actor=AUDIT_ACTOR,
        )

    saved = save_two_legged_tie_result(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        tie_id=tie.id,
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        advancing_team_id=first_team_id,
        second_leg_extra_time_home_score=0,
        second_leg_extra_time_away_score=1,
        audit_actor=AUDIT_ACTOR,
    )
    assert saved.result.advancing_team_id == first_team_id
    assert saved.result.resolution_method == "extra_time"


def test_correction_recalculates_scores_and_repeated_result_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest, tie, first_team_id, second_team_id = _create_tie(database_path)
    _save_match_prediction(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=1,
        away_score=0,
    )
    _save_tie_prediction(
        database_path,
        contest_id=contest.id,
        tie_id=tie.id,
        team_id=first_team_id,
    )
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=1,
        away_score=0,
        now_utc=AT_FIRST_LEG,
    )
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=0,
        away_score=0,
        now_utc=AT_SECOND_LEG,
    )

    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM match_prediction_scores"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM tie_prediction_scores").fetchone()[
                0
            ]
            == 1
        )

    repeated = _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=0,
        away_score=0,
        now_utc=AT_SECOND_LEG,
    )
    assert repeated.was_created is False

    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=0,
        away_score=1,
        now_utc=AT_SECOND_LEG,
    )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ADMIN_USER_ID,
        now_utc=AT_SECOND_LEG,
    )
    assert details.two_legged_ties[0].result.advancing_team_id == second_team_id
    assert details.two_legged_ties[0].awarded_points == 0
    first_leg = next(match for match in details.matches if match.leg_number == 1)
    assert first_leg.prediction_score.total_points == 0

    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM match_prediction_scores"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM tie_prediction_scores").fetchone()[
                0
            ]
            == 0
        )


def test_maximum_is_seven_and_tie_point_is_not_duplicated_in_match_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    contest, tie, first_team_id, _ = _create_tie(database_path)
    _save_match_prediction(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=1,
        away_score=0,
    )
    _save_match_prediction(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=0,
        away_score=0,
    )
    _save_tie_prediction(
        database_path,
        contest_id=contest.id,
        tie_id=tie.id,
        team_id=first_team_id,
    )

    before_results = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ADMIN_USER_ID,
        now_utc=BEFORE_FIRST_LEG,
    ).leaderboard[0]
    assert before_results.match_predictions_count == 2
    assert before_results.two_legged_tie_predictions_count == 1
    assert before_results.calculated_predictions_count == 0

    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.first_leg_match_id,
        home_score=1,
        away_score=0,
        now_utc=AT_FIRST_LEG,
    )
    _save_match_result(
        database_path,
        contest_id=contest.id,
        match_id=tie.second_leg_match_id,
        home_score=0,
        away_score=0,
        now_utc=AT_SECOND_LEG,
    )

    entry = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ADMIN_USER_ID,
        now_utc=AT_SECOND_LEG,
    ).leaderboard[0]
    assert entry.total_points == 7
    assert entry.calculated_predictions_count == 3
    assert [
        item.prediction_score.total_points for item in entry.prediction_history
    ] == [
        3,
        3,
    ]
    assert all(
        all(
            award.score_type != "advancing_team"
            for award in item.prediction_score.awards
        )
        for item in entry.prediction_history
    )


def test_legacy_single_match_keeps_advancing_team_scoring(tmp_path: Path) -> None:
    from app.contest_service import create_match

    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        chat_title="Одиночные матчи",
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        contest_name="Плей-офф",
        idempotency_key="legacy-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest
    home_team_id, away_team_id = ensure_contest_teams(
        database_path,
        contest_id=contest.id,
        names=("Аргентина", "Бразилия"),
    )
    match = create_match(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        starts_at_utc=FIRST_LEG_START,
        idempotency_key="legacy-match",
        audit_actor=AUDIT_ACTOR,
    ).match
    save_match_prediction(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        match_id=match.id,
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        predicted_home_score=2,
        predicted_away_score=1,
        predicted_advancing_team_id=home_team_id,
        now_utc=BEFORE_FIRST_LEG,
    )
    save_match_result(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        match_id=match.id,
        telegram_user_id=ADMIN_USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        home_score=2,
        away_score=1,
        advancing_team_id=home_team_id,
        audit_actor=AUDIT_ACTOR,
        now_utc=AT_FIRST_LEG,
    )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=ADMIN_USER_ID,
        now_utc=AT_FIRST_LEG,
    )
    assert details.matches[0].is_two_legged is False
    assert details.matches[0].leg_number is None
    assert details.matches[0].prediction_score.total_points == 4
    assert details.leaderboard[0].total_points == 4

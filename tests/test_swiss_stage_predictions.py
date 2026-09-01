from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Event

import pytest

from app import contest_service
from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    ContestCompletionUnavailableError,
    PredictionUnavailableError,
    SwissStagePredictionSettingsLockedError,
    complete_contest,
    create_champions_league_2026_27_contest,
    create_world_cup_2026_contest,
    get_contest_details,
    save_swiss_stage_prediction,
    save_swiss_stage_prediction_settings,
    save_swiss_stage_result,
    save_tournament_teams,
)
from app.database import database_connection, initialize_database


CHAT_ID = -1001234567890
ADMIN_ID = 101
ALICE_ID = 202
OPEN_TIME = datetime(2029, 1, 1, tzinfo=timezone.utc)
CLOSED_TIME = datetime(2030, 1, 2, tzinfo=timezone.utc)
DEADLINE = "2030-01-01T12:00:00Z"
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=ADMIN_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "predictor.db"
    initialize_database(path)
    return path


def _create_contest(database_path: Path) -> int:
    return create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=ADMIN_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        contest_name="Швейцарский этап",
        idempotency_key="create-swiss-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest.id


def _create_champions_league_contest(database_path: Path) -> int:
    return create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=ADMIN_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        contest_name="Лига чемпионов 2026/27",
        idempotency_key="create-ucl-contest",
        audit_actor=AUDIT_ACTOR,
    ).contest.id


def _configure(
    database_path: Path,
    *,
    contest_id: int,
    team_names: list[str] | None = None,
    direct_count: int = 2,
    elimination_count: int = 2,
) -> dict[str, int]:
    names = team_names or ["Альфа", "Бета", "Гамма", "Дельта", "Эпсилон"]
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        team_names=names,
        audit_actor=AUDIT_ACTOR,
    )
    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        enabled=True,
        deadline_at=DEADLINE,
        direct_qualifier_count=direct_count,
        elimination_qualifier_count=elimination_count,
        audit_actor=AUDIT_ACTOR,
    )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=OPEN_TIME,
    )
    return {team.name: team.id for team in details.swiss_stage_prediction.candidates}


def _save_alice_prediction(
    database_path: Path,
    *,
    contest_id: int,
    teams: dict[str, int],
) -> None:
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        first_name="Алиса",
        last_name=None,
        username="alice",
        direct_team_ids=[teams["Альфа"], teams["Бета"]],
        elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
        now_utc=OPEN_TIME,
    )


def test_swiss_prediction_checks_deadline_after_acquiring_write_lock(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)
    contender_connected = Event()
    clock = {"now": datetime(2030, 1, 1, 11, 59, 59, tzinfo=timezone.utc)}
    original_database_connection = contest_service.database_connection

    @contextmanager
    def coordinated_database_connection(path: Path):
        with original_database_connection(path) as connection:
            contender_connected.set()
            yield connection

    def controlled_now_utc(_value: datetime | None) -> datetime:
        return clock["now"]

    monkeypatch.setattr(
        contest_service,
        "database_connection",
        coordinated_database_connection,
    )
    monkeypatch.setattr(contest_service, "_resolve_now_utc", controlled_now_utc)

    with database_connection(database_path) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            prediction_future = executor.submit(
                save_swiss_stage_prediction,
                database_path=database_path,
                telegram_chat_id=CHAT_ID,
                contest_id=contest_id,
                telegram_user_id=ALICE_ID,
                first_name="Алиса",
                last_name=None,
                username="alice",
                direct_team_ids=[teams["Альфа"], teams["Бета"]],
                elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
            )
            assert contender_connected.wait(timeout=5)
            clock["now"] = datetime(
                2030,
                1,
                1,
                12,
                0,
                tzinfo=timezone.utc,
            )
            lock_connection.execute("COMMIT")
            with pytest.raises(PredictionUnavailableError, match="уже закрыт"):
                prediction_future.result(timeout=5)

    with database_connection(database_path) as connection:
        prediction_count = connection.execute(
            "SELECT COUNT(*) FROM swiss_stage_predictions WHERE contest_id = ?",
            (contest_id,),
        ).fetchone()[0]
    assert prediction_count == 0


def test_swiss_stage_defaults_to_three_direct_and_five_playoff_teams(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=OPEN_TIME,
    )

    prediction = details.swiss_stage_prediction
    assert prediction.is_enabled is False
    assert prediction.direct_qualifier_count == 3
    assert prediction.elimination_qualifier_count == 5


def test_champions_league_prediction_requires_thirty_six_teams_and_eight_plus_twelve(
    database_path: Path,
) -> None:
    contest_id = _create_champions_league_contest(database_path)
    first_35_teams = [f"Команда {number:02d}" for number in range(1, 36)]
    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        team_names=first_35_teams,
        audit_actor=AUDIT_ACTOR,
    )
    with pytest.raises(PredictionUnavailableError, match="общий этап"):
        save_swiss_stage_prediction(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ALICE_ID,
            first_name="Алиса",
            last_name=None,
            username="alice",
            direct_team_ids=[],
            elimination_team_ids=[],
            now_utc=OPEN_TIME,
        )

    with pytest.raises(ValueError, match="ровно 36 команд"):
        save_swiss_stage_prediction_settings(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            enabled=True,
            deadline_at=DEADLINE,
            direct_qualifier_count=8,
            elimination_qualifier_count=12,
            audit_actor=AUDIT_ACTOR,
            now_utc=OPEN_TIME,
        )

    save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        team_names=[*first_35_teams, "Команда 36"],
        audit_actor=AUDIT_ACTOR,
    )
    with pytest.raises(ValueError, match="8 команд напрямую"):
        save_swiss_stage_prediction_settings(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            enabled=True,
            deadline_at=DEADLINE,
            direct_qualifier_count=7,
            elimination_qualifier_count=13,
            audit_actor=AUDIT_ACTOR,
            now_utc=OPEN_TIME,
        )

    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        enabled=True,
        deadline_at=DEADLINE,
        direct_qualifier_count=8,
        elimination_qualifier_count=12,
        audit_actor=AUDIT_ACTOR,
        now_utc=OPEN_TIME,
    )
    with pytest.raises(ValueError, match="ровно 36 команд"):
        save_tournament_teams(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            team_names=first_35_teams,
            audit_actor=AUDIT_ACTOR,
        )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=OPEN_TIME,
    )
    candidate_ids = [team.id for team in details.swiss_stage_prediction.candidates]
    assert len(candidate_ids) == 36
    saved = save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        first_name="Алиса",
        last_name=None,
        username="alice",
        direct_team_ids=candidate_ids[:8],
        elimination_team_ids=candidate_ids[8:20],
        now_utc=OPEN_TIME,
    )

    assert len(saved.direct_teams) == 8
    assert tuple(team.id for team in saved.playoff_teams) == tuple(candidate_ids[20:])
    assert len(saved.elimination_teams) == 12
    assert details.matches == ()

    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=candidate_ids[:8],
        elimination_team_ids=candidate_ids[8:20],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    scored = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        now_utc=CLOSED_TIME,
    )

    assert scored.swiss_stage_prediction.awarded_points == 40
    assert scored.swiss_stage_prediction.prediction is not None
    assert scored.swiss_stage_prediction.actual_result is not None
    assert tuple(
        team.id for team in scored.swiss_stage_prediction.prediction.playoff_teams
    ) == tuple(candidate_ids[20:])
    assert tuple(
        team.id for team in scored.swiss_stage_prediction.actual_result.playoff_teams
    ) == tuple(candidate_ids[20:])
    assert scored.leaderboard[0].total_points == 40

    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[*candidate_ids[:6], candidate_ids[8], candidate_ids[20]],
        elimination_team_ids=[
            *candidate_ids[9:19],
            candidate_ids[7],
            candidate_ids[21],
        ],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    corrected = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        now_utc=CLOSED_TIME,
    )
    awards_by_team_id = {
        award.team.id: award for award in corrected.swiss_stage_prediction.awards
    }
    history = corrected.leaderboard[0].swiss_stage_prediction_history

    assert corrected.swiss_stage_prediction.awarded_points == 32
    assert corrected.leaderboard[0].total_points == 32
    assert awards_by_team_id[candidate_ids[0]].points == 2
    assert awards_by_team_id[candidate_ids[7]].actual_category == "elimination"
    assert awards_by_team_id[candidate_ids[7]].points == 0
    assert awards_by_team_id[candidate_ids[8]].actual_category == "direct"
    assert awards_by_team_id[candidate_ids[8]].points == 0
    assert awards_by_team_id[candidate_ids[6]].actual_category == "playoff"
    assert awards_by_team_id[candidate_ids[6]].points == 0
    assert awards_by_team_id[candidate_ids[19]].actual_category == "playoff"
    assert awards_by_team_id[candidate_ids[19]].points == 0
    assert history is not None
    assert history.awarded_points == 32
    assert tuple(team.id for team in history.prediction.playoff_teams) == tuple(
        candidate_ids[20:]
    )
    corrected_result_selected_ids = {
        *candidate_ids[:6],
        candidate_ids[8],
        candidate_ids[20],
        *candidate_ids[9:19],
        candidate_ids[7],
        candidate_ids[21],
    }
    assert history.actual_result is not None
    assert tuple(team.id for team in history.actual_result.playoff_teams) == tuple(
        team_id
        for team_id in candidate_ids
        if team_id not in corrected_result_selected_ids
    )
    assert {award.team.id: award.points for award in history.awards} == {
        award.team.id: award.points for award in corrected.swiss_stage_prediction.awards
    }


def test_champions_league_playoff_category_is_never_scored() -> None:
    awards = contest_service._swiss_stage_awards_from_rows(
        [{"id": 1, "name": "Альфа", "category": "playoff"}],
        result_rows=[{"id": 1, "name": "Альфа", "category": "playoff"}],
        template_key="champions_league_2026_27",
    )

    assert len(awards) == 1
    assert awards[0].actual_category == "playoff"
    assert awards[0].points == 0


def test_swiss_stage_settings_create_candidates_without_matches_and_are_audited(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=OPEN_TIME,
    )
    prediction = details.swiss_stage_prediction
    assert prediction.is_enabled is True
    assert prediction.is_open is True
    assert prediction.direct_qualifier_count == 2
    assert prediction.elimination_qualifier_count == 2
    assert {team.name for team in prediction.candidates} == set(teams)
    assert details.matches == ()

    with database_connection(database_path) as connection:
        audit_row = connection.execute(
            """
            SELECT event_type, entity_type, before_state, after_state
            FROM audit_events
            WHERE contest_id = ?
              AND event_type = 'swiss_stage_settings_updated'
            """,
            (contest_id,),
        ).fetchone()
    assert audit_row is not None
    assert audit_row["entity_type"] == "swiss_stage_prediction"
    assert audit_row["before_state"] is None
    assert '"direct_qualifier_count":2' in audit_row["after_state"]


@pytest.mark.parametrize(
    ("team_names", "direct_count", "elimination_count", "message"),
    [
        ([], 1, 1, "Сначала добавьте команды турнира"),
        (["А", "Б"], 0, 1, "положительным целым"),
        (["А", "Б"], 2, 1, "не может превышать"),
    ],
)
def test_swiss_stage_settings_validate_candidates_and_limits(
    database_path: Path,
    team_names: list[str],
    direct_count: int,
    elimination_count: int,
    message: str,
) -> None:
    contest_id = _create_contest(database_path)
    if team_names:
        save_tournament_teams(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            team_names=team_names,
            audit_actor=AUDIT_ACTOR,
        )
    with pytest.raises(ValueError, match=message):
        save_swiss_stage_prediction_settings(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            enabled=True,
            deadline_at=DEADLINE,
            direct_qualifier_count=direct_count,
            elimination_qualifier_count=elimination_count,
            audit_actor=AUDIT_ACTOR,
        )


def test_swiss_stage_prediction_is_atomic_replace_and_locks_settings(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)
    _save_alice_prediction(database_path, contest_id=contest_id, teams=teams)

    replacement = save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        first_name="Алиса",
        last_name=None,
        username="alice",
        direct_team_ids=[teams["Гамма"], teams["Дельта"]],
        elimination_team_ids=[teams["Альфа"], teams["Эпсилон"]],
        now_utc=OPEN_TIME,
    )
    assert [team.name for team in replacement.direct_teams] == [
        "Гамма",
        "Дельта",
    ]
    with database_connection(database_path) as connection:
        prediction_count = connection.execute(
            "SELECT COUNT(*) FROM swiss_stage_predictions"
        ).fetchone()[0]
        selection_count = connection.execute(
            "SELECT COUNT(*) FROM swiss_stage_prediction_selections"
        ).fetchone()[0]
    assert prediction_count == 1
    assert selection_count == 4

    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        enabled=True,
        deadline_at="2030-02-01T12:00:00Z",
        direct_qualifier_count=2,
        elimination_qualifier_count=2,
        audit_actor=AUDIT_ACTOR,
        now_utc=OPEN_TIME,
    )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=OPEN_TIME,
    )
    assert details.swiss_stage_prediction.deadline_at == "2030-02-01T12:00:00Z"
    assert details.swiss_stage_prediction.settings_locked is True

    with pytest.raises(SwissStagePredictionSettingsLockedError):
        save_swiss_stage_prediction_settings(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            enabled=False,
            deadline_at=None,
            direct_qualifier_count=2,
            elimination_qualifier_count=2,
            audit_actor=AUDIT_ACTOR,
        )


def test_swiss_stage_deadline_cannot_be_changed_after_it_has_passed(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)
    _save_alice_prediction(database_path, contest_id=contest_id, teams=teams)

    with pytest.raises(
        SwissStagePredictionSettingsLockedError,
        match="нельзя изменить после его наступления",
    ):
        save_swiss_stage_prediction_settings(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            enabled=True,
            deadline_at="2031-01-01T12:00:00Z",
            direct_qualifier_count=2,
            elimination_qualifier_count=2,
            audit_actor=AUDIT_ACTOR,
            now_utc=CLOSED_TIME,
        )


@pytest.mark.parametrize(
    ("direct_ids", "elimination_ids", "message"),
    [
        (["Альфа"], ["Гамма", "Дельта"], "ровно 2"),
        (["Альфа", "Альфа"], ["Гамма", "Дельта"], "не должна повторяться"),
        (["Альфа", "Бета"], ["Бета", "Дельта"], "обеих категориях"),
    ],
)
def test_swiss_stage_prediction_validates_full_selection(
    database_path: Path,
    direct_ids: list[str],
    elimination_ids: list[str],
    message: str,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)
    with pytest.raises(ValueError, match=message):
        save_swiss_stage_prediction(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ALICE_ID,
            first_name="Алиса",
            last_name=None,
            username="alice",
            direct_team_ids=[teams[name] for name in direct_ids],
            elimination_team_ids=[teams[name] for name in elimination_ids],
            now_utc=OPEN_TIME,
        )


def test_swiss_stage_scoring_history_and_correction_recalculate_on_read(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)
    _save_alice_prediction(database_path, contest_id=contest_id, teams=teams)

    before_deadline = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=OPEN_TIME,
    )
    assert len(before_deadline.leaderboard) == 1
    assert before_deadline.leaderboard[0].total_points == 0
    assert before_deadline.leaderboard[0].swiss_stage_prediction_count == 1
    assert before_deadline.leaderboard[0].swiss_stage_prediction_history is None

    after_deadline = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=CLOSED_TIME,
    )
    history_without_result = after_deadline.leaderboard[
        0
    ].swiss_stage_prediction_history
    assert history_without_result is not None
    assert history_without_result.awarded_points is None

    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[teams["Альфа"], teams["Гамма"]],
        elimination_team_ids=[teams["Бета"], teams["Эпсилон"]],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    scored = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        now_utc=CLOSED_TIME,
    )
    assert scored.leaderboard[0].total_points == 4
    assert scored.swiss_stage_prediction.awarded_points == 4
    assert [
        (award.team.name, award.points)
        for award in scored.swiss_stage_prediction.awards
    ] == [
        ("Альфа", 2),
        ("Бета", 1),
        ("Гамма", 1),
        ("Дельта", 0),
    ]

    with database_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE swiss_stage_results
            SET updated_at = '2000-01-01T00:00:00Z'
            WHERE contest_id = ?
            """,
            (contest_id,),
        )
    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[teams["Гамма"], teams["Альфа"]],
        elimination_team_ids=[teams["Эпсилон"], teams["Бета"]],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    with database_connection(database_path) as connection:
        unchanged_result_row = connection.execute(
            """
            SELECT updated_at
            FROM swiss_stage_results
            WHERE contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
        unchanged_audit_rows = connection.execute(
            """
            SELECT event_type, COUNT(*) AS event_count
            FROM audit_events
            WHERE contest_id = ?
              AND event_type IN (
                  'swiss_stage_result_set',
                  'swiss_stage_result_changed'
              )
            GROUP BY event_type
            """,
            (contest_id,),
        ).fetchall()
    assert unchanged_result_row["updated_at"] == "2000-01-01T00:00:00Z"
    assert {
        row["event_type"]: int(row["event_count"]) for row in unchanged_audit_rows
    } == {"swiss_stage_result_set": 1}

    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[teams["Бета"], teams["Дельта"]],
        elimination_team_ids=[teams["Альфа"], teams["Гамма"]],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    corrected = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=CLOSED_TIME,
    )
    assert corrected.leaderboard[0].total_points == 6
    with database_connection(database_path) as connection:
        changed_audit_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM audit_events
            WHERE contest_id = ?
              AND event_type = 'swiss_stage_result_changed'
            """,
            (contest_id,),
        ).fetchone()[0]
    assert changed_audit_count == 1


def test_swiss_stage_exact_four_plus_four_awards_sixteen_points(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    names = [f"Команда {number}" for number in range(1, 9)]
    teams = _configure(
        database_path,
        contest_id=contest_id,
        team_names=names,
        direct_count=4,
        elimination_count=4,
    )
    direct_ids = [teams[name] for name in names[:4]]
    elimination_ids = [teams[name] for name in names[4:]]
    save_swiss_stage_prediction(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        first_name="Алиса",
        last_name=None,
        username="alice",
        direct_team_ids=direct_ids,
        elimination_team_ids=elimination_ids,
        now_utc=OPEN_TIME,
    )
    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=direct_ids,
        elimination_team_ids=elimination_ids,
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=CLOSED_TIME,
    )
    assert details.leaderboard[0].total_points == 16


def test_swiss_stage_deadline_completion_and_cascade_rules(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)
    _save_alice_prediction(database_path, contest_id=contest_id, teams=teams)

    with pytest.raises(PredictionUnavailableError, match="уже закрыт"):
        save_swiss_stage_prediction(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ALICE_ID,
            first_name="Алиса",
            last_name=None,
            username="alice",
            direct_team_ids=[teams["Альфа"], teams["Бета"]],
            elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
            now_utc=CLOSED_TIME,
        )
    with pytest.raises(
        ContestCompletionUnavailableError,
        match="фактические итоги",
    ):
        complete_contest(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=ADMIN_ID,
            first_name="Администратор",
            last_name=None,
            username="admin",
            audit_actor=AUDIT_ACTOR,
            now_utc=CLOSED_TIME,
        )

    save_swiss_stage_result(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        direct_team_ids=[teams["Альфа"], teams["Бета"]],
        elimination_team_ids=[teams["Гамма"], teams["Дельта"]],
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )
    complete_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ADMIN_ID,
        first_name="Администратор",
        last_name=None,
        username="admin",
        audit_actor=AUDIT_ACTOR,
        now_utc=CLOSED_TIME,
    )

    with database_connection(database_path) as connection:
        connection.execute("DELETE FROM contests WHERE id = ?", (contest_id,))
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM swiss_stage_predictions"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == len(
            teams
        )


def test_swiss_stage_schema_reinitialization_preserves_data(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)
    _save_alice_prediction(database_path, contest_id=contest_id, teams=teams)

    initialize_database(database_path)
    initialize_database(database_path)

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        now_utc=OPEN_TIME,
    )
    assert details.swiss_stage_prediction.prediction is not None
    assert len(details.swiss_stage_prediction.prediction.direct_teams) == 2


def test_first_swiss_prediction_and_disabling_settings_are_serialized(
    database_path: Path,
) -> None:
    contest_id = _create_contest(database_path)
    teams = _configure(database_path, contest_id=contest_id)
    barrier = Barrier(2)

    def save_prediction() -> str:
        barrier.wait()
        try:
            _save_alice_prediction(
                database_path,
                contest_id=contest_id,
                teams=teams,
            )
        except ValueError:
            return "rejected"
        return "saved"

    def disable_settings() -> str:
        barrier.wait()
        try:
            save_swiss_stage_prediction_settings(
                database_path=database_path,
                telegram_chat_id=CHAT_ID,
                contest_id=contest_id,
                enabled=False,
                deadline_at=None,
                direct_qualifier_count=2,
                elimination_qualifier_count=2,
                audit_actor=AUDIT_ACTOR,
            )
        except SwissStagePredictionSettingsLockedError:
            return "locked"
        return "disabled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        prediction_result = executor.submit(save_prediction)
        settings_result = executor.submit(disable_settings)
        outcomes = {prediction_result.result(), settings_result.result()}

    assert outcomes in ({"saved", "locked"}, {"rejected", "disabled"})
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=ALICE_ID,
        now_utc=OPEN_TIME,
    )
    if details.swiss_stage_prediction.is_enabled:
        assert details.swiss_stage_prediction.prediction is not None
        assert details.swiss_stage_prediction.settings_locked is True
    else:
        assert details.swiss_stage_prediction.prediction is None

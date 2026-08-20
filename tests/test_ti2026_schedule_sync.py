from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.database import create_connection, initialize_database
from app.shared_tournament_service import (
    create_shared_match,
    create_shared_tournament,
    get_shared_tournament_details,
    save_shared_tournament_teams,
)
from app.ti2026_schedule_sync import (
    ValveSeries,
    parse_valve_schedule,
    synchronize_ti2026_schedule,
)


OWNER_ID = 123


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _series(
    node_id: int,
    team_1: int,
    team_2: int,
    scheduled_at: str,
    *,
    started: bool = False,
    completed: bool = False,
    score: tuple[int, int] = (0, 0),
) -> ValveSeries:
    return ValveSeries(
        node_id=node_id,
        team_id_1=team_1,
        team_id_2=team_2,
        scheduled_at=_time(scheduled_at),
        best_of=3,
        has_started=started,
        is_completed=completed,
        team_1_wins=score[0],
        team_2_wins=score[1],
    )


def _create_tournament(database_path: Path):
    details = create_shared_tournament(
        database_path=database_path,
        name="The International 2026",
        template_key="the_international_2026",
        actor_telegram_user_id=OWNER_ID,
    )
    return save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        team_names=[
            "Iron Wing",
            "Team Spirit",
            "Team Liquid",
            "Team Falcons",
        ],
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )


def test_parse_valve_schedule_selects_playoff_and_validates_event() -> None:
    payload = {
        "info": {"league_id": 19719, "name": "The International 2026"},
        "node_groups": [
            {
                "name": "",
                "node_groups": [
                    {
                        "name": "Playoff",
                        "nodes": [
                            {
                                "node_id": 21,
                                "node_type": 3,
                                "team_id_1": 2163,
                                "team_id_2": 9247354,
                                "scheduled_time": 1893492000,
                                "has_started": False,
                                "is_completed": False,
                                "team_1_wins": 0,
                                "team_2_wins": 0,
                            }
                        ],
                    }
                ],
            }
        ],
    }

    parsed = parse_valve_schedule(payload)

    assert len(parsed) == 1
    assert parsed[0].node_id == 21
    assert parsed[0].best_of == 5
    assert parsed[0].team_id_1 == 2163


def test_sync_links_existing_matches_moves_deadline_and_saves_result(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    details = _create_tournament(database_path)
    teams = {team.name: team.id for team in details.teams}
    previous_match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        home_team_id=teams["Team Spirit"],
        away_team_id=teams["Iron Wing"],
        starts_at_utc="2030-01-01T10:10:00Z",
        best_of=3,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    current_match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        home_team_id=teams["Team Liquid"],
        away_team_id=teams["Team Falcons"],
        starts_at_utc="2030-01-01T10:15:00Z",
        best_of=3,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    first_feed = (
        _series(
            14,
            10150413,
            7119388,
            "2030-01-01T09:00:00Z",
            started=True,
        ),
        _series(15, 2163, 9247354, "2030-01-01T10:00:00Z"),
    )

    first_result = synchronize_ti2026_schedule(
        database_path=database_path,
        series=first_feed,
        now_utc=_time("2030-01-01T10:05:00Z"),
    )
    after_first = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
    )
    matches = {match.id: match for match in after_first.matches}

    assert first_result.linked == 2
    assert first_result.deadlines_moved == 1
    assert matches[current_match.id].starts_at_utc == "2030-01-01T10:25:00Z"

    second_feed = (
        _series(
            14,
            10150413,
            7119388,
            "2030-01-01T09:00:00Z",
            started=True,
            completed=True,
            score=(1, 2),
        ),
        _series(15, 2163, 9247354, "2030-01-01T10:00:00Z"),
    )
    second_result = synchronize_ti2026_schedule(
        database_path=database_path,
        series=second_feed,
        now_utc=_time("2030-01-01T10:12:00Z"),
    )
    after_second = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
    )
    matches = {match.id: match for match in after_second.matches}

    assert second_result.results_saved == 1
    assert matches[previous_match.id].status == "finished"
    assert matches[previous_match.id].home_score == 2
    assert matches[previous_match.id].away_score == 1
    assert matches[previous_match.id].advancing_team_id == teams["Team Spirit"]
    assert matches[current_match.id].starts_at_utc == "2030-01-01T10:25:00Z"


def test_sync_creates_rematch_by_valve_node_without_reusing_finished_match(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    details = _create_tournament(database_path)
    teams = {team.name: team.id for team in details.teams}
    old_match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        home_team_id=teams["Team Liquid"],
        away_team_id=teams["Team Falcons"],
        starts_at_utc="2030-01-01T09:00:00Z",
        best_of=3,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE shared_matches
            SET status = 'finished', home_score_final = 2,
                away_score_final = 0, advancing_team_id = ?
            WHERE id = ?
            """,
            (teams["Team Liquid"], old_match.id),
        )

    feed = (
        _series(
            14,
            2163,
            9247354,
            "2030-01-01T09:00:00Z",
            started=True,
            completed=True,
            score=(2, 0),
        ),
        _series(21, 2163, 9247354, "2030-01-02T12:00:00Z"),
    )
    result = synchronize_ti2026_schedule(
        database_path=database_path,
        series=feed,
        now_utc=_time("2030-01-01T10:00:00Z"),
    )
    after = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
    )

    assert result.linked == 1
    assert result.created == 1
    assert len(after.matches) == 2
    assert (
        sum(
            frozenset((match.home_team.name, match.away_team.name))
            == frozenset(("Team Liquid", "Team Falcons"))
            for match in after.matches
        )
        == 2
    )


def test_sync_never_overwrites_a_conflicting_finished_result(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    details = _create_tournament(database_path)
    teams = {team.name: team.id for team in details.teams}
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        home_team_id=teams["Team Liquid"],
        away_team_id=teams["Team Falcons"],
        starts_at_utc="2030-01-01T09:00:00Z",
        best_of=3,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE shared_matches
            SET status = 'finished', home_score_final = 2,
                away_score_final = 0, advancing_team_id = ?
            WHERE id = ?
            """,
            (teams["Team Liquid"], match.id),
        )

    result = synchronize_ti2026_schedule(
        database_path=database_path,
        series=(
            _series(
                14,
                2163,
                9247354,
                "2030-01-01T09:00:00Z",
                started=True,
                completed=True,
                score=(1, 2),
            ),
        ),
        now_utc=_time("2030-01-01T12:00:00Z"),
    )
    unchanged = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
    ).matches[0]

    assert result.conflicts == 1
    assert unchanged.home_score == 2
    assert unchanged.away_score == 0
    assert unchanged.advancing_team_id == teams["Team Liquid"]

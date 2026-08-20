from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    SharedTournamentManagedError,
    create_world_cup_2026_contest,
    save_champion_prediction,
    save_match_prediction,
    save_swiss_stage_prediction,
    update_match_start,
)
from app.database import create_connection, initialize_database
from app.shared_tournament_service import (
    SharedMatchConflictError,
    SharedMatchUpdateUnavailableError,
    SharedTournamentConflictError,
    create_shared_match,
    create_shared_tournament,
    delete_shared_match,
    save_shared_champion_result,
    save_shared_champion_settings,
    save_shared_match_result,
    save_shared_swiss_result,
    save_shared_swiss_settings,
    save_shared_tournament_teams,
    update_shared_match_start,
)


OWNER_ID = 123


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _create_shared_tournament(database_path: Path):
    details = create_shared_tournament(
        database_path=database_path,
        name="Чемпионат мира 2026",
        template_key="world_cup_2026",
        actor_telegram_user_id=OWNER_ID,
    )
    details = save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        team_names=["Испания", "Франция"],
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )
    return details


def _create_contest(
    database_path: Path,
    *,
    shared_tournament_id: int,
    chat_id: int,
    suffix: str,
):
    return create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=chat_id,
        chat_title=f"Чат {suffix}",
        telegram_user_id=OWNER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name=f"Прогнозы {suffix}",
        idempotency_key=f"contest-{suffix}",
        audit_actor=AuditActor(
            telegram_chat_id=chat_id,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        shared_tournament_id=shared_tournament_id,
    ).contest


def _local_match_ids(database_path: Path, shared_match_id: int) -> dict[int, int]:
    with create_connection(database_path) as connection:
        return {
            int(row["contest_id"]): int(row["match_id"])
            for row in connection.execute(
                """
                SELECT contest_id, match_id
                FROM shared_match_links
                WHERE shared_match_id = ?
                ORDER BY contest_id
                """,
                (shared_match_id,),
            )
        }


def test_shared_match_is_materialized_per_contest_and_predictions_stay_separate(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    first = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    second = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1002,
        suffix="B",
    )
    local_ids = _local_match_ids(database_path, match.id)

    assert set(local_ids) == {first.id, second.id}
    assert local_ids[first.id] != local_ids[second.id]

    for contest, chat_id, score in (
        (first, -1001, (2, 1)),
        (second, -1002, (1, 0)),
    ):
        save_match_prediction(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            match_id=local_ids[contest.id],
            telegram_user_id=777,
            first_name="Участник",
            last_name=None,
            username=None,
            predicted_home_score=score[0],
            predicted_away_score=score[1],
            predicted_advancing_team_id=shared.teams[0].id,
            now_utc=_time("2030-05-01T00:00:00Z"),
        )

    with create_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT match_id, predicted_home_score, predicted_away_score
            FROM match_predictions
            ORDER BY match_id
            """
        ).fetchall()
    assert [
        (row["predicted_home_score"], row["predicted_away_score"]) for row in rows
    ] == [
        (2, 1),
        (1, 0),
    ]


def test_shared_start_update_is_atomic_and_deadline_never_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    first = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    second = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1002,
        suffix="B",
    )

    updated = update_shared_match_start(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        starts_at_utc="2030-06-02T15:00:00Z",
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-05-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        local_times = {
            str(row["starts_at_utc"])
            for row in connection.execute(
                """
                SELECT matches.starts_at_utc
                FROM shared_match_links AS link
                JOIN matches ON matches.id = link.match_id
                WHERE link.shared_match_id = ?
                """,
                (match.id,),
            )
        }
    assert local_times == {"2030-06-02T15:00:00Z"}

    with pytest.raises(SharedMatchUpdateUnavailableError, match="уже наступил"):
        update_shared_match_start(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            shared_match_id=match.id,
            starts_at_utc="2030-06-04T15:00:00Z",
            expected_version=updated.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2030-06-03T00:00:00Z"),
        )

    local_ids = _local_match_ids(database_path, match.id)
    with pytest.raises(SharedTournamentManagedError):
        update_match_start(
            database_path=database_path,
            telegram_chat_id=-1001,
            contest_id=first.id,
            match_id=local_ids[first.id],
            telegram_user_id=OWNER_ID,
            first_name="Eugene",
            last_name="Sabir",
            username="evsab",
            starts_at_utc="2030-06-05T00:00:00Z",
            audit_actor=AuditActor(
                telegram_chat_id=-1001,
                telegram_user_id=OWNER_ID,
                role=AuditActorRole.TELEGRAM_ADMIN,
            ),
            now_utc=_time("2030-05-01T00:00:00Z"),
        )
    assert second.id in local_ids


def test_shared_result_recalculates_every_contest_and_correction_changes_scores(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    contests = [
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1001,
                suffix="A",
            ),
            -1001,
        ),
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1002,
                suffix="B",
            ),
            -1002,
        ),
    ]
    local_ids = _local_match_ids(database_path, match.id)
    for contest, chat_id in contests:
        save_match_prediction(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            match_id=local_ids[contest.id],
            telegram_user_id=777,
            first_name="Участник",
            last_name=None,
            username=None,
            predicted_home_score=2,
            predicted_away_score=1,
            predicted_advancing_team_id=shared.teams[0].id,
            now_utc=_time("2030-05-01T00:00:00Z"),
        )

    saved = save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        home_score=2,
        away_score=1,
        advancing_team_id=shared.teams[0].id,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T13:00:00Z"),
    )
    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT SUM(points) FROM match_prediction_scores"
            ).fetchone()[0]
            == 6
        )

    save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        home_score=0,
        away_score=1,
        advancing_team_id=shared.teams[1].id,
        expected_version=saved.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-01T14:00:00Z"),
    )
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


def test_shared_delete_removes_all_local_predictions_and_recreation_is_new_match(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    local_match_id = _local_match_ids(database_path, match.id)[contest.id]
    save_match_prediction(
        database_path=database_path,
        telegram_chat_id=-1001,
        contest_id=contest.id,
        match_id=local_match_id,
        telegram_user_id=777,
        first_name="Участник",
        last_name=None,
        username=None,
        predicted_home_score=1,
        predicted_away_score=0,
        predicted_advancing_team_id=shared.teams[0].id,
        now_utc=_time("2030-05-01T00:00:00Z"),
    )

    deletion = delete_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=match.id,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
    )
    assert deletion.linked_contest_count == 1
    assert deletion.deleted_prediction_count == 1

    recreated = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-07-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-06-02T00:00:00Z"),
    )
    assert recreated.id != match.id
    with create_connection(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM match_predictions").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_match_links").fetchone()[0]
            == 1
        )


def test_same_team_pair_is_unique_regardless_of_time_or_order(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T12:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with pytest.raises(SharedMatchConflictError):
        create_shared_match(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            home_team_id=shared.teams[1].id,
            away_team_id=shared.teams[0].id,
            starts_at_utc="2030-08-01T12:00:00Z",
            best_of=None,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2029-01-01T00:00:00Z"),
        )


def test_shared_champion_settings_and_corrections_reach_completed_contests(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    shared = save_shared_champion_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        points=7,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with pytest.raises(SharedTournamentConflictError, match="уже был изменён"):
        save_shared_champion_settings(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            enabled=True,
            deadline_at="2030-06-02T12:00:00Z",
            points=5,
            expected_version=shared.tournament.version - 1,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Eugene",
            actor_last_name="Sabir",
            actor_username="evsab",
            now_utc=_time("2029-01-01T00:00:00Z"),
        )
    contests = [
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1001,
                suffix="A",
            ),
            -1001,
        ),
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1002,
                suffix="B",
            ),
            -1002,
        ),
    ]
    for contest, chat_id in contests:
        save_champion_prediction(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            telegram_user_id=777,
            first_name="Участник",
            last_name=None,
            username=None,
            predicted_team_id=shared.teams[0].id,
            now_utc=_time("2030-05-01T00:00:00Z"),
        )
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET is_active = 0 WHERE id = ?", (contests[1][0].id,)
        )

    saved = save_shared_champion_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        champion_team_id=shared.teams[0].id,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-02T00:00:00Z"),
    )
    assert saved.champion_prediction.actual_champion == shared.teams[0]
    corrected = save_shared_champion_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        champion_team_id=shared.teams[1].id,
        expected_version=saved.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-03T00:00:00Z"),
    )
    assert corrected.champion_prediction.actual_champion == shared.teams[1]
    with create_connection(database_path) as connection:
        assert {
            int(row["champion_team_id"])
            for row in connection.execute("SELECT champion_team_id FROM contests")
        } == {shared.teams[1].id}


def test_shared_swiss_result_is_common_but_predictions_are_per_contest(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    shared = _create_shared_tournament(database_path)
    shared = save_shared_swiss_settings(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        enabled=True,
        deadline_at="2030-06-01T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    contests = [
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1001,
                suffix="A",
            ),
            -1001,
        ),
        (
            _create_contest(
                database_path,
                shared_tournament_id=shared.tournament.id,
                chat_id=-1002,
                suffix="B",
            ),
            -1002,
        ),
    ]
    for contest, chat_id in contests:
        save_swiss_stage_prediction(
            database_path=database_path,
            telegram_chat_id=chat_id,
            contest_id=contest.id,
            telegram_user_id=777,
            first_name="Участник",
            last_name=None,
            username=None,
            direct_team_ids=[shared.teams[0].id],
            elimination_team_ids=[shared.teams[1].id],
            now_utc=_time("2030-05-01T00:00:00Z"),
        )
    saved = save_shared_swiss_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        direct_team_ids=[shared.teams[0].id],
        elimination_team_ids=[shared.teams[1].id],
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-02T00:00:00Z"),
    )
    assert saved.swiss_stage_prediction.direct_qualifier_team_ids == (
        shared.teams[0].id,
    )
    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM swiss_stage_predictions"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM swiss_stage_results").fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM swiss_stage_result_selections"
            ).fetchone()[0]
            == 4
        )

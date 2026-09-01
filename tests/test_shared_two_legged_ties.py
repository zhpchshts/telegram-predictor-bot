from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import create_champions_league_2026_27_contest
from app.database import create_connection, initialize_database
from app.shared_tournament_service import (
    SharedMatchConflictError,
    SharedMatchUpdateUnavailableError,
    SharedTournamentCompletionUnavailableError,
    archive_shared_tournament,
    create_shared_tournament,
    create_shared_two_legged_tie,
    delete_shared_match,
    delete_shared_two_legged_tie,
    get_shared_tournament_details,
    save_shared_match_result,
    save_shared_tournament_teams,
    save_shared_two_legged_tie_result,
    update_shared_match_start,
)


OWNER_ID = 123


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _create_shared(database_path: Path):
    details = create_shared_tournament(
        database_path=database_path,
        name="Лига чемпионов 2026/27",
        template_key="champions_league_2026_27",
        actor_telegram_user_id=OWNER_ID,
    )
    return save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        team_names=["Реал", "Интер"],
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )


def _create_contest(
    database_path: Path, *, shared_tournament_id: int, chat_id: int, suffix: str
):
    return create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=chat_id,
        chat_title=f"Чат {suffix}",
        telegram_user_id=OWNER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name=f"ЛЧ {suffix}",
        idempotency_key=f"ucl-{suffix}",
        audit_actor=AuditActor(
            telegram_chat_id=chat_id,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        shared_tournament_id=shared_tournament_id,
    ).contest


def _create_tie(database_path: Path, shared):
    return create_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        first_team_id=shared.teams[0].id,
        second_team_id=shared.teams[1].id,
        first_leg_starts_at_utc="2030-06-01T12:00:00Z",
        second_leg_starts_at_utc="2030-06-08T12:00:00Z",
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )


def _save_leg_result(
    database_path: Path,
    *,
    shared_tournament_id: int,
    match,
    home_score: int,
    away_score: int,
    now_utc: str,
):
    return save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        shared_match_id=match.id,
        home_score=home_score,
        away_score=away_score,
        advancing_team_id=None,
        expected_version=match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time(now_utc),
    )


def test_shared_pair_materializes_one_local_tie_per_contest_and_attaches_later(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pair-materialization.db"
    initialize_database(database_path)
    shared = _create_shared(database_path)
    first_contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    tie = _create_tie(database_path, shared)
    second_contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1002,
        suffix="B",
    )

    with create_connection(database_path) as connection:
        local_ties = connection.execute(
            """
            SELECT link.contest_id, ties.id, ties.is_two_legged,
                   ties.first_team_id, ties.second_team_id,
                   COUNT(matches.id) AS match_count,
                   GROUP_CONCAT(matches.leg_number) AS leg_numbers
            FROM shared_tie_links AS link
            JOIN ties ON ties.id = link.tie_id
            JOIN matches ON matches.tie_id = ties.id
            WHERE link.shared_tie_id = ?
            GROUP BY link.contest_id, ties.id
            ORDER BY link.contest_id
            """,
            (tie.id,),
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert {int(row["contest_id"]) for row in local_ties} == {
        first_contest.id,
        second_contest.id,
    }
    assert all(int(row["is_two_legged"]) == 1 for row in local_ties)
    assert all(int(row["match_count"]) == 2 for row in local_ties)
    assert all(
        set(str(row["leg_numbers"]).split(",")) == {"1", "2"} for row in local_ties
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    assert len(details.matches) == 2
    assert len(details.two_legged_ties) == 1
    assert details.two_legged_ties[0].first_leg.leg_number == 1
    assert details.two_legged_ties[0].second_leg.leg_number == 2


def test_shared_pair_penalties_resolve_tie_without_changing_leg_score(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pair-penalties.db"
    initialize_database(database_path)
    shared = _create_shared(database_path)
    contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    tie = _create_tie(database_path, shared)
    first_saved = _save_leg_result(
        database_path,
        shared_tournament_id=shared.tournament.id,
        match=tie.first_leg,
        home_score=1,
        away_score=0,
        now_utc="2030-06-01T13:00:00Z",
    )
    assert first_saved.advancing_team_id is None
    second_saved = _save_leg_result(
        database_path,
        shared_tournament_id=shared.tournament.id,
        match=tie.second_leg,
        home_score=1,
        away_score=0,
        now_utc="2030-06-08T13:00:00Z",
    )
    assert second_saved.home_score == 1
    unresolved = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    ).two_legged_ties[0]
    assert unresolved.aggregate_first_team_score == 1
    assert unresolved.aggregate_second_team_score == 1
    assert unresolved.advancing_team_id is None

    saved = save_shared_two_legged_tie_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_tie_id=tie.id,
        advancing_team_id=shared.teams[0].id,
        second_leg_extra_time_home_score=0,
        second_leg_extra_time_away_score=0,
        second_leg_home_penalty_score=4,
        second_leg_away_penalty_score=5,
        expected_version=unresolved.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-08T15:00:00Z"),
    )
    assert saved.resolution_method == "penalties"
    assert saved.advancing_team_id == shared.teams[0].id
    assert saved.second_leg.home_score == 1
    assert saved.second_leg.away_score == 0

    with create_connection(database_path) as connection:
        local = connection.execute(
            """
            SELECT ties.advancing_team_id, ties.resolution_method,
                   matches.home_score_final, matches.away_score_final
            FROM shared_tie_links AS link
            JOIN ties ON ties.id = link.tie_id
            JOIN matches ON matches.tie_id = ties.id AND matches.leg_number = 2
            WHERE link.shared_tie_id = ? AND link.contest_id = ?
            """,
            (tie.id, contest.id),
        ).fetchone()
    assert local["advancing_team_id"] == shared.teams[0].id
    assert local["resolution_method"] == "penalties"
    assert (local["home_score_final"], local["away_score_final"]) == (1, 0)

    with create_connection(database_path) as connection:
        side_effects = (
            connection.execute(
                """
                SELECT COUNT(*) FROM shared_tournament_events
                WHERE shared_tie_id = ?
                  AND event_type IN (
                      'shared_tie.result_recorded',
                      'shared_tie.result_corrected'
                  )
                """,
                (tie.id,),
            ).fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM tie_prediction_scores").fetchone()[
                0
            ],
        )
    replayed = save_shared_two_legged_tie_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_tie_id=tie.id,
        advancing_team_id=shared.teams[0].id,
        second_leg_extra_time_home_score=0,
        second_leg_extra_time_away_score=0,
        second_leg_home_penalty_score=4,
        second_leg_away_penalty_score=5,
        expected_version=unresolved.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-08T15:01:00Z"),
    )
    assert replayed == saved
    with create_connection(database_path) as connection:
        assert side_effects == (
            connection.execute(
                """
                SELECT COUNT(*) FROM shared_tournament_events
                WHERE shared_tie_id = ?
                  AND event_type IN (
                      'shared_tie.result_recorded',
                      'shared_tie.result_corrected'
                  )
                """,
                (tie.id,),
            ).fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM tie_prediction_scores").fetchone()[
                0
            ],
        )


def test_shared_pair_aggregate_result_and_correction_recalculate_tie_scores(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pair-correction.db"
    initialize_database(database_path)
    shared = _create_shared(database_path)
    contest = _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    tie = _create_tie(database_path, shared)
    with create_connection(database_path) as connection:
        user_id = int(
            connection.execute(
                """
                INSERT INTO users (telegram_user_id, first_name)
                VALUES (777, 'Участник')
                """
            ).lastrowid
        )
        local_tie_id = int(
            connection.execute(
                """
                SELECT tie_id FROM shared_tie_links
                WHERE shared_tie_id = ? AND contest_id = ?
                """,
                (tie.id, contest.id),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO tie_predictions (
                tie_id, user_id, predicted_advancing_team_id
            ) VALUES (?, ?, ?)
            """,
            (local_tie_id, user_id, shared.teams[1].id),
        )

    _save_leg_result(
        database_path,
        shared_tournament_id=shared.tournament.id,
        match=tie.first_leg,
        home_score=1,
        away_score=0,
        now_utc="2030-06-01T13:00:00Z",
    )
    second = _save_leg_result(
        database_path,
        shared_tournament_id=shared.tournament.id,
        match=tie.second_leg,
        home_score=2,
        away_score=0,
        now_utc="2030-06-08T13:00:00Z",
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    ).two_legged_ties[0]
    assert details.resolution_method == "aggregate"
    assert details.advancing_team_id == shared.teams[1].id
    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT SUM(points) FROM tie_prediction_scores"
            ).fetchone()[0]
            == 1
        )

    save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=second.id,
        home_score=0,
        away_score=0,
        advancing_team_id=None,
        expected_version=second.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-08T14:00:00Z"),
    )
    corrected = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    ).two_legged_ties[0]
    assert corrected.advancing_team_id == shared.teams[0].id
    assert corrected.resolution_method == "aggregate"
    with create_connection(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM tie_prediction_scores").fetchone()[
                0
            ]
            == 0
        )


def test_shared_pair_start_order_and_member_deletion_are_protected(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pair-invariants.db"
    initialize_database(database_path)
    shared = _create_shared(database_path)
    tie = _create_tie(database_path, shared)

    with pytest.raises(SharedMatchUpdateUnavailableError, match="раньше ответного"):
        update_shared_match_start(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            shared_match_id=tie.first_leg.id,
            starts_at_utc="2030-06-09T12:00:00Z",
            expected_version=tie.first_leg.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2029-01-01T00:00:00Z"),
        )

    updated_second = update_shared_match_start(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=tie.second_leg.id,
        starts_at_utc="2030-06-10T12:00:00Z",
        expected_version=tie.second_leg.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-06-02T00:00:00Z"),
    )
    assert updated_second.starts_at_utc == "2030-06-10T12:00:00Z"
    with pytest.raises(SharedMatchConflictError, match="целиком"):
        delete_shared_match(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            shared_match_id=tie.first_leg.id,
            expected_version=tie.first_leg.version,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Eugene",
            actor_last_name="Sabir",
            actor_username="evsab",
        )


def test_shared_tournament_archive_requires_and_accepts_pair_resolution(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pair-archive.db"
    initialize_database(database_path)
    shared = _create_shared(database_path)
    tie = _create_tie(database_path, shared)
    _save_leg_result(
        database_path,
        shared_tournament_id=shared.tournament.id,
        match=tie.first_leg,
        home_score=1,
        away_score=0,
        now_utc="2030-06-01T13:00:00Z",
    )
    _save_leg_result(
        database_path,
        shared_tournament_id=shared.tournament.id,
        match=tie.second_leg,
        home_score=1,
        away_score=0,
        now_utc="2030-06-08T13:00:00Z",
    )
    unresolved = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    with pytest.raises(
        SharedTournamentCompletionUnavailableError,
        match="двухматчевые противостояния",
    ):
        archive_shared_tournament(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            expected_version=unresolved.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2030-06-08T14:00:00Z"),
        )
    save_shared_two_legged_tie_result(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_tie_id=tie.id,
        advancing_team_id=shared.teams[0].id,
        second_leg_extra_time_home_score=0,
        second_leg_extra_time_away_score=0,
        second_leg_home_penalty_score=4,
        second_leg_away_penalty_score=5,
        expected_version=unresolved.two_legged_ties[0].version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
        now_utc=_time("2030-06-08T15:00:00Z"),
    )
    complete = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    archived = archive_shared_tournament(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        expected_version=complete.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-06-08T16:00:00Z"),
    )
    assert archived.tournament.is_archived is True


def test_shared_pair_delete_removes_both_legs_and_all_local_ties(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "pair-delete.db"
    initialize_database(database_path)
    shared = _create_shared(database_path)
    _create_contest(
        database_path,
        shared_tournament_id=shared.tournament.id,
        chat_id=-1001,
        suffix="A",
    )
    tie = _create_tie(database_path, shared)

    result = delete_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_tie_id=tie.id,
        expected_version=tie.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Eugene",
        actor_last_name="Sabir",
        actor_username="evsab",
    )
    assert result.linked_contest_count == 1
    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_two_legged_ties"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_matches").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_tie_links").fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM ties").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

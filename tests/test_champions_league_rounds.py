from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    create_champions_league_2026_27_contest,
    create_match,
    create_two_legged_tie,
    get_contest_details,
    save_tournament_teams,
)
from app.database import create_connection, initialize_database


CHAT_ID = -1001
USER_ID = 123
ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_local_ucl_entities_keep_canonical_round_names_and_formats(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "rounds.db"
    initialize_database(database_path)
    contest = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Чат ЛЧ",
        telegram_user_id=USER_ID,
        first_name="Owner",
        last_name=None,
        username=None,
        contest_name="Прогнозы ЛЧ",
        idempotency_key="ucl-rounds",
        audit_actor=ACTOR,
    ).contest
    teams = save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        team_names=["Alpha FC", "Beta FC", "Gamma FC", "Delta FC"],
        audit_actor=ACTOR,
    ).teams

    tie = create_two_legged_tie(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=USER_ID,
        first_name="Owner",
        last_name=None,
        username=None,
        first_team_id=teams[0].id,
        second_team_id=teams[1].id,
        first_leg_starts_at_utc="2030-04-01T18:00:00Z",
        second_leg_starts_at_utc="2030-04-08T18:00:00Z",
        idempotency_key="quarterfinal-1",
        audit_actor=ACTOR,
        now_utc=_time("2029-01-01T00:00:00Z"),
        round_key="quarterfinal",
    ).tie
    final = create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=USER_ID,
        first_name="Owner",
        last_name=None,
        username=None,
        home_team_id=teams[2].id,
        away_team_id=teams[3].id,
        starts_at_utc="2030-05-25T18:00:00Z",
        best_of=None,
        idempotency_key="final-1",
        audit_actor=ACTOR,
        round_key="final",
    ).match
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=USER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )

    assert (tie.round_key, tie.round_name, tie.round_position) == (
        "quarterfinal",
        "1/4 финала",
        30,
    )
    assert (final.round_key, final.round_name, final.round_position) == (
        "final",
        "Финал",
        50,
    )
    assert {item.round_key for item in details.two_legged_ties} == {"quarterfinal"}
    with create_connection(database_path) as connection:
        stages = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT stage_key, name, position, stage_type
                FROM stages
                WHERE stage_key IS NOT NULL
                ORDER BY position
                """
            ).fetchall()
        ]
    assert stages == [
        ("quarterfinal", "1/4 финала", 30, "knockout"),
        ("final", "Финал", 50, "final"),
    ]

    with pytest.raises(ValueError, match="Финал"):
        create_two_legged_tie(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest.id,
            telegram_user_id=USER_ID,
            first_name="Owner",
            last_name=None,
            username=None,
            first_team_id=teams[2].id,
            second_team_id=teams[3].id,
            first_leg_starts_at_utc="2030-05-01T18:00:00Z",
            second_leg_starts_at_utc="2030-05-08T18:00:00Z",
            idempotency_key="invalid-final-pair",
            audit_actor=ACTOR,
            now_utc=_time("2029-01-01T00:00:00Z"),
            round_key="final",
        )
    with pytest.raises(ValueError, match="двухматчевыми"):
        create_match(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest.id,
            telegram_user_id=USER_ID,
            first_name="Owner",
            last_name=None,
            username=None,
            home_team_id=teams[0].id,
            away_team_id=teams[2].id,
            starts_at_utc="2030-05-01T18:00:00Z",
            best_of=None,
            idempotency_key="invalid-semifinal-single",
            audit_actor=ACTOR,
            round_key="semifinal",
        )

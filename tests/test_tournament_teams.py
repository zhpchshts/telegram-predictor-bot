from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    TournamentTeamsLockedError,
    create_match,
    create_world_cup_2026_contest,
    get_contest_details,
    save_champion_prediction_settings,
    save_swiss_stage_prediction_settings,
    save_tournament_teams,
)
from app.database import create_connection, initialize_database


CHAT_ID = -1009876543210
USER_ID = 123
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


def _create_contest(database_path: Path, *, suffix: str = "1") -> int:
    return create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Тестовый чат",
        telegram_user_id=USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        contest_name=f"Турнир {suffix}",
        idempotency_key=f"contest-{suffix}",
        audit_actor=AUDIT_ACTOR,
    ).contest.id


def _save_teams(
    database_path: Path,
    *,
    contest_id: int,
    names: list[str],
):
    return save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        team_names=names,
        audit_actor=AUDIT_ACTOR,
    )


def test_tournament_teams_replace_normalize_reuse_and_audit(tmp_path: Path) -> None:
    database_path = tmp_path / "teams.db"
    initialize_database(database_path)
    first_contest_id = _create_contest(database_path)
    second_contest_id = _create_contest(database_path, suffix="2")

    first = _save_teams(
        database_path,
        contest_id=first_contest_id,
        names=["  Team   Liquid  ", "", "Team Spirit", "PARIVISION"],
    )
    assert [team.name for team in first.teams] == [
        "Team Liquid",
        "Team Spirit",
        "PARIVISION",
    ]
    assert first.is_locked is False

    identical = _save_teams(
        database_path,
        contest_id=first_contest_id,
        names=["Team Liquid", "Team Spirit", "PARIVISION"],
    )
    assert identical == first

    replaced = _save_teams(
        database_path,
        contest_id=first_contest_id,
        names=["PARIVISION", "Team Liquid"],
    )
    isolated = _save_teams(
        database_path,
        contest_id=second_contest_id,
        names=["Team Liquid", "Tundra Esports"],
    )
    assert [team.name for team in replaced.teams] == [
        "PARIVISION",
        "Team Liquid",
    ]
    assert isolated.teams[0].id == replaced.teams[1].id

    with create_connection(database_path) as connection:
        team_rows = connection.execute(
            "SELECT id, name FROM teams ORDER BY id"
        ).fetchall()
        audit_rows = connection.execute(
            """
            SELECT before_state, after_state
            FROM audit_events
            WHERE event_type = 'tournament_teams_updated'
              AND contest_id = ?
            ORDER BY id
            """,
            (first_contest_id,),
        ).fetchall()
    assert len([row for row in team_rows if row["name"] == "Team Liquid"]) == 1
    assert len(audit_rows) == 2
    assert json.loads(audit_rows[1]["before_state"])["teams"][0]["name"] == (
        "Team Liquid"
    )
    assert json.loads(audit_rows[1]["after_state"])["teams"][0]["name"] == (
        "PARIVISION"
    )


@pytest.mark.parametrize(
    "names",
    [
        [],
        ["", "  "],
        ["Team Liquid", " team liquid "],
    ],
)
def test_tournament_team_validation(names: list[str], tmp_path: Path) -> None:
    database_path = tmp_path / "validation.db"
    initialize_database(database_path)
    contest_id = _create_contest(database_path)

    with pytest.raises(ValueError):
        _save_teams(database_path, contest_id=contest_id, names=names)


def test_match_uses_contest_team_ids_and_locks_list(tmp_path: Path) -> None:
    database_path = tmp_path / "matches.db"
    initialize_database(database_path)
    contest_id = _create_contest(database_path)
    other_contest_id = _create_contest(database_path, suffix="2")
    teams = _save_teams(
        database_path,
        contest_id=contest_id,
        names=["Альфа", "Бета"],
    ).teams
    other_team = _save_teams(
        database_path,
        contest_id=other_contest_id,
        names=["Гамма"],
    ).teams[0]

    match = create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        home_team_id=teams[0].id,
        away_team_id=teams[1].id,
        starts_at_utc="2030-01-01T12:00:00Z",
        idempotency_key="match",
        audit_actor=AUDIT_ACTOR,
    )
    assert match.match.home_team_name == "Альфа"
    assert get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
    ).tournament_teams.lock_reasons == ("match_exists",)

    with pytest.raises(TournamentTeamsLockedError):
        _save_teams(
            database_path,
            contest_id=contest_id,
            names=["Альфа", "Бета", "Гамма"],
        )
    with pytest.raises(ValueError, match="список команд турнира"):
        create_match(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest_id,
            telegram_user_id=USER_ID,
            first_name="Admin",
            last_name=None,
            username="admin",
            home_team_id=teams[0].id,
            away_team_id=other_team.id,
            starts_at_utc="2030-01-02T12:00:00Z",
            idempotency_key="foreign-team",
            audit_actor=AUDIT_ACTOR,
        )


def test_long_term_predictions_use_tournament_teams_before_matches(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "long-term.db"
    initialize_database(database_path)
    contest_id = _create_contest(database_path)
    teams = _save_teams(
        database_path,
        contest_id=contest_id,
        names=["Альфа", "Бета", "Гамма"],
    ).teams

    save_champion_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        enabled=True,
        deadline_at="2030-01-01T12:00:00Z",
        points=5,
        audit_actor=AUDIT_ACTOR,
    )
    save_swiss_stage_prediction_settings(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        enabled=True,
        deadline_at="2030-01-01T12:00:00Z",
        direct_qualifier_count=1,
        elimination_qualifier_count=1,
        audit_actor=AUDIT_ACTOR,
    )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        now_utc=None,
    )
    assert details.matches == ()
    assert details.champion_prediction.candidates == teams
    assert details.swiss_stage_prediction.candidates == teams


def test_existing_contests_are_backfilled_without_data_loss(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.db"
    initialize_database(database_path)
    contest_id = _create_contest(database_path)
    match = create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=USER_ID,
        first_name="Admin",
        last_name=None,
        username="admin",
        home_team_name="Матчевая 1",
        away_team_name="Матчевая 2",
        starts_at_utc="2030-01-01T12:00:00Z",
        idempotency_key="legacy-match",
        audit_actor=AUDIT_ACTOR,
    ).match
    with create_connection(database_path) as connection:
        prediction_team_id = int(
            connection.execute(
                "INSERT INTO teams (name) VALUES ('Прогнозная')"
            ).lastrowid
        )
        champion_team_id = int(
            connection.execute(
                "INSERT INTO teams (name) VALUES ('Фактическая')"
            ).lastrowid
        )
        user_id = int(
            connection.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?",
                (USER_ID,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO champion_predictions (
                contest_id, user_id, predicted_team_id
            )
            VALUES (?, ?, ?)
            """,
            (contest_id, user_id, prediction_team_id),
        )
        connection.execute(
            "UPDATE contests SET champion_team_id = ? WHERE id = ?",
            (champion_team_id, contest_id),
        )
        connection.execute(
            "DELETE FROM contest_teams WHERE contest_id = ?",
            (contest_id,),
        )

    initialize_database(database_path)
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT teams.id, teams.name
            FROM contest_teams
            JOIN teams ON teams.id = contest_teams.team_id
            WHERE contest_teams.contest_id = ?
            ORDER BY contest_teams.position
            """,
            (contest_id,),
        ).fetchall()
        match_count = connection.execute(
            "SELECT COUNT(*) FROM matches WHERE id = ?",
            (match.id,),
        ).fetchone()[0]
        prediction_count = connection.execute(
            "SELECT COUNT(*) FROM champion_predictions WHERE contest_id = ?",
            (contest_id,),
        ).fetchone()[0]
        foreign_key_target = {
            row["table"]
            for row in connection.execute(
                "PRAGMA foreign_key_list(swiss_stage_prediction_selections)"
            )
        }
    assert [row["name"] for row in rows] == [
        "Матчевая 1",
        "Матчевая 2",
        "Прогнозная",
        "Фактическая",
    ]
    assert match_count == 1
    assert prediction_count == 1
    assert "contest_teams" in foreign_key_target


def test_legacy_swiss_selection_foreign_keys_are_migrated_safely(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-swiss.db"
    initialize_database(database_path)
    contest_id = _create_contest(database_path)
    teams = _save_teams(
        database_path,
        contest_id=contest_id,
        names=["Альфа", "Бета"],
    ).teams

    with create_connection(database_path) as connection:
        user_id = int(
            connection.execute(
                "SELECT id FROM users WHERE telegram_user_id = ?",
                (USER_ID,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO swiss_stage_prediction_settings (
                contest_id,
                enabled,
                deadline_at,
                direct_qualifier_count,
                elimination_qualifier_count
            )
            VALUES (?, 1, '2030-01-01T12:00:00Z', 1, 1)
            """,
            (contest_id,),
        )
        connection.executemany(
            """
            INSERT INTO swiss_stage_prediction_candidates (
                contest_id, team_id, position
            )
            VALUES (?, ?, ?)
            """,
            [
                (contest_id, teams[0].id, 0),
                (contest_id, teams[1].id, 1),
            ],
        )
        prediction_id = int(
            connection.execute(
                """
                INSERT INTO swiss_stage_predictions (contest_id, user_id)
                VALUES (?, ?)
                """,
                (contest_id, user_id),
            ).lastrowid
        )
        connection.execute(
            "INSERT INTO swiss_stage_results (contest_id) VALUES (?)",
            (contest_id,),
        )

    connection = create_connection(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            f"""
            DROP TABLE swiss_stage_prediction_selections;
            DROP TABLE swiss_stage_result_selections;

            CREATE TABLE swiss_stage_prediction_selections (
                prediction_id INTEGER NOT NULL,
                contest_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('direct', 'elimination')),
                PRIMARY KEY (prediction_id, team_id),
                FOREIGN KEY (prediction_id, contest_id)
                    REFERENCES swiss_stage_predictions(id, contest_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (contest_id, team_id)
                    REFERENCES swiss_stage_prediction_candidates(
                        contest_id, team_id
                    )
                    ON DELETE CASCADE
            );
            CREATE TABLE swiss_stage_result_selections (
                contest_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('direct', 'elimination')),
                PRIMARY KEY (contest_id, team_id),
                FOREIGN KEY (contest_id)
                    REFERENCES swiss_stage_results(contest_id)
                    ON DELETE CASCADE,
                FOREIGN KEY (contest_id, team_id)
                    REFERENCES swiss_stage_prediction_candidates(
                        contest_id, team_id
                    )
                    ON DELETE CASCADE
            );
            INSERT INTO swiss_stage_prediction_selections (
                prediction_id, contest_id, team_id, category
            )
            VALUES ({prediction_id}, {contest_id}, {teams[0].id}, 'direct');
            INSERT INTO swiss_stage_result_selections (
                contest_id, team_id, category
            )
            VALUES ({contest_id}, {teams[1].id}, 'elimination');
            """
        )
    finally:
        connection.close()

    initialize_database(database_path)
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        prediction_rows = connection.execute(
            """
            SELECT prediction_id, contest_id, team_id, category
            FROM swiss_stage_prediction_selections
            """
        ).fetchall()
        result_rows = connection.execute(
            """
            SELECT contest_id, team_id, category
            FROM swiss_stage_result_selections
            """
        ).fetchall()
        prediction_targets = {
            row["table"]
            for row in connection.execute(
                "PRAGMA foreign_key_list(swiss_stage_prediction_selections)"
            )
        }
        result_targets = {
            row["table"]
            for row in connection.execute(
                "PRAGMA foreign_key_list(swiss_stage_result_selections)"
            )
        }
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert [tuple(row) for row in prediction_rows] == [
        (prediction_id, contest_id, teams[0].id, "direct")
    ]
    assert [tuple(row) for row in result_rows] == [
        (contest_id, teams[1].id, "elimination")
    ]
    assert "contest_teams" in prediction_targets
    assert "contest_teams" in result_targets
    assert foreign_key_errors == []


def test_first_match_and_team_replacement_are_serialized(tmp_path: Path) -> None:
    database_path = tmp_path / "concurrency.db"
    initialize_database(database_path)
    contest_id = _create_contest(database_path)
    teams = _save_teams(
        database_path,
        contest_id=contest_id,
        names=["Альфа", "Бета"],
    ).teams

    def create_first_match() -> str:
        try:
            create_match(
                database_path=database_path,
                telegram_chat_id=CHAT_ID,
                contest_id=contest_id,
                telegram_user_id=USER_ID,
                first_name="Admin",
                last_name=None,
                username="admin",
                home_team_id=teams[0].id,
                away_team_id=teams[1].id,
                starts_at_utc="2030-01-01T12:00:00Z",
                idempotency_key="race-match",
                audit_actor=AUDIT_ACTOR,
            )
        except ValueError:
            return "rejected"
        return "created"

    def replace_teams() -> str:
        try:
            _save_teams(
                database_path,
                contest_id=contest_id,
                names=["Гамма", "Дельта"],
            )
        except TournamentTeamsLockedError:
            return "locked"
        return "replaced"

    with ThreadPoolExecutor(max_workers=2) as executor:
        match_future = executor.submit(create_first_match)
        replacement_future = executor.submit(replace_teams)
        outcomes = {match_future.result(), replacement_future.result()}

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest_id,
    )
    if details.matches:
        assert [team.name for team in details.tournament_teams.teams] == [
            "Альфа",
            "Бета",
        ]
        assert outcomes == {"created", "locked"}
    else:
        assert [team.name for team in details.tournament_teams.teams] == [
            "Гамма",
            "Дельта",
        ]
        assert outcomes == {"rejected", "replaced"}

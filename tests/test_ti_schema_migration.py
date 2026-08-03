from __future__ import annotations

from pathlib import Path
import sqlite3

from scripts.migrate_ti_2026_schema import migrate_database


HISTORICAL_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE chats (
    id INTEGER PRIMARY KEY,
    telegram_chat_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    short_name TEXT,
    country_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE contests (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    champion_prediction_enabled INTEGER NOT NULL DEFAULT 0,
    champion_prediction_deadline_at TEXT,
    champion_prediction_points INTEGER NOT NULL DEFAULT 5,
    champion_team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
    match_prediction_publication_enabled INTEGER NOT NULL DEFAULT 0,
    match_prediction_publication_enabled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE competitions (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    season TEXT NOT NULL,
    competition_type TEXT NOT NULL CHECK (
        competition_type IN ('world_cup', 'champions_league', 'europa_league', 'other')
    ),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (contest_id, name, season)
);

CREATE INDEX idx_competitions_contest_id ON competitions(contest_id);

CREATE TABLE scoring_rule_sets (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    exact_score_points INTEGER NOT NULL DEFAULT 3,
    goal_difference_points INTEGER NOT NULL DEFAULT 2,
    outcome_points INTEGER NOT NULL DEFAULT 1,
    advancing_team_points INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (competition_id, version)
);

CREATE TABLE stages (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    stage_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (competition_id, position)
);

CREATE TABLE matches (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
    scoring_rule_set_id INTEGER NOT NULL REFERENCES scoring_rule_sets(id),
    home_team_id INTEGER NOT NULL REFERENCES teams(id),
    away_team_id INTEGER NOT NULL REFERENCES teams(id),
    starts_at_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    home_score_final INTEGER,
    away_score_final INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def test_migration_preserves_existing_data_and_expands_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(HISTORICAL_SCHEMA)
    connection.executescript(
        """
        INSERT INTO chats (id, telegram_chat_id, title)
        VALUES (1, -1001, 'Predictions');
        INSERT INTO teams (id, name) VALUES (1, 'Home'), (2, 'Away');
        INSERT INTO contests (id, chat_id, name, slug)
        VALUES (1, 1, 'World Cup', 'world-cup');
        INSERT INTO competitions (
            id, contest_id, name, season, competition_type
        )
        VALUES (1, 1, 'World Cup', '2026', 'world_cup');
        INSERT INTO scoring_rule_sets (id, competition_id, version)
        VALUES (1, 1, 1);
        INSERT INTO stages (
            id, competition_id, name, position, stage_type
        )
        VALUES (1, 1, 'Playoffs', 1, 'knockout');
        INSERT INTO matches (
            id,
            stage_id,
            scoring_rule_set_id,
            home_team_id,
            away_team_id,
            starts_at_utc
        )
        VALUES (1, 1, 1, 1, 2, '2026-08-10T12:00:00Z');
        """
    )
    connection.close()

    migrate_database(database_path)
    migrate_database(database_path)

    connection = sqlite3.connect(database_path)
    try:
        contest = connection.execute(
            "SELECT id, name, template_key FROM contests"
        ).fetchone()
        match = connection.execute("SELECT id, best_of FROM matches").fetchone()
        competition = connection.execute(
            "SELECT id, name, competition_type FROM competitions"
        ).fetchone()

        connection.execute(
            """
            INSERT INTO competitions (
                contest_id, name, season, competition_type
            )
            VALUES (1, 'The International', '2026', 'the_international')
            """
        )
        connection.execute("UPDATE matches SET best_of = 3 WHERE id = 1")
        connection.execute(
            """
            INSERT INTO champion_prediction_candidates (
                contest_id, team_id, position
            )
            VALUES (1, 1, 0)
            """
        )

        assert contest == (1, "World Cup", "world_cup_2026")
        assert match == (1, None)
        assert competition == (1, "World Cup", "world_cup")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()

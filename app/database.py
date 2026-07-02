from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY,
    telegram_chat_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT NOT NULL,
    last_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contests (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contest_creation_requests (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chat_id, actor_user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS competitions (
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

CREATE TABLE IF NOT EXISTS scoring_rule_sets (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    exact_score_points INTEGER NOT NULL DEFAULT 3 CHECK (exact_score_points >= 0),
    goal_difference_points INTEGER NOT NULL DEFAULT 2 CHECK (
        goal_difference_points >= 0
    ),
    outcome_points INTEGER NOT NULL DEFAULT 1 CHECK (outcome_points >= 0),
    advancing_team_points INTEGER NOT NULL DEFAULT 1 CHECK (
        advancing_team_points >= 0
    ),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (competition_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_scoring_rule_sets_active_competition
    ON scoring_rule_sets(competition_id)
    WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS stages (
    id INTEGER PRIMARY KEY,
    competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    stage_type TEXT NOT NULL CHECK (
        stage_type IN ('league', 'group', 'knockout', 'final', 'other')
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (competition_id, position)
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    short_name TEXT,
    country_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ties (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
    scoring_rule_set_id INTEGER NOT NULL REFERENCES scoring_rule_sets(id),
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    is_two_legged INTEGER NOT NULL DEFAULT 0 CHECK (is_two_legged IN (0, 1)),
    advancing_team_id INTEGER REFERENCES teams(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (stage_id, position)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    stage_id INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
    tie_id INTEGER REFERENCES ties(id) ON DELETE SET NULL,
    scoring_rule_set_id INTEGER NOT NULL REFERENCES scoring_rule_sets(id),
    home_team_id INTEGER NOT NULL REFERENCES teams(id),
    away_team_id INTEGER NOT NULL REFERENCES teams(id),
    starts_at_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (
        status IN ('scheduled', 'started', 'finished', 'cancelled')
    ),
    home_score_final INTEGER,
    away_score_final INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (home_team_id != away_team_id),
    CHECK (
        (home_score_final IS NULL AND away_score_final IS NULL)
        OR (
            home_score_final IS NOT NULL
            AND away_score_final IS NOT NULL
            AND home_score_final >= 0
            AND away_score_final >= 0
        )
    )
);

CREATE TABLE IF NOT EXISTS match_creation_requests (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (contest_id, actor_user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS match_predictions (
    id INTEGER PRIMARY KEY,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    predicted_home_score INTEGER NOT NULL CHECK (predicted_home_score >= 0),
    predicted_away_score INTEGER NOT NULL CHECK (predicted_away_score >= 0),
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (match_id, user_id)
);

CREATE TABLE IF NOT EXISTS tie_predictions (
    id INTEGER PRIMARY KEY,
    tie_id INTEGER NOT NULL REFERENCES ties(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    predicted_advancing_team_id INTEGER NOT NULL REFERENCES teams(id),
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tie_id, user_id)
);

CREATE TABLE IF NOT EXISTS match_prediction_scores (
    id INTEGER PRIMARY KEY,
    match_prediction_id INTEGER NOT NULL REFERENCES match_predictions(id)
        ON DELETE CASCADE,
    scoring_rule_set_id INTEGER NOT NULL REFERENCES scoring_rule_sets(id),
    score_type TEXT NOT NULL CHECK (
        score_type IN ('exact_score', 'goal_difference', 'outcome')
    ),
    points INTEGER NOT NULL CHECK (points >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (match_prediction_id, score_type)
);

CREATE TABLE IF NOT EXISTS tie_prediction_scores (
    id INTEGER PRIMARY KEY,
    tie_prediction_id INTEGER NOT NULL REFERENCES tie_predictions(id)
        ON DELETE CASCADE,
    scoring_rule_set_id INTEGER NOT NULL REFERENCES scoring_rule_sets(id),
    points INTEGER NOT NULL CHECK (points >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tie_prediction_id)
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contests_chat_id
    ON contests(chat_id);

DROP INDEX IF EXISTS idx_contests_active_chat_id;

CREATE INDEX IF NOT EXISTS idx_competitions_contest_id
    ON competitions(contest_id);

CREATE INDEX IF NOT EXISTS idx_stages_competition_id
    ON stages(competition_id);

CREATE INDEX IF NOT EXISTS idx_ties_stage_id
    ON ties(stage_id);

CREATE INDEX IF NOT EXISTS idx_matches_stage_id
    ON matches(stage_id);

CREATE INDEX IF NOT EXISTS idx_matches_tie_id
    ON matches(tie_id);

CREATE INDEX IF NOT EXISTS idx_matches_starts_at_utc
    ON matches(starts_at_utc);

CREATE INDEX IF NOT EXISTS idx_match_predictions_match_id
    ON match_predictions(match_id);

CREATE INDEX IF NOT EXISTS idx_match_predictions_user_id
    ON match_predictions(user_id);

CREATE INDEX IF NOT EXISTS idx_tie_predictions_tie_id
    ON tie_predictions(tie_id);

CREATE INDEX IF NOT EXISTS idx_tie_predictions_user_id
    ON tie_predictions(user_id);

CREATE INDEX IF NOT EXISTS idx_event_log_contest_id
    ON event_log(contest_id);
"""


def create_connection(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def initialize_database(database_path: Path) -> None:
    with create_connection(database_path) as connection:
        connection.executescript(SCHEMA)


@contextmanager
def database_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    connection = create_connection(database_path)

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

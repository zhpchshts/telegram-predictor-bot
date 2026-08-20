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

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
    app_button_text TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (length(trim(app_button_text)) BETWEEN 1 AND 64)
);

CREATE TABLE IF NOT EXISTS telegram_chat_migrations (
    old_telegram_chat_id INTEGER PRIMARY KEY,
    new_telegram_chat_id INTEGER NOT NULL UNIQUE,
    migrated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (old_telegram_chat_id != new_telegram_chat_id)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT NOT NULL,
    last_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS supermoderator_assignments (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_by_user_id INTEGER NOT NULL REFERENCES users(id),
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_by_user_id INTEGER REFERENCES users(id),
    revoked_at TEXT,
    CHECK (
        (revoked_by_user_id IS NULL AND revoked_at IS NULL)
        OR
        (revoked_by_user_id IS NOT NULL AND revoked_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_supermoderator_assignments_active_chat_user
    ON supermoderator_assignments(chat_id, user_id)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_supermoderator_assignments_chat_user_history
    ON supermoderator_assignments(chat_id, user_id, assigned_at, id);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    actor_user_id INTEGER NOT NULL,
    actor_role TEXT NOT NULL CHECK (
        actor_role IN (
            'telegram_admin',
            'supermoderator',
            'participant'
        )
    ),
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    contest_id INTEGER,
    before_state TEXT,
    after_state TEXT,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_events_chat_created
    ON audit_events(chat_id, created_at, id);

CREATE INDEX IF NOT EXISTS idx_audit_events_contest_created
    ON audit_events(contest_id, created_at, id);

CREATE INDEX IF NOT EXISTS idx_audit_events_event_type
    ON audit_events(event_type);

CREATE TABLE IF NOT EXISTS contests (
  id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  template_key TEXT NOT NULL DEFAULT 'world_cup_2026' CHECK (
    template_key IN ('world_cup_2026', 'the_international_2026')
  ),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  champion_prediction_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (champion_prediction_enabled IN (0, 1)),
  champion_prediction_deadline_at TEXT,
  champion_prediction_points INTEGER NOT NULL DEFAULT 5
    CHECK (champion_prediction_points >= 0),
  champion_team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
  match_prediction_publication_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (match_prediction_publication_enabled IN (0, 1)),
  match_prediction_publication_enabled_at TEXT,
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
        competition_type IN (
            'world_cup',
            'champions_league',
            'europa_league',
            'the_international',
            'other'
        )
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

CREATE TABLE IF NOT EXISTS shared_tournaments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    template_key TEXT NOT NULL CHECK (
        template_key IN ('world_cup_2026', 'the_international_2026')
    ),
    is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by_telegram_user_id INTEGER NOT NULL CHECK (
        created_by_telegram_user_id > 0
    ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_tournaments_active_name
    ON shared_tournaments(lower(name))
    WHERE is_archived = 0;

CREATE TABLE IF NOT EXISTS shared_tournament_settings (
    shared_tournament_id INTEGER PRIMARY KEY
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    champion_prediction_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (champion_prediction_enabled IN (0, 1)),
    champion_prediction_deadline_at TEXT,
    champion_prediction_points INTEGER NOT NULL DEFAULT 5
        CHECK (champion_prediction_points >= 0),
    champion_team_id INTEGER REFERENCES teams(id),
    swiss_stage_prediction_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (swiss_stage_prediction_enabled IN (0, 1)),
    swiss_stage_prediction_deadline_at TEXT,
    swiss_direct_qualifier_count INTEGER NOT NULL DEFAULT 3
        CHECK (swiss_direct_qualifier_count > 0),
    swiss_elimination_qualifier_count INTEGER NOT NULL DEFAULT 5
        CHECK (swiss_elimination_qualifier_count > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shared_tournament_teams (
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (shared_tournament_id, team_id),
    UNIQUE (shared_tournament_id, position)
);

CREATE TABLE IF NOT EXISTS shared_swiss_stage_result_selections (
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    category TEXT NOT NULL CHECK (category IN ('direct', 'elimination')),
    PRIMARY KEY (shared_tournament_id, team_id),
    FOREIGN KEY (shared_tournament_id, team_id)
        REFERENCES shared_tournament_teams(shared_tournament_id, team_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS shared_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    home_team_id INTEGER NOT NULL REFERENCES teams(id),
    away_team_id INTEGER NOT NULL REFERENCES teams(id),
    starts_at_utc TEXT NOT NULL,
    best_of INTEGER CHECK (best_of IS NULL OR best_of IN (3, 5)),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (
        status IN ('scheduled', 'started', 'finished', 'cancelled')
    ),
    home_score_final INTEGER,
    away_score_final INTEGER,
    advancing_team_id INTEGER REFERENCES teams(id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (home_team_id != away_team_id),
    CHECK (
        (home_score_final IS NULL AND away_score_final IS NULL)
        OR (
            home_score_final IS NOT NULL
            AND away_score_final IS NOT NULL
            AND home_score_final >= 0
            AND away_score_final >= 0
        )
    ),
    CHECK (
        advancing_team_id IS NULL
        OR advancing_team_id = home_team_id
        OR advancing_team_id = away_team_id
    )
);

CREATE INDEX IF NOT EXISTS idx_shared_matches_tournament_start
    ON shared_matches(shared_tournament_id, starts_at_utc, id);

CREATE TABLE IF NOT EXISTS contest_shared_tournaments (
    contest_id INTEGER PRIMARY KEY
        REFERENCES contests(id) ON DELETE CASCADE,
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id),
    linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (shared_tournament_id, contest_id)
);

CREATE INDEX IF NOT EXISTS idx_contest_shared_tournaments_tournament
    ON contest_shared_tournaments(shared_tournament_id, contest_id);

CREATE TABLE IF NOT EXISTS shared_match_links (
    shared_match_id INTEGER NOT NULL
        REFERENCES shared_matches(id) ON DELETE CASCADE,
    match_id INTEGER NOT NULL
        REFERENCES matches(id) ON DELETE CASCADE,
    contest_id INTEGER NOT NULL
        REFERENCES contests(id) ON DELETE CASCADE,
    linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (shared_match_id, contest_id),
    UNIQUE (match_id)
);

CREATE INDEX IF NOT EXISTS idx_shared_match_links_contest
    ON shared_match_links(contest_id, match_id);

CREATE TABLE IF NOT EXISTS shared_tournament_events (
    id INTEGER PRIMARY KEY,
    shared_tournament_id INTEGER NOT NULL
        REFERENCES shared_tournaments(id) ON DELETE CASCADE,
    shared_match_id INTEGER REFERENCES shared_matches(id) ON DELETE SET NULL,
    actor_telegram_user_id INTEGER NOT NULL CHECK (actor_telegram_user_id > 0),
    event_type TEXT NOT NULL,
    before_state TEXT,
    after_state TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_shared_tournament_events_tournament
    ON shared_tournament_events(shared_tournament_id, created_at, id);

CREATE TABLE IF NOT EXISTS contest_teams (
    contest_id INTEGER NOT NULL
        REFERENCES contests(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL
        REFERENCES teams(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (contest_id, team_id),
    UNIQUE (contest_id, position)
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
    best_of INTEGER CHECK (best_of IS NULL OR best_of IN (3, 5)),
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

CREATE TABLE IF NOT EXISTS champion_prediction_candidates (
  contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
  team_id INTEGER NOT NULL REFERENCES teams(id),
  position INTEGER NOT NULL CHECK (position >= 0),
  PRIMARY KEY (contest_id, team_id),
  UNIQUE (contest_id, position)
);

CREATE TABLE IF NOT EXISTS champion_predictions (
  id INTEGER PRIMARY KEY,
  contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  predicted_team_id INTEGER NOT NULL REFERENCES teams(id),
  submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (contest_id, user_id)
);

CREATE TABLE IF NOT EXISTS swiss_stage_prediction_settings (
    contest_id INTEGER PRIMARY KEY REFERENCES contests(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    deadline_at TEXT,
    direct_qualifier_count INTEGER NOT NULL DEFAULT 3
        CHECK (direct_qualifier_count > 0),
    elimination_qualifier_count INTEGER NOT NULL DEFAULT 5
        CHECK (elimination_qualifier_count > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS swiss_stage_prediction_candidates (
    contest_id INTEGER NOT NULL
        REFERENCES swiss_stage_prediction_settings(contest_id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (contest_id, team_id),
    UNIQUE (contest_id, position)
);

CREATE TABLE IF NOT EXISTS swiss_stage_predictions (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL
        REFERENCES swiss_stage_prediction_settings(contest_id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (contest_id, user_id),
    UNIQUE (id, contest_id)
);

CREATE TABLE IF NOT EXISTS swiss_stage_prediction_selections (
    prediction_id INTEGER NOT NULL,
    contest_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('direct', 'elimination')),
    PRIMARY KEY (prediction_id, team_id),
    FOREIGN KEY (prediction_id, contest_id)
        REFERENCES swiss_stage_predictions(id, contest_id) ON DELETE CASCADE,
    FOREIGN KEY (contest_id, team_id)
        REFERENCES contest_teams(contest_id, team_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS swiss_stage_results (
    contest_id INTEGER PRIMARY KEY
        REFERENCES swiss_stage_prediction_settings(contest_id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS swiss_stage_result_selections (
    contest_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('direct', 'elimination')),
    PRIMARY KEY (contest_id, team_id),
    FOREIGN KEY (contest_id)
        REFERENCES swiss_stage_results(contest_id) ON DELETE CASCADE,
    FOREIGN KEY (contest_id, team_id)
        REFERENCES contest_teams(contest_id, team_id)
        ON DELETE CASCADE
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

CREATE TABLE IF NOT EXISTS match_prediction_publications (
    match_id INTEGER PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS match_prediction_publication_messages (
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    part_number INTEGER NOT NULL CHECK (part_number >= 0),
    telegram_message_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (match_id, part_number)
);

CREATE TABLE IF NOT EXISTS leaderboard_publication_snapshots (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    actor_user_id INTEGER NOT NULL REFERENCES users(id),
    idempotency_key TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (contest_id, actor_user_id, idempotency_key)
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

CREATE TABLE IF NOT EXISTS contest_publications (
    id INTEGER PRIMARY KEY,
    contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    publication_type TEXT NOT NULL CHECK (
        publication_type IN (
            'match_result',
            'champion_predictions',
            'champion_result',
            'swiss_predictions',
            'swiss_result',
            'leaderboard_snapshot',
            'contest_completed'
        )
    ),
    entity_id INTEGER NOT NULL,
    desired_revision INTEGER NOT NULL DEFAULT 1 CHECK (
        desired_revision >= 1
    ),
    settled_revision INTEGER NOT NULL DEFAULT 0 CHECK (
        settled_revision >= 0
        AND settled_revision <= desired_revision
    ),
    desired_action TEXT NOT NULL DEFAULT 'publish' CHECK (
        desired_action IN ('publish', 'withdraw')
    ),
    delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        delivery_status IN (
            'pending',
            'published',
            'withdrawn',
            'terminal_failed'
        )
    ),
    first_event_id INTEGER NOT NULL,
    latest_event_id INTEGER NOT NULL,
    reconcile_at TEXT,
    claim_token TEXT,
    claim_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (contest_id, publication_type, entity_id),
    CHECK (
        (claim_token IS NULL AND claim_expires_at IS NULL)
        OR
        (claim_token IS NOT NULL AND claim_expires_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS contest_publication_messages (
    publication_id INTEGER NOT NULL
        REFERENCES contest_publications(id) ON DELETE CASCADE,
    part_number INTEGER NOT NULL CHECK (part_number >= 0),
    telegram_message_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    content_text TEXT,
    part_status TEXT NOT NULL DEFAULT 'active' CHECK (
        part_status IN ('active', 'retired', 'terminal_failed')
    ),
    last_error TEXT,
    sent_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (publication_id, part_number)
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

CREATE INDEX IF NOT EXISTS idx_champion_predictions_contest_id
  ON champion_predictions(contest_id);

CREATE INDEX IF NOT EXISTS idx_champion_predictions_user_id
  ON champion_predictions(user_id);

CREATE INDEX IF NOT EXISTS idx_champion_prediction_candidates_team_id
  ON champion_prediction_candidates(team_id);

CREATE INDEX IF NOT EXISTS idx_contest_teams_team_id
    ON contest_teams(team_id);

CREATE INDEX IF NOT EXISTS idx_swiss_stage_predictions_user_id
    ON swiss_stage_predictions(user_id);

CREATE INDEX IF NOT EXISTS idx_swiss_stage_prediction_selections_contest
    ON swiss_stage_prediction_selections(contest_id);

CREATE INDEX IF NOT EXISTS idx_event_log_contest_id
    ON event_log(contest_id);

CREATE INDEX IF NOT EXISTS idx_contest_publications_contest_order
    ON contest_publications(contest_id, first_event_id, id);

CREATE INDEX IF NOT EXISTS idx_contest_publications_due_retry
    ON contest_publications(next_attempt_at)
    WHERE next_attempt_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contest_publications_due_reconcile
    ON contest_publications(reconcile_at)
    WHERE reconcile_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contest_publications_active_claim
    ON contest_publications(claim_expires_at)
    WHERE claim_token IS NOT NULL;
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

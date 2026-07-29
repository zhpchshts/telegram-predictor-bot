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


def _migrate_contests_for_champion_predictions(
    connection: sqlite3.Connection,
) -> None:
    contest_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(contests)")
    }

    if "champion_prediction_enabled" not in contest_columns:
        connection.execute(
            """
            ALTER TABLE contests
            ADD COLUMN champion_prediction_enabled INTEGER NOT NULL DEFAULT 0
            CHECK (champion_prediction_enabled IN (0, 1))
            """
        )

    if "champion_prediction_deadline_at" not in contest_columns:
        connection.execute(
            """
            ALTER TABLE contests
            ADD COLUMN champion_prediction_deadline_at TEXT
            """
        )

    if "champion_prediction_points" not in contest_columns:
        connection.execute(
            """
            ALTER TABLE contests
            ADD COLUMN champion_prediction_points INTEGER NOT NULL DEFAULT 5
            CHECK (champion_prediction_points >= 0)
            """
        )

    if "champion_team_id" not in contest_columns:
        connection.execute(
            """
            ALTER TABLE contests
            ADD COLUMN champion_team_id INTEGER
            REFERENCES teams(id) ON DELETE SET NULL
            """
        )

    if "match_prediction_publication_enabled" not in contest_columns:
        connection.execute(
            """
            ALTER TABLE contests
            ADD COLUMN match_prediction_publication_enabled INTEGER NOT NULL DEFAULT 0
            CHECK (match_prediction_publication_enabled IN (0, 1))
            """
        )

    if "match_prediction_publication_enabled_at" not in contest_columns:
        connection.execute(
            """
            ALTER TABLE contests
            ADD COLUMN match_prediction_publication_enabled_at TEXT
            """
        )


def _migrate_contest_publication_messages(connection: sqlite3.Connection) -> None:
    message_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(contest_publication_messages)")
    }
    if "last_error" not in message_columns:
        connection.execute(
            """
            ALTER TABLE contest_publication_messages
            ADD COLUMN last_error TEXT
            """
        )
    if "content_text" not in message_columns:
        connection.execute(
            """
            ALTER TABLE contest_publication_messages
            ADD COLUMN content_text TEXT
            """
        )


def _migrate_contest_publications_for_champion_predictions(
    database_path: Path,
) -> None:
    connection = create_connection(database_path)
    try:
        table_row = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND name = 'contest_publications'
            """
        ).fetchone()
        if table_row is None or table_row["sql"] is None:
            return
        if "champion_predictions" in str(table_row["sql"]):
            return
        if connection.in_transaction:
            raise RuntimeError(
                "Publication schema migration must start outside a transaction."
            )

        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE contest_publications_new (
                    id INTEGER PRIMARY KEY,
                    contest_id INTEGER NOT NULL
                        REFERENCES contests(id) ON DELETE CASCADE,
                    publication_type TEXT NOT NULL CHECK (
                        publication_type IN (
                            'match_result',
                            'champion_predictions',
                            'champion_result',
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
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
                        attempt_count >= 0
                    ),
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
                )
                """
            )
            connection.execute(
                """
                INSERT INTO contest_publications_new (
                    id,
                    contest_id,
                    publication_type,
                    entity_id,
                    desired_revision,
                    settled_revision,
                    desired_action,
                    delivery_status,
                    first_event_id,
                    latest_event_id,
                    reconcile_at,
                    claim_token,
                    claim_expires_at,
                    attempt_count,
                    next_attempt_at,
                    last_error,
                    created_at,
                    updated_at
                )
                SELECT
                    id,
                    contest_id,
                    publication_type,
                    entity_id,
                    desired_revision,
                    settled_revision,
                    desired_action,
                    delivery_status,
                    first_event_id,
                    latest_event_id,
                    reconcile_at,
                    claim_token,
                    claim_expires_at,
                    attempt_count,
                    next_attempt_at,
                    last_error,
                    created_at,
                    updated_at
                FROM contest_publications
                """
            )
            connection.execute("DROP TABLE contest_publications")
            connection.execute(
                "ALTER TABLE contest_publications_new RENAME TO contest_publications"
            )
            connection.execute(
                """
                CREATE INDEX idx_contest_publications_contest_order
                ON contest_publications(contest_id, first_event_id, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX idx_contest_publications_due_retry
                ON contest_publications(next_attempt_at)
                WHERE next_attempt_at IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX idx_contest_publications_due_reconcile
                ON contest_publications(reconcile_at)
                WHERE reconcile_at IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE INDEX idx_contest_publications_active_claim
                ON contest_publications(claim_expires_at)
                WHERE claim_token IS NOT NULL
                """
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                "Foreign key violations found after publication schema migration."
            )
    finally:
        connection.close()


def _backfill_contest_teams(connection: sqlite3.Connection) -> None:
    contest_rows = connection.execute(
        """
        SELECT id
        FROM contests
        ORDER BY id
        """
    ).fetchall()
    for contest_row in contest_rows:
        contest_id = int(contest_row["id"])
        existing_rows = connection.execute(
            """
            SELECT team_id, position
            FROM contest_teams
            WHERE contest_id = ?
            ORDER BY position, team_id
            """,
            (contest_id,),
        ).fetchall()
        existing_team_ids = {int(row["team_id"]) for row in existing_rows}
        next_position = (
            max(int(row["position"]) for row in existing_rows) + 1
            if existing_rows
            else 0
        )

        source_rows = connection.execute(
            """
            WITH match_teams AS (
                SELECT
                    matches.home_team_id AS team_id,
                    MIN(matches.created_at) AS first_used_at
                FROM matches
                JOIN stages ON stages.id = matches.stage_id
                JOIN competitions
                    ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?
                GROUP BY matches.home_team_id

                UNION ALL

                SELECT
                    matches.away_team_id AS team_id,
                    MIN(matches.created_at) AS first_used_at
                FROM matches
                JOIN stages ON stages.id = matches.stage_id
                JOIN competitions
                    ON competitions.id = stages.competition_id
                WHERE competitions.contest_id = ?
                GROUP BY matches.away_team_id
            ),
            ordered_match_teams AS (
                SELECT team_id, MIN(first_used_at) AS first_used_at
                FROM match_teams
                GROUP BY team_id
            ),
            champion_prediction_teams AS (
                SELECT
                    predicted_team_id AS team_id,
                    MIN(submitted_at) AS first_used_at
                FROM champion_predictions
                WHERE contest_id = ?
                GROUP BY predicted_team_id
            )
            SELECT team_id, source_order, item_order
            FROM (
                SELECT
                    team_id,
                    1 AS source_order,
                    printf('%s:%020d', first_used_at, team_id) AS item_order
                FROM ordered_match_teams

                UNION ALL

                SELECT
                    team_id,
                    2 AS source_order,
                    printf('%s:%020d', first_used_at, team_id) AS item_order
                FROM champion_prediction_teams

                UNION ALL

                SELECT
                    champion_team_id AS team_id,
                    3 AS source_order,
                    printf('%020d', champion_team_id) AS item_order
                FROM contests
                WHERE id = ? AND champion_team_id IS NOT NULL
            )
            ORDER BY source_order, item_order
            """,
            (contest_id, contest_id, contest_id, contest_id),
        ).fetchall()
        for source_row in source_rows:
            team_id = int(source_row["team_id"])
            if team_id in existing_team_ids:
                continue
            connection.execute(
                """
                INSERT INTO contest_teams (contest_id, team_id, position)
                VALUES (?, ?, ?)
                """,
                (contest_id, team_id, next_position),
            )
            existing_team_ids.add(team_id)
            next_position += 1


def _migrate_swiss_stage_selection_foreign_keys(database_path: Path) -> None:
    connection = create_connection(database_path)
    try:
        prediction_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(swiss_stage_prediction_selections)"
        ).fetchall()
        result_foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(swiss_stage_result_selections)"
        ).fetchall()
        if any(
            str(row["table"]) == "contest_teams" for row in prediction_foreign_keys
        ) and any(str(row["table"]) == "contest_teams" for row in result_foreign_keys):
            return
        if connection.in_transaction:
            raise RuntimeError(
                "Swiss-stage schema migration must start outside a transaction."
            )

        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;

                CREATE TABLE swiss_stage_prediction_selections_new (
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
                        REFERENCES contest_teams(contest_id, team_id)
                        ON DELETE CASCADE
                );

                INSERT INTO swiss_stage_prediction_selections_new (
                    prediction_id,
                    contest_id,
                    team_id,
                    category
                )
                SELECT prediction_id, contest_id, team_id, category
                FROM swiss_stage_prediction_selections;

                CREATE TABLE swiss_stage_result_selections_new (
                    contest_id INTEGER NOT NULL,
                    team_id INTEGER NOT NULL,
                    category TEXT NOT NULL
                        CHECK (category IN ('direct', 'elimination')),
                    PRIMARY KEY (contest_id, team_id),
                    FOREIGN KEY (contest_id)
                        REFERENCES swiss_stage_results(contest_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (contest_id, team_id)
                        REFERENCES contest_teams(contest_id, team_id)
                        ON DELETE CASCADE
                );

                INSERT INTO swiss_stage_result_selections_new (
                    contest_id,
                    team_id,
                    category
                )
                SELECT contest_id, team_id, category
                FROM swiss_stage_result_selections;

                DROP TABLE swiss_stage_prediction_selections;
                DROP TABLE swiss_stage_result_selections;
                ALTER TABLE swiss_stage_prediction_selections_new
                    RENAME TO swiss_stage_prediction_selections;
                ALTER TABLE swiss_stage_result_selections_new
                    RENAME TO swiss_stage_result_selections;

                CREATE INDEX idx_swiss_stage_prediction_selections_contest
                    ON swiss_stage_prediction_selections(contest_id);

                COMMIT;
                """
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                "Foreign key violations found after Swiss-stage schema migration."
            )
    finally:
        connection.close()


def initialize_database(database_path: Path) -> None:
    _migrate_contest_publications_for_champion_predictions(database_path)
    with create_connection(database_path) as connection:
        connection.executescript(SCHEMA)
        _migrate_contests_for_champion_predictions(connection)
        _migrate_contest_publication_messages(connection)
        _backfill_contest_teams(connection)
    _migrate_swiss_stage_selection_foreign_keys(database_path)


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

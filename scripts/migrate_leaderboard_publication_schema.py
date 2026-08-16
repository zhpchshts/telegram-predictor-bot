from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3


EXPECTED_PUBLICATION_COLUMNS = (
    "id",
    "contest_id",
    "publication_type",
    "entity_id",
    "desired_revision",
    "settled_revision",
    "desired_action",
    "delivery_status",
    "first_event_id",
    "latest_event_id",
    "reconcile_at",
    "claim_token",
    "claim_expires_at",
    "attempt_count",
    "next_attempt_at",
    "last_error",
    "created_at",
    "updated_at",
)


def migrate_database(database_path: Path) -> None:
    if not database_path.is_file():
        raise RuntimeError(f"Database file does not exist: {database_path}")

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _require_healthy_database(connection, phase="before migration")
        publication_sql = _table_sql(connection, "contest_publications")
        if "'leaderboard_snapshot'" not in publication_sql:
            if (
                _column_names(connection, "contest_publications")
                != EXPECTED_PUBLICATION_COLUMNS
            ):
                raise RuntimeError(
                    "Unexpected contest_publications schema; refusing to rebuild it."
                )
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                _rebuild_contest_publications(connection)
                _create_snapshot_table(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
        else:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _create_snapshot_table(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        if "'leaderboard_snapshot'" not in _table_sql(
            connection, "contest_publications"
        ):
            raise RuntimeError("Leaderboard publication type was not added.")
        _require_healthy_database(connection, phase="after migration")
    finally:
        connection.close()


def _rebuild_contest_publications(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE contest_publications_new (
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
                    'pending', 'published', 'withdrawn', 'terminal_failed'
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
        )
        """
    )
    columns = ", ".join(EXPECTED_PUBLICATION_COLUMNS)
    connection.execute(
        f"""
        INSERT INTO contest_publications_new ({columns})
        SELECT {columns} FROM contest_publications
        """
    )
    connection.execute("DROP TABLE contest_publications")
    connection.execute(
        "ALTER TABLE contest_publications_new RENAME TO contest_publications"
    )
    for statement in (
        """
        CREATE INDEX IF NOT EXISTS idx_contest_publications_contest_order
        ON contest_publications(contest_id, first_event_id, id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_contest_publications_due_retry
        ON contest_publications(next_attempt_at)
        WHERE next_attempt_at IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_contest_publications_due_reconcile
        ON contest_publications(reconcile_at)
        WHERE reconcile_at IS NOT NULL
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_contest_publications_active_claim
        ON contest_publications(claim_expires_at)
        WHERE claim_token IS NOT NULL
        """,
    ):
        connection.execute(statement)


def _create_snapshot_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS leaderboard_publication_snapshots (
            id INTEGER PRIMARY KEY,
            contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
            actor_user_id INTEGER NOT NULL REFERENCES users(id),
            idempotency_key TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (contest_id, actor_user_id, idempotency_key)
        )
        """
    )


def _column_names(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    )


def _table_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"Required table is missing: {table_name}")
    return str(row[0])


def _require_healthy_database(
    connection: sqlite3.Connection,
    *,
    phase: str,
) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise RuntimeError(f"Database integrity check failed {phase}.")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"Foreign key check failed {phase}: {foreign_key_errors!r}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add manual intermediate leaderboard publications to Klever."
    )
    parser.add_argument("database_path", type=Path)
    arguments = parser.parse_args()
    migrate_database(arguments.database_path)
    print(f"Migration completed: {arguments.database_path}")


if __name__ == "__main__":
    main()

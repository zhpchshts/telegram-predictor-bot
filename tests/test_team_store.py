from __future__ import annotations

from pathlib import Path

from app.database import create_connection, initialize_database
from app.team_store import resolve_team_ids


def test_team_resolution_preserves_oldest_casefold_match(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        oldest_id = int(
            connection.execute("INSERT INTO teams (name) VALUES ('Alpha')").lastrowid
        )
        connection.execute("INSERT INTO teams (name) VALUES ('alpha')")

        resolved_ids = resolve_team_ids(
            connection,
            team_names=("ALPHA", "Beta", "beta"),
        )

        beta_row = connection.execute(
            "SELECT id FROM teams WHERE name = 'Beta'"
        ).fetchone()

    assert beta_row is not None
    beta_id = int(beta_row["id"])
    assert resolved_ids == (oldest_id, beta_id, beta_id)


def test_team_resolution_uses_one_preload_select_for_full_roster(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    team_names = tuple(f"Team {index}" for index in range(36))

    with create_connection(database_path) as connection:
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        resolved_ids = resolve_team_ids(connection, team_names=team_names)
        connection.set_trace_callback(None)

    select_count = sum(
        statement.lstrip().upper().startswith("SELECT") for statement in statements
    )
    insert_count = sum(
        statement.lstrip().upper().startswith("INSERT") for statement in statements
    )
    assert len(set(resolved_ids)) == 36
    assert select_count == 1
    assert insert_count == 36

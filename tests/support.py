from __future__ import annotations

from pathlib import Path

from app.database import create_connection


def ensure_contest_teams(
    database_path: Path,
    *,
    contest_id: int,
    names: tuple[str, ...],
) -> tuple[int, ...]:
    team_ids: list[int] = []
    with create_connection(database_path) as connection:
        next_position = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(position) + 1, 0)
                FROM contest_teams
                WHERE contest_id = ?
                """,
                (contest_id,),
            ).fetchone()[0]
        )
        for name in names:
            row = connection.execute(
                "SELECT id FROM teams WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                team_id = int(
                    connection.execute(
                        "INSERT INTO teams (name) VALUES (?)",
                        (name,),
                    ).lastrowid
                )
            else:
                team_id = int(row["id"])

            existing = connection.execute(
                """
                SELECT 1
                FROM contest_teams
                WHERE contest_id = ? AND team_id = ?
                """,
                (contest_id, team_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO contest_teams (contest_id, team_id, position)
                    VALUES (?, ?, ?)
                    """,
                    (contest_id, team_id, next_position),
                )
                next_position += 1
            team_ids.append(team_id)

    return tuple(team_ids)

"""Persistence helpers for the global team identity table."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence


def resolve_team_ids(
    connection: sqlite3.Connection,
    *,
    team_names: Sequence[str],
) -> tuple[int, ...]:
    """Resolve names case-insensitively, creating missing global teams.

    Historical databases may contain names that differ only by case because
    SQLite's UNIQUE constraint uses its default case-sensitive collation. The
    former per-name resolver selected the oldest such row, so the preload map
    intentionally preserves the first ID in database order.
    """

    rows = connection.execute(
        """
        SELECT id, name
        FROM teams
        ORDER BY id ASC
        """
    ).fetchall()
    team_id_by_normalized_name: dict[str, int] = {}
    for row in rows:
        team_id_by_normalized_name.setdefault(
            str(row["name"]).casefold(), int(row["id"])
        )

    team_ids: list[int] = []
    for team_name in team_names:
        normalized_name = team_name.casefold()
        team_id = team_id_by_normalized_name.get(normalized_name)
        if team_id is None:
            team_id = int(
                connection.execute(
                    "INSERT INTO teams (name) VALUES (?)",
                    (team_name,),
                ).lastrowid
            )
            team_id_by_normalized_name[normalized_name] = team_id
        team_ids.append(team_id)
    return tuple(team_ids)

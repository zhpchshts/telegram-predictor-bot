from __future__ import annotations

from pathlib import Path

from app.contest_service import get_active_contests
from app.database import create_connection, initialize_database


def test_get_active_contests_returns_empty_tuple_without_contests(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    contests = get_active_contests(
        database_path=database_path,
        telegram_chat_id=-1001234567890,
    )

    assert contests == ()

    with create_connection(database_path) as connection:
        chats_count = connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0]

    assert chats_count == 0


def test_get_active_contests_returns_all_active_contests_and_excludes_archived(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        chat_id = int(
            connection.execute(
                """
                INSERT INTO chats (telegram_chat_id, title)
                VALUES (?, ?)
                """,
                (-1001234567890, "Футбольные прогнозы"),
            ).lastrowid
        )

        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, "Чемпионат мира 2026", "world-cup-2026", 1),
        )
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, "Лига чемпионов 2026/27", "champions-league-2026-27", 1),
        )
        connection.execute(
            """
            INSERT INTO contests (chat_id, name, slug, is_active)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, "Архивный конкурс", "archived-contest", 0),
        )

    contests = get_active_contests(
        database_path=database_path,
        telegram_chat_id=-1001234567890,
    )

    assert [(contest.name, contest.slug) for contest in contests] == [
        ("Лига чемпионов 2026/27", "champions-league-2026-27"),
        ("Чемпионат мира 2026", "world-cup-2026"),
    ]
    assert all(contest.created_at for contest in contests)

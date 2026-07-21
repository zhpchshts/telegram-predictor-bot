from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.database import create_connection, initialize_database
from app.user_service import get_or_create_telegram_user, upsert_telegram_user


def test_upsert_telegram_user_uses_stable_telegram_id(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    created = upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=456,
        username="old_name",
        first_name=None,
        last_name=None,
    )
    updated = upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=456,
        username="new_name",
        first_name="Имя",
        last_name="Фамилия",
    )

    assert updated.id == created.id
    assert updated.username == "new_name"
    assert updated.first_name == "Имя"
    with create_connection(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_upsert_telegram_user_does_not_identify_by_username(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    first = upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=1,
        username="reused_name",
        first_name="Первый",
        last_name=None,
    )
    second = upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=2,
        username="reused_name",
        first_name="Второй",
        last_name=None,
    )

    assert first.id != second.id


def test_concurrent_upsert_does_not_duplicate_telegram_user(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    barrier = Barrier(2)

    def upsert() -> int:
        barrier.wait()
        return upsert_telegram_user(
            database_path=database_path,
            telegram_user_id=456,
            username="same_user",
            first_name="Имя",
            last_name=None,
        ).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        user_ids = list(executor.map(lambda _value: upsert(), range(2)))

    assert user_ids[0] == user_ids[1]
    with create_connection(database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE telegram_user_id = 456"
        ).fetchone()[0]
    assert count == 1


def test_get_or_create_telegram_user_stores_no_invented_profile_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    user = get_or_create_telegram_user(
        database_path=database_path,
        telegram_user_id=789012345,
    )

    assert user.telegram_user_id == 789012345
    assert user.username is None
    assert user.first_name == ""
    assert user.last_name is None


def test_get_or_create_telegram_user_preserves_existing_profile(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    existing = upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=456,
        username="known_user",
        first_name="Имя",
        last_name="Фамилия",
    )

    selected = get_or_create_telegram_user(
        database_path=database_path,
        telegram_user_id=456,
    )

    assert selected == existing


def test_profile_upsert_does_not_replace_known_name_with_missing_snapshot(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    existing = upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=456,
        username="known_user",
        first_name="Имя",
        last_name="Фамилия",
    )

    updated = upsert_telegram_user(
        database_path=database_path,
        telegram_user_id=456,
        username="renamed_user",
        first_name=None,
        last_name=None,
    )

    assert updated.id == existing.id
    assert updated.first_name == "Имя"
    assert updated.username == "renamed_user"

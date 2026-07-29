from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import contest_service, match_lifecycle
from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import (
    ContestNotFoundError,
    create_match,
    create_world_cup_2026_contest,
    get_contest_details,
    save_match_prediction,
    save_match_result,
)
from app.database import create_connection, initialize_database
from app.match_lifecycle import start_due_matches
from tests.support import ensure_contest_teams


TELEGRAM_CHAT_ID = -1001234567890
TELEGRAM_USER_ID = 123
MATCH_START = datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc)
AUDIT_ACTOR = AuditActor(
    telegram_chat_id=TELEGRAM_CHAT_ID,
    telegram_user_id=TELEGRAM_USER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


def _create_contest(database_path: Path, *, suffix: str = "1"):
    return create_world_cup_2026_contest(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        chat_title="Football",
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name=None,
        username=None,
        contest_name=f"Contest {suffix}",
        idempotency_key=f"contest-{suffix}",
        audit_actor=AUDIT_ACTOR,
    ).contest


def _create_match(database_path: Path, *, contest_id: int, suffix: str = "1"):
    home_team_id, away_team_id = ensure_contest_teams(
        database_path,
        contest_id=contest_id,
        names=(f"Home {suffix}", f"Away {suffix}"),
    )
    return create_match(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest_id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name=None,
        username=None,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        starts_at_utc="2026-06-11T18:00:00Z",
        idempotency_key=f"match-{suffix}",
        audit_actor=AUDIT_ACTOR,
    ).match


def _status(database_path: Path, match_id: int) -> str:
    with create_connection(database_path) as connection:
        row = connection.execute(
            "SELECT status FROM matches WHERE id = ?",
            (match_id,),
        ).fetchone()
    assert row is not None
    return str(row["status"])


@pytest.mark.parametrize(
    ("now_utc", "expected_status", "expected_count"),
    [
        (
            datetime(2026, 6, 11, 17, 59, 59, tzinfo=timezone.utc),
            "scheduled",
            0,
        ),
        (MATCH_START, "started", 1),
        (
            datetime(2026, 6, 11, 18, 0, 1, tzinfo=timezone.utc),
            "started",
            1,
        ),
        (
            datetime(2026, 6, 11, 18, 0, 0, 500000, tzinfo=timezone.utc),
            "started",
            1,
        ),
    ],
)
def test_start_due_matches_respects_start_time(
    tmp_path: Path,
    now_utc: datetime,
    expected_status: str,
    expected_count: int,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest_id=contest.id)

    changed_count = start_due_matches(
        database_path=database_path,
        now_utc=now_utc,
    )

    assert changed_count == expected_count
    assert _status(database_path, match.id) == expected_status


def test_start_due_matches_is_idempotent_and_does_not_write_events(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest_id=contest.id)

    with create_connection(database_path) as connection:
        events_before = connection.execute("SELECT COUNT(*) FROM event_log").fetchone()[
            0
        ]

    assert start_due_matches(database_path=database_path, now_utc=MATCH_START) == 1
    assert start_due_matches(database_path=database_path, now_utc=MATCH_START) == 0

    with create_connection(database_path) as connection:
        events_after = connection.execute("SELECT COUNT(*) FROM event_log").fetchone()[
            0
        ]

    assert _status(database_path, match.id) == "started"
    assert events_after == events_before


def test_concurrent_reconciliation_starts_match_once(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest_id=contest.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        changed_counts = list(
            executor.map(
                lambda _index: start_due_matches(
                    database_path=database_path,
                    now_utc=MATCH_START,
                ),
                range(2),
            )
        )

    assert sum(changed_counts) == 1
    assert _status(database_path, match.id) == "started"


@pytest.mark.parametrize("status", ["started", "finished", "cancelled"])
def test_start_due_matches_does_not_change_non_scheduled_statuses(
    tmp_path: Path,
    status: str,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest_id=contest.id)
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE matches SET status = ? WHERE id = ?",
            (status, match.id),
        )

    assert start_due_matches(database_path=database_path, now_utc=MATCH_START) == 0
    assert _status(database_path, match.id) == status


def test_start_due_matches_ignores_completed_contests(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest_id=contest.id)
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET is_active = 0 WHERE id = ?",
            (contest.id,),
        )

    assert start_due_matches(database_path=database_path, now_utc=MATCH_START) == 0
    assert _status(database_path, match.id) == "scheduled"


def test_start_due_matches_can_be_scoped_to_one_contest(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    first_contest = _create_contest(database_path, suffix="1")
    second_contest = _create_contest(database_path, suffix="2")
    first_match = _create_match(
        database_path,
        contest_id=first_contest.id,
        suffix="1",
    )
    second_match = _create_match(
        database_path,
        contest_id=second_contest.id,
        suffix="2",
    )

    assert (
        start_due_matches(
            database_path=database_path,
            contest_id=first_contest.id,
            now_utc=MATCH_START,
        )
        == 1
    )
    assert _status(database_path, first_match.id) == "started"
    assert _status(database_path, second_match.id) == "scheduled"

    assert start_due_matches(database_path=database_path, now_utc=MATCH_START) == 1
    assert _status(database_path, second_match.id) == "started"


def test_start_due_matches_rejects_naive_now(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with pytest.raises(ValueError, match="timezone"):
        start_due_matches(
            database_path=database_path,
            now_utc=datetime(2026, 6, 11, 18, 0),
        )


def test_get_contest_details_starts_only_requested_contest(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    first_contest = _create_contest(database_path, suffix="1")
    second_contest = _create_contest(database_path, suffix="2")
    first_match = _create_match(
        database_path,
        contest_id=first_contest.id,
        suffix="1",
    )
    second_match = _create_match(
        database_path,
        contest_id=second_contest.id,
        suffix="2",
    )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=first_contest.id,
        now_utc=MATCH_START,
    )

    assert details.matches[0].status == "started"
    assert _status(database_path, first_match.id) == "started"
    assert _status(database_path, second_match.id) == "scheduled"


def test_get_contest_details_uses_one_supplied_time_for_all_calculations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest_id=contest.id)
    save_match_prediction(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        match_id=match.id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name=None,
        username=None,
        predicted_home_score=2,
        predicted_away_score=1,
        predicted_advancing_team_id=match.home_team_id,
        now_utc=datetime(2026, 6, 11, 17, 59, tzinfo=timezone.utc),
    )
    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contests
            SET champion_prediction_enabled = 1,
                champion_prediction_deadline_at = '2026-06-12T18:00:00Z'
            WHERE id = ?
            """,
            (contest.id,),
        )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=TELEGRAM_USER_ID,
        now_utc=MATCH_START,
    )

    assert details.matches[0].status == "started"
    assert details.champion_prediction.is_open is True
    assert len(details.leaderboard) == 1
    assert details.leaderboard[0].prediction_history[0].id == match.id


def test_get_contest_details_validates_chat_before_reconciliation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest_id=contest.id)

    with pytest.raises(ContestNotFoundError):
        get_contest_details(
            database_path=database_path,
            telegram_chat_id=-999,
            contest_id=contest.id,
            now_utc=MATCH_START,
        )

    assert _status(database_path, match.id) == "scheduled"


def test_get_contest_details_rereads_contest_after_reconciliation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)

    def complete_contest_during_reconciliation(**_kwargs) -> int:
        with create_connection(database_path) as connection:
            connection.execute(
                "UPDATE contests SET is_active = 0 WHERE id = ?",
                (contest.id,),
            )
        return 0

    monkeypatch.setattr(
        contest_service,
        "start_due_matches",
        complete_contest_during_reconciliation,
    )

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        now_utc=MATCH_START,
    )

    assert details.is_active is False


@pytest.mark.parametrize("initial_status", ["scheduled", "started"])
def test_save_match_result_finishes_due_scheduled_and_started_matches(
    tmp_path: Path,
    initial_status: str,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    contest = _create_contest(database_path)
    match = _create_match(database_path, contest_id=contest.id)
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE matches SET status = ? WHERE id = ?",
            (initial_status, match.id),
        )

    save_match_result(
        database_path=database_path,
        telegram_chat_id=TELEGRAM_CHAT_ID,
        contest_id=contest.id,
        match_id=match.id,
        telegram_user_id=TELEGRAM_USER_ID,
        first_name="Eugene",
        last_name=None,
        username=None,
        home_score=2,
        away_score=1,
        advancing_team_id=match.home_team_id,
        now_utc=MATCH_START,
        audit_actor=AUDIT_ACTOR,
    )

    assert _status(database_path, match.id) == "finished"


def test_worker_runs_immediately_before_first_sleep(monkeypatch) -> None:
    calls: list[str] = []

    def fake_start_due_matches(**_kwargs) -> int:
        calls.append("start")
        return 0

    async def fake_sleep(_seconds: float) -> None:
        calls.append("sleep")
        raise asyncio.CancelledError

    monkeypatch.setattr(match_lifecycle, "start_due_matches", fake_start_due_matches)
    monkeypatch.setattr(match_lifecycle.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            match_lifecycle.run_match_lifecycle_worker(database_path=Path("unused.db"))
        )

    assert calls == ["start", "sleep"]


def test_worker_continues_after_iteration_error(monkeypatch) -> None:
    start_attempts = 0
    sleep_attempts = 0

    def fake_start_due_matches(**_kwargs) -> int:
        nonlocal start_attempts
        start_attempts += 1
        if start_attempts == 1:
            raise RuntimeError("temporary failure")
        return 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal sleep_attempts
        sleep_attempts += 1
        if sleep_attempts == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(match_lifecycle, "start_due_matches", fake_start_due_matches)
    monkeypatch.setattr(match_lifecycle.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            match_lifecycle.run_match_lifecycle_worker(database_path=Path("unused.db"))
        )

    assert start_attempts == 2
    assert sleep_attempts == 2

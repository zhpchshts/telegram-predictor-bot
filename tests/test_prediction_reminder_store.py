from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.database import create_connection, database_connection, initialize_database
from app.prediction_reminder_store import (
    MAX_REMINDER_LEAD_TIME_MINUTES,
    MIN_REMINDER_LEAD_TIME_MINUTES,
    PredictionReminderStoreError,
    RenderedReminderPart,
    claim_next_prediction_reminder_delivery,
    finish_prediction_reminder_retry,
    finish_prediction_reminder_success,
    finish_prediction_reminder_terminal,
    finish_prediction_reminder_unknown,
    get_reminder_preference,
    get_reminder_settings,
    load_prediction_reminder_parts,
    mark_prediction_reminder_part_sending,
    prepare_prediction_reminder_render_request,
    queue_manual_prediction_reminder,
    reconcile_prediction_reminder_occurrences,
    record_prediction_reminder_part_sent,
    save_reminder_preference,
    save_reminder_settings,
    serialize_reminder_time,
    store_prediction_reminder_parts,
)
from tests.test_contest_service import (
    TELEGRAM_USER_ID,
    create_contest,
    create_test_match,
    delete_test_match,
    update_test_match_start,
)


NOW = datetime(2035, 1, 1, 9, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=3)


def _setup_contest(tmp_path: Path) -> tuple[Path, int, int, int]:
    database_path = tmp_path / "reminders.db"
    initialize_database(database_path)
    created = create_contest(database_path=database_path)
    with closing(create_connection(database_path)) as connection:
        row = connection.execute(
            """
            SELECT contests.id AS contest_id, chats.id AS chat_id,
                   users.id AS actor_user_id
            FROM contests
            JOIN chats ON chats.id = contests.chat_id
            JOIN users ON users.telegram_user_id = ?
            WHERE contests.id = ?
            """,
            (TELEGRAM_USER_ID, created.contest.id),
        ).fetchone()
    assert row is not None
    return (
        database_path,
        int(row["contest_id"]),
        int(row["chat_id"]),
        int(row["actor_user_id"]),
    )


def _create_match(
    database_path: Path,
    contest_id: int,
    *,
    starts_at: datetime = START,
    key: str = "match",
    home: str = "Аргентина",
    away: str = "Бразилия",
) -> int:
    created = create_test_match(
        database_path=database_path,
        contest_id=contest_id,
        starts_at_utc=serialize_reminder_time(starts_at),
        idempotency_key=key,
        home_team_name=home,
        away_team_name=away,
    )
    return created.match.id


def _enable(
    database_path: Path,
    contest_id: int,
    *,
    lead_time_minutes: int = 180,
) -> None:
    save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=True,
        lead_time_minutes=lead_time_minutes,
        now_utc=NOW,
    )


def _set_long_term_deadlines(
    database_path: Path,
    contest_id: int,
    *,
    swiss_deadline: datetime | None,
    champion_deadline: datetime | None,
) -> None:
    with database_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contests
            SET champion_prediction_enabled = ?,
                champion_prediction_deadline_at = ?
            WHERE id = ?
            """,
            (
                int(champion_deadline is not None),
                serialize_reminder_time(champion_deadline)
                if champion_deadline is not None
                else None,
                contest_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO swiss_stage_prediction_settings (
                contest_id, enabled, deadline_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(contest_id) DO UPDATE SET
                enabled = excluded.enabled,
                deadline_at = excluded.deadline_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                contest_id,
                int(swiss_deadline is not None),
                serialize_reminder_time(swiss_deadline)
                if swiss_deadline is not None
                else None,
            ),
        )


def _fetch_all(
    database_path: Path, sql: str, parameters: tuple[object, ...] = ()
) -> list[dict[str, object]]:
    with closing(create_connection(database_path)) as connection:
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def _send_success(database_path: Path, delivery, *, now: datetime = NOW) -> None:
    request = prepare_prediction_reminder_render_request(
        database_path=database_path, delivery=delivery, now_utc=now
    )
    assert request is not None
    parts = store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(RenderedReminderPart(html="<b>Напоминание</b>"),),
        now_utc=now,
    )
    assert len(parts) == 1
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=now,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=101,
        now_utc=now,
    )
    assert finish_prediction_reminder_success(
        database_path=database_path, delivery=delivery, now_utc=now
    )


def _store_one_part(database_path: Path, delivery, *, now: datetime = NOW) -> None:
    assert (
        prepare_prediction_reminder_render_request(
            database_path=database_path, delivery=delivery, now_utc=now
        )
        is not None
    )
    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(RenderedReminderPart(html="reminder"),),
        now_utc=now,
    )


def test_settings_and_preferences_default_off_and_revisioned(tmp_path: Path) -> None:
    database_path, contest_id, chat_id, actor_user_id = _setup_contest(tmp_path)

    settings = get_reminder_settings(database_path=database_path, contest_id=contest_id)
    assert (settings.enabled, settings.lead_time_minutes, settings.revision) == (
        False,
        180,
        0,
    )
    saved = save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=True,
        lead_time_minutes=180,
        actor_user_id=actor_user_id,
        now_utc=NOW,
    )
    assert saved.revision == 1
    assert (
        save_reminder_settings(
            database_path=database_path,
            contest_id=contest_id,
            enabled=True,
            lead_time_minutes=180,
            actor_user_id=actor_user_id,
            now_utc=NOW,
        ).revision
        == 1
    )
    assert (
        save_reminder_settings(
            database_path=database_path,
            contest_id=contest_id,
            enabled=False,
            lead_time_minutes=180,
            actor_user_id=actor_user_id,
            now_utc=NOW,
        ).revision
        == 2
    )
    for invalid in (
        MIN_REMINDER_LEAD_TIME_MINUTES - 1,
        MAX_REMINDER_LEAD_TIME_MINUTES + 1,
    ):
        with pytest.raises(ValueError):
            save_reminder_settings(
                database_path=database_path,
                contest_id=contest_id,
                enabled=True,
                lead_time_minutes=invalid,
            )

    preference = get_reminder_preference(
        database_path=database_path, chat_id=chat_id, user_id=actor_user_id
    )
    assert (preference.mention_in_prediction_reminders, preference.revision) == (
        False,
        0,
    )
    assert (
        save_reminder_preference(
            database_path=database_path,
            chat_id=chat_id,
            user_id=actor_user_id,
            mention_in_prediction_reminders=True,
            now_utc=NOW,
        ).revision
        == 1
    )
    assert (
        save_reminder_preference(
            database_path=database_path,
            chat_id=chat_id,
            user_id=actor_user_id,
            mention_in_prediction_reminders=True,
            now_utc=NOW,
        ).revision
        == 1
    )


def test_settings_write_rejects_completed_contest_inside_store_transaction(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET is_active = 0 WHERE id = ?", (contest_id,)
        )
    with pytest.raises(PredictionReminderStoreError, match="not active"):
        save_reminder_settings(
            database_path=database_path,
            contest_id=contest_id,
            enabled=True,
            lead_time_minutes=180,
            now_utc=NOW,
        )
    assert not get_reminder_settings(
        database_path=database_path, contest_id=contest_id
    ).enabled


def test_reconcile_match_occurrence_select_count_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_database_connection = database_connection

    def exercise(match_count: int, *, case_name: str) -> int:
        database_path, contest_id, _, _ = _setup_contest(tmp_path / case_name)
        match_ids = [
            _create_match(
                database_path,
                contest_id,
                key=f"bounded-selects-{index}",
                home=f"Home {index}",
                away=f"Away {index}",
            )
            for index in range(match_count)
        ]
        _enable(database_path, contest_id)
        assert (
            reconcile_prediction_reminder_occurrences(
                database_path=database_path,
                now_utc=NOW,
            )
            == match_count
        )

        rescheduled_start = START + timedelta(hours=1)
        update_test_match_start(
            database_path=database_path,
            contest_id=contest_id,
            match_id=match_ids[0],
            starts_at_utc=serialize_reminder_time(rescheduled_start),
            now_utc=NOW,
        )

        statements: list[str] = []

        @contextmanager
        def traced_database_connection(path: Path):
            with original_database_connection(path) as connection:
                connection.set_trace_callback(statements.append)
                try:
                    yield connection
                finally:
                    connection.set_trace_callback(None)

        with monkeypatch.context() as scoped_monkeypatch:
            scoped_monkeypatch.setattr(
                "app.prediction_reminder_store.database_connection",
                traced_database_connection,
            )
            assert (
                reconcile_prediction_reminder_occurrences(
                    database_path=database_path,
                    now_utc=NOW,
                )
                == 1
            )

        occurrences = _fetch_all(
            database_path,
            """
            SELECT original_match_id, match_id, observed_starts_at_utc,
                   schedule_revision, status
            FROM prediction_reminder_occurrences
            ORDER BY original_match_id, schedule_revision
            """,
        )
        rescheduled = [
            row for row in occurrences if row["original_match_id"] == match_ids[0]
        ]
        assert rescheduled == [
            {
                "original_match_id": match_ids[0],
                "match_id": None,
                "observed_starts_at_utc": serialize_reminder_time(START),
                "schedule_revision": 1,
                "status": "cancelled",
            },
            {
                "original_match_id": match_ids[0],
                "match_id": match_ids[0],
                "observed_starts_at_utc": serialize_reminder_time(rescheduled_start),
                "schedule_revision": 2,
                "status": "scheduled",
            },
        ]
        unchanged = [
            row for row in occurrences if row["original_match_id"] != match_ids[0]
        ]
        assert len(unchanged) == match_count - 1
        assert all(
            row["match_id"] == row["original_match_id"]
            and row["observed_starts_at_utc"] == serialize_reminder_time(START)
            and row["schedule_revision"] == 1
            and row["status"] == "scheduled"
            for row in unchanged
        )
        return sum(
            statement.lstrip().upper().startswith("SELECT") for statement in statements
        )

    single_match_selects = exercise(1, case_name="single-match")
    many_match_selects = exercise(24, case_name="many-matches")

    assert many_match_selects <= single_match_selects + 1, (
        single_match_selects,
        many_match_selects,
    )


def test_exact_start_batch_is_claimed_once_and_late_match_is_supplemental(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="same-1")
    _create_match(
        database_path,
        contest_id,
        key="same-2",
        home="Испания",
        away="Франция",
    )
    _enable(database_path, contest_id)
    assert (
        reconcile_prediction_reminder_occurrences(
            database_path=database_path, now_utc=NOW
        )
        == 2
    )

    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return claim_next_prediction_reminder_delivery(
            database_path=database_path, now_utc=NOW
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _: claim(), range(2)))
    deliveries = [delivery for delivery in claimed if delivery is not None]
    assert len(deliveries) == 1
    first = deliveries[0]
    assert first.supplemental_sequence == 1
    assert (
        len(
            _fetch_all(
                database_path,
                "SELECT * FROM prediction_reminder_delivery_items WHERE delivery_id = ?",
                (first.id,),
            )
        )
        == 2
    )
    _send_success(database_path, first)

    _create_match(
        database_path,
        contest_id,
        key="same-late",
        home="Италия",
        away="Германия",
    )
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    supplemental = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert supplemental is not None
    assert supplemental.supplemental_sequence == 2
    assert (
        len(
            _fetch_all(
                database_path,
                "SELECT * FROM prediction_reminder_delivery_items WHERE delivery_id = ?",
                (supplemental.id,),
            )
        )
        == 1
    )


def test_match_swiss_and_champion_with_same_deadline_share_one_auto_batch(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id)
    _set_long_term_deadlines(
        database_path,
        contest_id,
        swiss_deadline=START,
        champion_deadline=START,
    )
    _enable(database_path, contest_id)

    assert (
        reconcile_prediction_reminder_occurrences(
            database_path=database_path,
            now_utc=NOW,
        )
        == 3
    )
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path,
        now_utc=NOW,
    )
    assert delivery is not None
    request = prepare_prediction_reminder_render_request(
        database_path=database_path,
        delivery=delivery,
        now_utc=NOW,
    )
    assert request is not None
    assert len(request.items) == 1
    assert {item.kind for item in request.deadlines} == {"swiss", "champion"}
    assert {item.deadline_at_utc for item in request.deadlines} == {
        serialize_reminder_time(START)
    }
    _send_success(database_path, delivery)
    assert (
        claim_next_prediction_reminder_delivery(
            database_path=database_path,
            now_utc=NOW,
        )
        is None
    )


def test_auto_deadline_batch_does_not_broaden_to_other_open_deadlines(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _set_long_term_deadlines(
        database_path,
        contest_id,
        swiss_deadline=START,
        champion_deadline=START + timedelta(days=1),
    )
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)

    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path,
        now_utc=NOW,
    )
    assert delivery is not None
    request = prepare_prediction_reminder_render_request(
        database_path=database_path,
        delivery=delivery,
        now_utc=NOW,
    )
    assert request is not None
    assert request.items == ()
    assert [item.kind for item in request.deadlines] == ["swiss"]


def test_champion_deadline_change_rearms_a_finalized_occurrence(tmp_path: Path) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _set_long_term_deadlines(
        database_path,
        contest_id,
        swiss_deadline=None,
        champion_deadline=START,
    )
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    first_delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert first_delivery is not None
    _send_success(database_path, first_delivery)

    moved_deadline = START + timedelta(hours=1)
    _set_long_term_deadlines(
        database_path,
        contest_id,
        swiss_deadline=None,
        champion_deadline=moved_deadline,
    )
    assert (
        reconcile_prediction_reminder_occurrences(
            database_path=database_path,
            now_utc=NOW,
        )
        == 1
    )
    rows = _fetch_all(
        database_path,
        """
        SELECT contest_id, observed_deadline_at_utc, schedule_revision, status
        FROM prediction_reminder_deadline_occurrences
        WHERE original_contest_id = ? AND deadline_kind = 'champion'
        ORDER BY schedule_revision
        """,
        (contest_id,),
    )
    assert rows == [
        {
            "contest_id": None,
            "observed_deadline_at_utc": serialize_reminder_time(START),
            "schedule_revision": 1,
            "status": "cancelled",
        },
        {
            "contest_id": contest_id,
            "observed_deadline_at_utc": serialize_reminder_time(moved_deadline),
            "schedule_revision": 2,
            "status": "scheduled",
        },
    ]
    assert (
        claim_next_prediction_reminder_delivery(
            database_path=database_path, now_utc=NOW
        )
        is None
    )
    second_delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path,
        now_utc=NOW + timedelta(hours=1),
    )
    assert second_delivery is not None and second_delivery.id != first_delivery.id


def test_timestamp_canonicalization_and_submillisecond_starts_do_not_batch(
    tmp_path: Path,
) -> None:
    assert serialize_reminder_time(NOW) == "2035-01-01T09:00:00.000000Z"
    assert (
        serialize_reminder_time(NOW.replace(microsecond=123456))
        == "2035-01-01T09:00:00.123456Z"
    )

    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    first_id = _create_match(database_path, contest_id, key="micro-1")
    second_id = _create_match(
        database_path,
        contest_id,
        key="micro-2",
        home="Португалия",
        away="Нидерланды",
    )
    first_start = "2035-01-01T12:00:00.000001Z"
    second_start = "2035-01-01T12:00:00.000002Z"
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE matches SET starts_at_utc = ? WHERE id = ?",
            (first_start, first_id),
        )
        connection.execute(
            "UPDATE matches SET starts_at_utc = ? WHERE id = ?",
            (second_start, second_id),
        )
    _enable(database_path, contest_id)
    due = datetime(2035, 1, 1, 9, 0, 0, 2, tzinfo=timezone.utc)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=due)
    first = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=due
    )
    second = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=due
    )
    assert first is not None and second is not None
    assert {first.batch_starts_at_utc, second.batch_starts_at_utc} == {
        first_start,
        second_start,
    }
    assert all(
        len(
            _fetch_all(
                database_path,
                "SELECT * FROM prediction_reminder_delivery_items WHERE delivery_id = ?",
                (delivery.id,),
            )
        )
        == 1
        for delivery in (first, second)
    )


def test_fractional_deadline_is_not_claimed_one_microsecond_early(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    match_id = _create_match(database_path, contest_id, key="fractional-deadline")
    start = datetime(2035, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE matches SET starts_at_utc = ? WHERE id = ?",
            (serialize_reminder_time(start), match_id),
        )
    _enable(database_path, contest_id)
    due = start - timedelta(minutes=180)
    reconcile_prediction_reminder_occurrences(
        database_path=database_path, now_utc=due - timedelta(microseconds=1)
    )
    assert (
        claim_next_prediction_reminder_delivery(
            database_path=database_path,
            now_utc=due - timedelta(microseconds=1),
        )
        is None
    )
    assert (
        claim_next_prediction_reminder_delivery(
            database_path=database_path, now_utc=due
        )
        is not None
    )


@pytest.mark.parametrize("outcome", ["sent", "unknown"])
def test_reschedule_rearms_finalized_occurrence_and_a_b_a_is_distinct(
    tmp_path: Path, outcome: str
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    match_id = _create_match(database_path, contest_id, key=f"reschedule-{outcome}")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    if outcome == "sent":
        _send_success(database_path, delivery)
    else:
        _store_one_part(database_path, delivery)
        assert mark_prediction_reminder_part_sending(
            database_path=database_path,
            delivery=delivery,
            part_number=0,
            now_utc=NOW,
        )
        assert finish_prediction_reminder_unknown(
            database_path=database_path,
            delivery=delivery,
            part_number=0,
            error="connection outcome is unknown",
            now_utc=NOW,
        )

    save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=True,
        lead_time_minutes=120,
        now_utc=NOW,
    )
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    assert (
        len(_fetch_all(database_path, "SELECT id FROM prediction_reminder_occurrences"))
        == 1
    )

    start_b = START + timedelta(hours=1)
    update_test_match_start(
        database_path=database_path,
        contest_id=contest_id,
        match_id=match_id,
        starts_at_utc=serialize_reminder_time(start_b),
        now_utc=NOW,
    )
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    update_test_match_start(
        database_path=database_path,
        contest_id=contest_id,
        match_id=match_id,
        starts_at_utc=serialize_reminder_time(START),
        now_utc=NOW,
    )
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    rows = _fetch_all(
        database_path,
        """
        SELECT match_id, observed_starts_at_utc, schedule_revision, status
        FROM prediction_reminder_occurrences ORDER BY schedule_revision
        """,
    )
    assert [row["schedule_revision"] for row in rows] == [1, 2, 3]
    assert [row["status"] for row in rows] == ["cancelled", "cancelled", "scheduled"]
    assert rows[0]["match_id"] is None and rows[1]["match_id"] is None
    assert rows[2]["match_id"] == match_id


def test_deleted_and_reused_match_id_gets_a_fresh_live_occurrence(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    old_match_id = _create_match(database_path, contest_id, key="delete-old")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delete_test_match(
        database_path=database_path,
        contest_id=contest_id,
        match_id=old_match_id,
    )
    new_match_id = _create_match(
        database_path,
        contest_id,
        key="delete-new",
        home="Бельгия",
        away="Хорватия",
    )
    assert new_match_id == old_match_id
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    rows = _fetch_all(
        database_path,
        """
        SELECT match_id, original_match_id, schedule_revision, status
        FROM prediction_reminder_occurrences ORDER BY id
        """,
    )
    assert rows == [
        {
            "match_id": None,
            "original_match_id": old_match_id,
            "schedule_revision": 1,
            "status": "deleted",
        },
        {
            "match_id": new_match_id,
            "original_match_id": new_match_id,
            "schedule_revision": 1,
            "status": "scheduled",
        },
    ]


@pytest.mark.parametrize("deadline_kind", ["match", "swiss", "champion"])
def test_claim_refuses_stale_due_after_lead_time_change(
    tmp_path: Path,
    deadline_kind: str,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    if deadline_kind == "match":
        _create_match(database_path, contest_id, key="stale-lead-match")
    else:
        _set_long_term_deadlines(
            database_path,
            contest_id,
            swiss_deadline=START if deadline_kind == "swiss" else None,
            champion_deadline=START if deadline_kind == "champion" else None,
        )
    _enable(database_path, contest_id, lead_time_minutes=180)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)

    current = save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=True,
        lead_time_minutes=60,
        now_utc=NOW,
    )
    assert current.revision == 2
    assert (
        claim_next_prediction_reminder_delivery(
            database_path=database_path,
            now_utc=NOW,
        )
        is None
    )

    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    table = (
        "prediction_reminder_occurrences"
        if deadline_kind == "match"
        else "prediction_reminder_deadline_occurrences"
    )
    live_where = "status = 'scheduled'"
    if deadline_kind != "match":
        live_where += f" AND deadline_kind = '{deadline_kind}'"
    occurrence = _fetch_all(
        database_path,
        f"SELECT due_at FROM {table} WHERE {live_where}",
    )[0]
    assert occurrence["due_at"] == serialize_reminder_time(START - timedelta(hours=1))
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path,
        now_utc=START - timedelta(hours=1),
    )
    assert delivery is not None and delivery.settings_revision == 2


def test_settings_revision_change_does_not_strand_a_batched_retry(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="settings-revision")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    first = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert first is not None
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=first,
        part_number=None,
        error="preflight retry",
        retry_after_seconds=60,
        now_utc=NOW,
    )
    save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=False,
        lead_time_minutes=180,
        now_utc=NOW,
    )
    current = save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=True,
        lead_time_minutes=180,
        now_utc=NOW,
    )
    assert current.revision == 3
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    replacement = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert replacement is not None
    assert replacement.id != first.id
    assert replacement.settings_revision == 3
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (first.id,),
        )[0]["status"]
        == "cancelled"
    )


def test_reconcile_does_not_release_occurrences_from_a_final_delivery(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="final-reset-guard")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    with database_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'sent', claim_token = NULL, claim_expires_at = NULL,
                finished_at = updated_at
            WHERE id = ?
            """,
            (delivery.id,),
        )
    save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=False,
        lead_time_minutes=180,
        now_utc=NOW,
    )
    assert (
        reconcile_prediction_reminder_occurrences(
            database_path=database_path, now_utc=NOW
        )
        == 0
    )
    occurrence = _fetch_all(
        database_path,
        "SELECT status, delivery_id FROM prediction_reminder_occurrences",
    )[0]
    assert occurrence["status"] == "batched"
    assert occurrence["delivery_id"] == delivery.id


def test_settings_change_after_render_blocks_transition_to_telegram_send(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="pre-send-race")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    _store_one_part(database_path, delivery)
    save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=False,
        lead_time_minutes=180,
        now_utc=NOW,
    )
    assert not mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )


def test_reschedule_after_render_blocks_transition_to_telegram_send(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    match_id = _create_match(database_path, contest_id, key="reschedule-send-race")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    _store_one_part(database_path, delivery)
    update_test_match_start(
        database_path=database_path,
        contest_id=contest_id,
        match_id=match_id,
        starts_at_utc=serialize_reminder_time(START + timedelta(hours=1)),
        now_utc=NOW,
    )
    assert not mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (delivery.id,),
        )[0]["status"]
        == "cancelled"
    )


def test_auto_recipients_are_opted_in_participants_missing_an_actionable_forecast(
    tmp_path: Path,
) -> None:
    database_path, contest_id, chat_id, _ = _setup_contest(tmp_path)
    participant_match_id = _create_match(
        database_path,
        contest_id,
        starts_at=START + timedelta(days=1),
        key="participant-match",
    )
    reminder_match_id = _create_match(
        database_path,
        contest_id,
        key="recipient-match",
        home="Англия",
        away="Уругвай",
    )
    with database_connection(database_path) as connection:
        users: list[int] = []
        for telegram_id, name in ((501, "Missing"), (502, "Complete"), (503, "None")):
            cursor = connection.execute(
                "INSERT INTO users (telegram_user_id, first_name) VALUES (?, ?)",
                (telegram_id, name),
            )
            assert cursor.lastrowid is not None
            users.append(int(cursor.lastrowid))
        matches = {
            int(row["id"]): row
            for row in connection.execute(
                "SELECT id, tie_id, home_team_id FROM matches WHERE id IN (?, ?)",
                (participant_match_id, reminder_match_id),
            ).fetchall()
        }
        for user_id in users[:2]:
            connection.execute(
                """
                INSERT INTO match_predictions (
                    match_id, user_id, predicted_home_score, predicted_away_score
                ) VALUES (?, ?, 1, 0)
                """,
                (participant_match_id, user_id),
            )
            connection.execute(
                """
                INSERT INTO tie_predictions (
                    tie_id, user_id, predicted_advancing_team_id
                ) VALUES (?, ?, ?)
                """,
                (
                    int(matches[participant_match_id]["tie_id"]),
                    user_id,
                    int(matches[participant_match_id]["home_team_id"]),
                ),
            )
        connection.execute(
            """
            INSERT INTO match_predictions (
                match_id, user_id, predicted_home_score, predicted_away_score
            ) VALUES (?, ?, 2, 1)
            """,
            (reminder_match_id, users[1]),
        )
        connection.execute(
            """
            INSERT INTO tie_predictions (
                tie_id, user_id, predicted_advancing_team_id
            ) VALUES (?, ?, ?)
            """,
            (
                int(matches[reminder_match_id]["tie_id"]),
                users[1],
                int(matches[reminder_match_id]["home_team_id"]),
            ),
        )
    for user_id in users:
        save_reminder_preference(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
            mention_in_prediction_reminders=True,
            now_utc=NOW,
        )
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    request = prepare_prediction_reminder_render_request(
        database_path=database_path, delivery=delivery, now_utc=NOW
    )
    assert request is not None
    assert [recipient.user_id for recipient in request.recipients] == [users[0]]

    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(RenderedReminderPart(html="first snapshot"),),
        now_utc=NOW,
    )
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=delivery,
        part_number=None,
        error="safe retry before send",
        retry_after_seconds=0,
        now_utc=NOW,
    )
    save_reminder_preference(
        database_path=database_path,
        chat_id=chat_id,
        user_id=users[0],
        mention_in_prediction_reminders=False,
        now_utc=NOW,
    )
    retried = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert retried is not None and retried.id == delivery.id
    refreshed = prepare_prediction_reminder_render_request(
        database_path=database_path, delivery=retried, now_utc=NOW
    )
    assert refreshed is not None
    assert refreshed.recipients == ()


@pytest.mark.parametrize("deadline_kind", ["swiss", "champion"])
def test_auto_deadline_recipients_refresh_opt_in_and_missing_forecast(
    tmp_path: Path,
    deadline_kind: str,
) -> None:
    database_path, contest_id, chat_id, _ = _setup_contest(tmp_path)
    participant_match_id = _create_match(
        database_path,
        contest_id,
        starts_at=START + timedelta(days=1),
        key=f"{deadline_kind}-participant",
    )
    with database_connection(database_path) as connection:
        match = connection.execute(
            """
            SELECT home_team_id, away_team_id
            FROM matches WHERE id = ?
            """,
            (participant_match_id,),
        ).fetchone()
        assert match is not None
        identities = (
            ((601, "Missing"), (602, "Partial"), (603, "Complete"))
            if deadline_kind == "swiss"
            else ((601, "Missing"), (602, "Complete"))
        )
        users: list[int] = []
        for telegram_id, name in identities:
            cursor = connection.execute(
                "INSERT INTO users (telegram_user_id, first_name) VALUES (?, ?)",
                (telegram_id, name),
            )
            assert cursor.lastrowid is not None
            users.append(int(cursor.lastrowid))
        for user_id in users:
            connection.execute(
                """
                INSERT INTO match_predictions (
                    match_id, user_id, predicted_home_score, predicted_away_score
                ) VALUES (?, ?, 1, 0)
                """,
                (participant_match_id, user_id),
            )
        if deadline_kind == "champion":
            connection.execute(
                """
                UPDATE contests
                SET champion_prediction_enabled = 1,
                    champion_prediction_deadline_at = ?
                WHERE id = ?
                """,
                (serialize_reminder_time(START), contest_id),
            )
            connection.execute(
                """
                INSERT INTO champion_predictions (
                    contest_id, user_id, predicted_team_id
                ) VALUES (?, ?, ?)
                """,
                (contest_id, users[-1], int(match["home_team_id"])),
            )
        else:
            connection.execute(
                """
                INSERT INTO swiss_stage_prediction_settings (
                    contest_id, enabled, deadline_at,
                    direct_qualifier_count, elimination_qualifier_count
                ) VALUES (?, 1, ?, 1, 1)
                ON CONFLICT(contest_id) DO UPDATE SET
                    enabled = 1,
                    deadline_at = excluded.deadline_at,
                    direct_qualifier_count = 1,
                    elimination_qualifier_count = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (contest_id, serialize_reminder_time(START)),
            )
            prediction = connection.execute(
                """
                INSERT INTO swiss_stage_predictions (contest_id, user_id)
                VALUES (?, ?)
                """,
                (contest_id, users[-1]),
            )
            assert prediction.lastrowid is not None
            empty_prediction = connection.execute(
                """
                INSERT INTO swiss_stage_predictions (contest_id, user_id)
                VALUES (?, ?)
                """,
                (contest_id, users[0]),
            )
            partial_prediction = connection.execute(
                """
                INSERT INTO swiss_stage_predictions (contest_id, user_id)
                VALUES (?, ?)
                """,
                (contest_id, users[1]),
            )
            assert empty_prediction.lastrowid is not None
            assert partial_prediction.lastrowid is not None
            connection.execute(
                """
                INSERT INTO swiss_stage_prediction_selections (
                    prediction_id, contest_id, team_id, category
                ) VALUES (?, ?, ?, 'direct')
                """,
                (
                    int(partial_prediction.lastrowid),
                    contest_id,
                    int(match["home_team_id"]),
                ),
            )
            connection.executemany(
                """
                INSERT INTO swiss_stage_prediction_selections (
                    prediction_id, contest_id, team_id, category
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        int(prediction.lastrowid),
                        contest_id,
                        int(match["home_team_id"]),
                        "direct",
                    ),
                    (
                        int(prediction.lastrowid),
                        contest_id,
                        int(match["away_team_id"]),
                        "elimination",
                    ),
                ),
            )
    for user_id in users:
        save_reminder_preference(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
            mention_in_prediction_reminders=True,
            now_utc=NOW,
        )
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path,
        now_utc=NOW,
    )
    assert delivery is not None
    request = prepare_prediction_reminder_render_request(
        database_path=database_path,
        delivery=delivery,
        now_utc=NOW,
    )
    assert request is not None
    assert [item.kind for item in request.deadlines] == [deadline_kind]
    incomplete_users = users[:-1]
    assert [recipient.user_id for recipient in request.recipients] == incomplete_users

    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(RenderedReminderPart(html="deadline reminder"),),
        now_utc=NOW,
    )
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=delivery,
        part_number=None,
        error="safe retry before send",
        retry_after_seconds=0,
        now_utc=NOW,
    )
    for user_id in incomplete_users:
        save_reminder_preference(
            database_path=database_path,
            chat_id=chat_id,
            user_id=user_id,
            mention_in_prediction_reminders=False,
            now_utc=NOW,
        )
    retried = claim_next_prediction_reminder_delivery(
        database_path=database_path,
        now_utc=NOW,
    )
    assert retried is not None and retried.id == delivery.id
    refreshed = prepare_prediction_reminder_render_request(
        database_path=database_path,
        delivery=retried,
        now_utc=NOW,
    )
    assert refreshed is not None
    assert refreshed.recipients == ()


def test_multipart_safe_retry_resumes_only_pending_parts(tmp_path: Path) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="multipart")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    assert (
        prepare_prediction_reminder_render_request(
            database_path=database_path, delivery=delivery, now_utc=NOW
        )
        is not None
    )
    parts = store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="part one"),
            RenderedReminderPart(html="part two"),
        ),
        now_utc=NOW,
    )
    assert [part.status for part in parts] == ["pending", "pending"]

    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=700,
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        now_utc=NOW,
    )
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        error="Telegram 429",
        retry_after_seconds=0,
        now_utc=NOW,
    )

    retried = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert retried is not None and retried.id == delivery.id
    resumed = load_prediction_reminder_parts(
        database_path=database_path, delivery=retried, now_utc=NOW
    )
    assert [(part.part_number, part.status) for part in resumed] == [
        (0, "sent"),
        (1, "pending"),
    ]
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=retried,
        part_number=1,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=retried,
        part_number=1,
        telegram_message_id=701,
        now_utc=NOW,
    )
    assert finish_prediction_reminder_success(
        database_path=database_path, delivery=retried, now_utc=NOW
    )


@pytest.mark.parametrize("settings_change", ["disable", "revision"])
def test_multipart_with_sent_prefix_finishes_after_settings_change(
    tmp_path: Path,
    settings_change: str,
) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key=f"partial-{settings_change}")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    assert (
        prepare_prediction_reminder_render_request(
            database_path=database_path, delivery=delivery, now_utc=NOW
        )
        is not None
    )
    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="sent prefix"),
            RenderedReminderPart(html="pending tail"),
        ),
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=710,
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        now_utc=NOW,
    )
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        error="Telegram 429",
        retry_after_seconds=0,
        now_utc=NOW,
    )
    save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=settings_change != "disable",
        lead_time_minutes=120 if settings_change == "revision" else 180,
        actor_user_id=actor_user_id,
        now_utc=NOW,
    )
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)

    retried = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert retried is not None and retried.id == delivery.id
    resumed = load_prediction_reminder_parts(
        database_path=database_path, delivery=retried, now_utc=NOW
    )
    assert [(part.part_number, part.status) for part in resumed] == [
        (0, "sent"),
        (1, "pending"),
    ]
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=retried,
        part_number=1,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=retried,
        part_number=1,
        telegram_message_id=711,
        now_utc=NOW,
    )
    assert finish_prediction_reminder_success(
        database_path=database_path, delivery=retried, now_utc=NOW
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_occurrences",
        )[0]["status"]
        == "sent"
    )


def test_multipart_resume_removes_opted_out_mentions_from_pending_html(
    tmp_path: Path,
) -> None:
    database_path, contest_id, chat_id, actor_user_id = _setup_contest(tmp_path)
    match_id = _create_match(database_path, contest_id, key="multipart-opt-out")
    with database_connection(database_path) as connection:
        match = connection.execute(
            "SELECT home_team_id FROM matches WHERE id = ?", (match_id,)
        ).fetchone()
        assert match is not None
        connection.execute(
            """
            INSERT INTO champion_predictions (
                contest_id, user_id, predicted_team_id
            ) VALUES (?, ?, ?)
            """,
            (contest_id, actor_user_id, int(match["home_team_id"])),
        )
    save_reminder_preference(
        database_path=database_path,
        chat_id=chat_id,
        user_id=actor_user_id,
        mention_in_prediction_reminders=True,
        now_utc=NOW,
    )
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    request = prepare_prediction_reminder_render_request(
        database_path=database_path, delivery=delivery, now_utc=NOW
    )
    assert request is not None
    assert [item.telegram_user_id for item in request.recipients] == [TELEGRAM_USER_ID]
    initial = store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="<p>already sent</p>"),
            RenderedReminderPart(
                html=(
                    "<p>pending tail</p>"
                    "<p><b>Ждём прогнозы от:</b><br>"
                    f'<a href="tg://user?id={TELEGRAM_USER_ID}">Игрок</a></p>'
                )
            ),
        ),
        now_utc=NOW,
    )
    original_pending_hash = initial[1].content_hash
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=702,
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        now_utc=NOW,
    )
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        error="Telegram 429",
        retry_after_seconds=0,
        now_utc=NOW,
    )
    save_reminder_preference(
        database_path=database_path,
        chat_id=chat_id,
        user_id=actor_user_id,
        mention_in_prediction_reminders=False,
        now_utc=NOW,
    )

    retried = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert retried is not None and retried.id == delivery.id
    resumed = load_prediction_reminder_parts(
        database_path=database_path, delivery=retried, now_utc=NOW
    )
    pending = resumed[1]
    assert pending.status == "pending"
    assert f"tg://user?id={TELEGRAM_USER_ID}" not in pending.html
    assert "Ждём прогнозы от" not in pending.html
    assert pending.content_hash != original_pending_hash
    assert (
        _fetch_all(
            database_path,
            """
            SELECT status FROM prediction_reminder_delivery_recipients
            WHERE delivery_id = ? AND telegram_user_id = ?
            """,
            (delivery.id, TELEGRAM_USER_ID),
        )[0]["status"]
        == "suppressed"
    )


def test_manual_multipart_resume_removes_recipient_after_forecast_completion(
    tmp_path: Path,
) -> None:
    database_path, contest_id, chat_id, actor_user_id = _setup_contest(tmp_path)
    match_id = _create_match(database_path, contest_id, key="manual-completed")
    with database_connection(database_path) as connection:
        match = connection.execute(
            """
            SELECT home_team_id, tie_id FROM matches WHERE id = ?
            """,
            (match_id,),
        ).fetchone()
        assert match is not None and match["tie_id"] is not None
        connection.execute(
            """
            INSERT INTO champion_predictions (
                contest_id, user_id, predicted_team_id
            ) VALUES (?, ?, ?)
            """,
            (contest_id, actor_user_id, int(match["home_team_id"])),
        )
    save_reminder_preference(
        database_path=database_path,
        chat_id=chat_id,
        user_id=actor_user_id,
        mention_in_prediction_reminders=True,
        now_utc=NOW,
    )
    queued = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="manual-completed",
        now_utc=NOW,
    )
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None and delivery.id == queued.delivery_id
    request = prepare_prediction_reminder_render_request(
        database_path=database_path, delivery=delivery, now_utc=NOW
    )
    assert request is not None
    assert [item.telegram_user_id for item in request.recipients] == [TELEGRAM_USER_ID]
    initial = store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="<p>already sent</p>"),
            RenderedReminderPart(
                html=(
                    "<p>pending manual tail</p>"
                    "<p><b>Ждём прогнозы от:</b><br>"
                    f'<a href="tg://user?id={TELEGRAM_USER_ID}">Игрок</a></p>'
                )
            ),
        ),
        now_utc=NOW,
    )
    original_pending_hash = initial[1].content_hash
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=720,
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        now_utc=NOW,
    )
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        error="Telegram 429",
        retry_after_seconds=0,
        now_utc=NOW,
    )
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO match_predictions (
                match_id, user_id, predicted_home_score, predicted_away_score
            ) VALUES (?, ?, 1, 0)
            """,
            (match_id, actor_user_id),
        )
        connection.execute(
            """
            INSERT INTO tie_predictions (
                tie_id, user_id, predicted_advancing_team_id
            ) VALUES (?, ?, ?)
            """,
            (
                int(match["tie_id"]),
                actor_user_id,
                int(match["home_team_id"]),
            ),
        )

    retried = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert retried is not None and retried.id == delivery.id
    resumed = load_prediction_reminder_parts(
        database_path=database_path, delivery=retried, now_utc=NOW
    )
    pending = resumed[1]
    assert f"tg://user?id={TELEGRAM_USER_ID}" not in pending.html
    assert "Ждём прогнозы от" not in pending.html
    assert pending.content_hash != original_pending_hash
    assert (
        _fetch_all(
            database_path,
            """
            SELECT status FROM prediction_reminder_delivery_recipients
            WHERE delivery_id = ? AND telegram_user_id = ?
            """,
            (delivery.id, TELEGRAM_USER_ID),
        )[0]["status"]
        == "suppressed"
    )


def test_manual_far_before_lead_does_not_suppress_later_auto(tmp_path: Path) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="manual-far")
    _enable(database_path, contest_id, lead_time_minutes=60)
    queued = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="manual-far",
        now_utc=NOW,
    )
    assert not _fetch_all(
        database_path,
        "SELECT 1 FROM prediction_reminder_delivery_items WHERE delivery_id = ?",
        (queued.delivery_id,),
    )
    manual = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert manual is not None and manual.kind == "manual"
    _send_success(database_path, manual)

    due = START - timedelta(minutes=60)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=due)
    automatic = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=due
    )
    assert automatic is not None and automatic.kind == "automatic"


def test_manual_in_window_cancels_pending_auto_and_terminal_releases_coverage(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="manual-terminal")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    automatic = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert automatic is not None
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=automatic,
        part_number=None,
        error="pending auto",
        retry_after_seconds=60,
        now_utc=NOW,
    )

    queued = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="manual-terminal",
        now_utc=NOW,
    )
    replay = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="manual-terminal",
        now_utc=NOW,
    )
    assert replay.delivery_id == queued.delivery_id
    assert not replay.was_created
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (automatic.id,),
        )[0]["status"]
        == "cancelled"
    )
    assert (
        len(
            _fetch_all(
                database_path,
                "SELECT * FROM prediction_reminder_delivery_items WHERE delivery_id = ?",
                (queued.delivery_id,),
            )
        )
        == 1
    )

    manual = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert manual is not None and manual.id == queued.delivery_id
    assert finish_prediction_reminder_terminal(
        database_path=database_path,
        delivery=manual,
        error="definite pre-send failure",
        now_utc=NOW,
    )
    occurrence = _fetch_all(
        database_path,
        "SELECT status, delivery_id FROM prediction_reminder_occurrences",
    )[0]
    assert occurrence == {"status": "scheduled", "delivery_id": None}
    replacement = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert replacement is not None and replacement.kind == "automatic"


def test_manual_in_window_suppresses_deadline_auto_and_terminal_releases_it(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    _set_long_term_deadlines(
        database_path,
        contest_id,
        swiss_deadline=None,
        champion_deadline=START,
    )
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    automatic = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert automatic is not None and automatic.kind == "automatic"
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=automatic,
        part_number=None,
        error="pending deadline auto",
        retry_after_seconds=60,
        now_utc=NOW,
    )

    queued = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="manual-deadline-terminal",
        now_utc=NOW,
    )
    replay = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="manual-deadline-terminal",
        now_utc=NOW,
    )
    assert replay.delivery_id == queued.delivery_id
    assert not replay.was_created
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (automatic.id,),
        )[0]["status"]
        == "cancelled"
    )
    covered = _fetch_all(
        database_path,
        """
        SELECT status, delivery_id
        FROM prediction_reminder_deadline_occurrences
        WHERE contest_id = ? AND deadline_kind = 'champion'
        """,
        (contest_id,),
    )[0]
    assert covered == {"status": "batched", "delivery_id": queued.delivery_id}

    manual = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert manual is not None and manual.id == queued.delivery_id
    assert finish_prediction_reminder_terminal(
        database_path=database_path,
        delivery=manual,
        error="definite pre-send failure",
        now_utc=NOW,
    )
    released = _fetch_all(
        database_path,
        """
        SELECT status, delivery_id
        FROM prediction_reminder_deadline_occurrences
        WHERE contest_id = ? AND deadline_kind = 'champion'
        """,
        (contest_id,),
    )[0]
    assert released == {"status": "scheduled", "delivery_id": None}
    replacement = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert replacement is not None and replacement.kind == "automatic"


@pytest.mark.parametrize("outcome", ["sent", "unknown"])
def test_manual_sent_or_unknown_suppresses_auto(tmp_path: Path, outcome: str) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key=f"manual-{outcome}")
    _enable(database_path, contest_id)
    queued = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key=f"manual-{outcome}",
        now_utc=NOW,
    )
    manual = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert manual is not None and manual.id == queued.delivery_id
    if outcome == "sent":
        _send_success(database_path, manual)
    else:
        assert finish_prediction_reminder_unknown(
            database_path=database_path,
            delivery=manual,
            error="ambiguous preflight",
            now_utc=NOW,
        )
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    assert (
        claim_next_prediction_reminder_delivery(
            database_path=database_path, now_utc=NOW
        )
        is None
    )
    assert _fetch_all(
        database_path,
        "SELECT status FROM prediction_reminder_occurrences",
    )[0]["status"] in {"sent", "unknown"}


def test_manual_idempotent_replay_survives_contest_completion(tmp_path: Path) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    queued = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="stable-replay",
        now_utc=NOW,
    )
    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET is_active = 0 WHERE id = ?", (contest_id,)
        )
    replay = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="stable-replay",
        now_utc=NOW,
    )
    assert replay.delivery_id == queued.delivery_id
    assert not replay.was_created


def test_manual_idempotent_replay_survives_contest_delete(tmp_path: Path) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    queued = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="deleted-replay",
        now_utc=NOW,
    )
    with database_connection(database_path) as connection:
        connection.execute("DELETE FROM contests WHERE id = ?", (contest_id,))
    replay = queue_manual_prediction_reminder(
        database_path=database_path,
        contest_id=contest_id,
        actor_user_id=actor_user_id,
        idempotency_key="deleted-replay",
        now_utc=NOW,
    )
    assert replay == type(queued)(
        request_id=queued.request_id,
        delivery_id=queued.delivery_id,
        was_created=False,
    )
    delivery = _fetch_all(
        database_path,
        """
        SELECT contest_id, original_contest_id
        FROM prediction_reminder_deliveries WHERE id = ?
        """,
        (queued.delivery_id,),
    )[0]
    assert delivery == {"contest_id": None, "original_contest_id": contest_id}


def test_abandoned_preparing_is_retryable_but_sending_is_unknown(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="abandoned-preparing")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    preparing = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW, lease_seconds=1
    )
    assert preparing is not None
    later = NOW + timedelta(seconds=2)
    reconcile_prediction_reminder_occurrences(
        database_path=database_path, now_utc=later
    )
    retried = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=later, lease_seconds=1
    )
    assert retried is not None and retried.id == preparing.id
    assert retried.claim_token != preparing.claim_token

    # Let this second claim become ambiguous after the durable `sending` marker.
    _store_one_part(database_path, retried, now=later)
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=retried,
        part_number=0,
        now_utc=later,
    )
    reconcile_prediction_reminder_occurrences(
        database_path=database_path, now_utc=later + timedelta(seconds=2)
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (retried.id,),
        )[0]["status"]
        == "unknown"
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_occurrences",
        )[0]["status"]
        == "unknown"
    )


def test_abandoned_claim_with_recorded_prefix_resumes_pending_tail(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="crash-recorded-prefix")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW, lease_seconds=1
    )
    assert delivery is not None
    assert (
        prepare_prediction_reminder_render_request(
            database_path=database_path, delivery=delivery, now_utc=NOW
        )
        is not None
    )
    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="recorded prefix"),
            RenderedReminderPart(html="pending tail"),
        ),
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=800,
        now_utc=NOW,
    )

    later = NOW + timedelta(seconds=2)
    reconcile_prediction_reminder_occurrences(
        database_path=database_path, now_utc=later
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (delivery.id,),
        )[0]["status"]
        == "retry"
    )
    assert [
        row["status"]
        for row in _fetch_all(
            database_path,
            """
            SELECT status FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery.id,),
        )
    ] == ["sent", "pending"]
    retried = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=later
    )
    assert retried is not None and retried.id == delivery.id
    resumed = load_prediction_reminder_parts(
        database_path=database_path, delivery=retried, now_utc=later
    )
    assert [(part.part_number, part.status) for part in resumed] == [
        (0, "sent"),
        (1, "pending"),
    ]


def test_abandoned_claim_with_all_parts_recorded_finalizes_sent(tmp_path: Path) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="crash-all-recorded")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW, lease_seconds=1
    )
    assert delivery is not None
    _store_one_part(database_path, delivery)
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=801,
        now_utc=NOW,
    )

    reconcile_prediction_reminder_occurrences(
        database_path=database_path, now_utc=NOW + timedelta(seconds=2)
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (delivery.id,),
        )[0]["status"]
        == "sent"
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_occurrences",
        )[0]["status"]
        == "sent"
    )


@pytest.mark.parametrize("source", ["automatic", "manual"])
def test_abandoned_partial_after_deadline_keeps_occurrence_suppressed(
    tmp_path: Path,
    source: str,
) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key=f"expired-partial-{source}")
    _enable(database_path, contest_id)
    if source == "manual":
        queued = queue_manual_prediction_reminder(
            database_path=database_path,
            contest_id=contest_id,
            actor_user_id=actor_user_id,
            idempotency_key="expired-partial-manual",
            now_utc=NOW,
        )
        delivery = claim_next_prediction_reminder_delivery(
            database_path=database_path, now_utc=NOW, lease_seconds=1
        )
        assert delivery is not None and delivery.id == queued.delivery_id
        recovery_time = NOW + timedelta(minutes=16)
    else:
        reconcile_prediction_reminder_occurrences(
            database_path=database_path, now_utc=NOW
        )
        delivery = claim_next_prediction_reminder_delivery(
            database_path=database_path, now_utc=NOW, lease_seconds=1
        )
        assert delivery is not None and delivery.kind == "automatic"
        recovery_time = START + timedelta(seconds=1)
    assert (
        prepare_prediction_reminder_render_request(
            database_path=database_path, delivery=delivery, now_utc=NOW
        )
        is not None
    )
    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="recorded"),
            RenderedReminderPart(html="never sent"),
        ),
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=802,
        now_utc=NOW,
    )

    reconcile_prediction_reminder_occurrences(
        database_path=database_path, now_utc=recovery_time
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (delivery.id,),
        )[0]["status"]
        == "partial"
    )
    assert [
        row["status"]
        for row in _fetch_all(
            database_path,
            """
            SELECT status FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery.id,),
        )
    ] == ["sent", "skipped"]
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_occurrences",
        )[0]["status"]
        == "sent"
    )


@pytest.mark.parametrize("source", ["automatic", "manual"])
def test_unclaimed_retry_with_sent_prefix_expires_as_partial(
    tmp_path: Path,
    source: str,
) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key=f"retry-expiry-{source}")
    _enable(database_path, contest_id)
    if source == "manual":
        queued = queue_manual_prediction_reminder(
            database_path=database_path,
            contest_id=contest_id,
            actor_user_id=actor_user_id,
            idempotency_key="retry-expiry-manual",
            now_utc=NOW,
        )
        delivery = claim_next_prediction_reminder_delivery(
            database_path=database_path, now_utc=NOW
        )
        assert delivery is not None and delivery.id == queued.delivery_id
        expiry_time = NOW + timedelta(minutes=16)
    else:
        reconcile_prediction_reminder_occurrences(
            database_path=database_path, now_utc=NOW
        )
        delivery = claim_next_prediction_reminder_delivery(
            database_path=database_path, now_utc=NOW
        )
        assert delivery is not None and delivery.kind == "automatic"
        expiry_time = START + timedelta(seconds=1)
    assert (
        prepare_prediction_reminder_render_request(
            database_path=database_path, delivery=delivery, now_utc=NOW
        )
        is not None
    )
    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="recorded"),
            RenderedReminderPart(html="retry pending"),
        ),
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=803,
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        now_utc=NOW,
    )
    assert finish_prediction_reminder_retry(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        error="Telegram 429",
        retry_after_seconds=1,
        now_utc=NOW,
    )

    reconcile_prediction_reminder_occurrences(
        database_path=database_path, now_utc=expiry_time
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (delivery.id,),
        )[0]["status"]
        == "partial"
    )
    assert [
        row["status"]
        for row in _fetch_all(
            database_path,
            """
            SELECT status FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery.id,),
        )
    ] == ["sent", "skipped"]
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_occurrences",
        )[0]["status"]
        == "sent"
    )


def test_stale_mark_with_sent_prefix_settles_known_partial(tmp_path: Path) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="stale-mark-partial")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path,
        now_utc=NOW,
        lease_seconds=4 * 60 * 60,
    )
    assert delivery is not None
    assert (
        prepare_prediction_reminder_render_request(
            database_path=database_path, delivery=delivery, now_utc=NOW
        )
        is not None
    )
    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="recorded"),
            RenderedReminderPart(html="stale tail"),
        ),
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    assert record_prediction_reminder_part_sent(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        telegram_message_id=804,
        now_utc=NOW,
    )
    assert not mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        now_utc=START,
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (delivery.id,),
        )[0]["status"]
        == "partial"
    )
    assert [
        row["status"]
        for row in _fetch_all(
            database_path,
            """
            SELECT status FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery.id,),
        )
    ] == ["sent", "skipped"]
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_occurrences",
        )[0]["status"]
        == "sent"
    )


def test_stale_gate_marks_current_sending_part_unknown(tmp_path: Path) -> None:
    database_path, contest_id, _, actor_user_id = _setup_contest(tmp_path)
    _create_match(database_path, contest_id, key="stale-sending-unknown")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    assert (
        prepare_prediction_reminder_render_request(
            database_path=database_path, delivery=delivery, now_utc=NOW
        )
        is not None
    )
    store_prediction_reminder_parts(
        database_path=database_path,
        delivery=delivery,
        parts=(
            RenderedReminderPart(html="in flight"),
            RenderedReminderPart(html="not started"),
        ),
        now_utc=NOW,
    )
    assert mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=0,
        now_utc=NOW,
    )
    save_reminder_settings(
        database_path=database_path,
        contest_id=contest_id,
        enabled=False,
        lead_time_minutes=180,
        actor_user_id=actor_user_id,
        now_utc=NOW,
    )
    assert not mark_prediction_reminder_part_sending(
        database_path=database_path,
        delivery=delivery,
        part_number=1,
        now_utc=NOW,
    )
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_deliveries WHERE id = ?",
            (delivery.id,),
        )[0]["status"]
        == "unknown"
    )
    parts = _fetch_all(
        database_path,
        """
        SELECT status, last_error FROM prediction_reminder_delivery_parts
        WHERE delivery_id = ? ORDER BY part_number
        """,
        (delivery.id,),
    )
    assert parts[0]["status"] == "unknown"
    assert "state changed" in parts[0]["last_error"]
    assert parts[1]["status"] == "pending"
    assert (
        _fetch_all(
            database_path,
            "SELECT status FROM prediction_reminder_occurrences",
        )[0]["status"]
        == "unknown"
    )


def test_contest_delete_keeps_delivery_ledger_and_tombstone_snapshots(
    tmp_path: Path,
) -> None:
    database_path, contest_id, _, _ = _setup_contest(tmp_path)
    match_id = _create_match(database_path, contest_id, key="contest-delete")
    _enable(database_path, contest_id)
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)
    delivery = claim_next_prediction_reminder_delivery(
        database_path=database_path, now_utc=NOW
    )
    assert delivery is not None
    with database_connection(database_path) as connection:
        connection.execute("DELETE FROM contests WHERE id = ?", (contest_id,))
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=NOW)

    delivery_row = _fetch_all(
        database_path,
        "SELECT contest_id, original_contest_id, status FROM prediction_reminder_deliveries",
    )[0]
    assert delivery_row["contest_id"] is None
    assert delivery_row["original_contest_id"] == contest_id
    occurrence = _fetch_all(
        database_path,
        """
        SELECT contest_id, original_contest_id, match_id, original_match_id, status
        FROM prediction_reminder_occurrences
        """,
    )[0]
    assert occurrence == {
        "contest_id": None,
        "original_contest_id": contest_id,
        "match_id": None,
        "original_match_id": match_id,
        "status": "deleted",
    }
    item = _fetch_all(
        database_path,
        "SELECT match_id_snapshot, starts_at_snapshot FROM prediction_reminder_delivery_items",
    )[0]
    assert item == {
        "match_id_snapshot": match_id,
        "starts_at_snapshot": serialize_reminder_time(START),
    }
    with closing(create_connection(database_path)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Sequence
from uuid import uuid4

from app.database import database_connection


DEFAULT_REMINDER_LEAD_TIME_MINUTES = 180
MIN_REMINDER_LEAD_TIME_MINUTES = 5
MAX_REMINDER_LEAD_TIME_MINUTES = 10_080
REMINDER_CLAIM_SECONDS = 90
REMINDER_MAX_ATTEMPTS = 96
REMINDER_MAX_BACKOFF_SECONDS = 300
MANUAL_REMINDER_EXPIRY_MINUTES = 15

_MENTION_BLOCK_PATTERN = re.compile(
    r"<p><b>Ждём прогнозы от:</b><br>(?P<body>.*?)</p>",
    flags=re.DOTALL,
)
_MENTION_ANCHOR_PATTERN = re.compile(
    r'<a href="tg://user\?id=(?P<telegram_user_id>\d+)">.*?</a>',
    flags=re.DOTALL,
)

DeliveryStatus = Literal[
    "pending",
    "preparing",
    "sending",
    "retry",
    "sent",
    "partial",
    "cancelled",
    "expired",
    "terminal_failed",
    "unknown",
]
ReminderDeadlineKind = Literal["swiss", "champion"]


class PredictionReminderStoreError(RuntimeError):
    pass


class PredictionReminderClaimLostError(PredictionReminderStoreError):
    pass


@dataclass(frozen=True, slots=True)
class ReminderSettings:
    contest_id: int
    enabled: bool
    lead_time_minutes: int
    revision: int


@dataclass(frozen=True, slots=True)
class ReminderPreference:
    chat_id: int
    user_id: int
    mention_in_prediction_reminders: bool
    revision: int


@dataclass(frozen=True, slots=True)
class ClaimedReminderDelivery:
    id: int
    contest_id: int
    telegram_chat_id: int
    kind: Literal["automatic", "manual"]
    batch_starts_at_utc: str | None
    supplemental_sequence: int
    settings_revision: int
    claim_token: str


@dataclass(frozen=True, slots=True)
class ReminderDeliveryItem:
    match_id: int
    starts_at_utc: str
    home_team_name: str
    away_team_name: str


@dataclass(frozen=True, slots=True)
class ReminderDeadlineItem:
    kind: ReminderDeadlineKind
    deadline_at_utc: str


@dataclass(frozen=True, slots=True)
class ReminderRecipient:
    user_id: int
    telegram_user_id: int
    username: str | None
    first_name: str
    last_name: str | None


@dataclass(frozen=True, slots=True)
class ReminderRenderRequest:
    delivery_id: int
    contest_id: int
    contest_name: str
    telegram_chat_id: int
    kind: Literal["automatic", "manual"]
    batch_starts_at_utc: str | None
    supplemental_sequence: int
    items: tuple[ReminderDeliveryItem, ...]
    deadlines: tuple[ReminderDeadlineItem, ...]
    recipients: tuple[ReminderRecipient, ...]


@dataclass(frozen=True, slots=True)
class RenderedReminderPart:
    html: str
    has_launch_button: bool = True


@dataclass(frozen=True, slots=True)
class StoredReminderPart:
    part_number: int
    html: str
    content_hash: str
    has_launch_button: bool
    status: str
    telegram_message_id: int | None


@dataclass(frozen=True, slots=True)
class ManualReminderQueueResult:
    request_id: int
    delivery_id: int
    was_created: bool


def serialize_reminder_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Reminder timestamps must include a timezone.")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def resolve_reminder_time(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Reminder timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


def get_reminder_settings(*, database_path: Path, contest_id: int) -> ReminderSettings:
    with database_connection(database_path) as connection:
        contest = connection.execute(
            "SELECT 1 FROM contests WHERE id = ?", (contest_id,)
        ).fetchone()
        if contest is None:
            raise PredictionReminderStoreError("Contest was not found.")
        row = connection.execute(
            """
            SELECT enabled, lead_time_minutes, revision
            FROM contest_prediction_reminder_settings
            WHERE contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
    if row is None:
        return ReminderSettings(
            contest_id=contest_id,
            enabled=False,
            lead_time_minutes=DEFAULT_REMINDER_LEAD_TIME_MINUTES,
            revision=0,
        )
    return _settings_from_row(contest_id=contest_id, row=row)


def save_reminder_settings(
    *,
    database_path: Path,
    contest_id: int,
    enabled: bool,
    lead_time_minutes: int,
    actor_user_id: int | None = None,
    now_utc: datetime | None = None,
) -> ReminderSettings:
    _require_bool(enabled, field_name="enabled")
    _validate_lead_time(lead_time_minutes)
    now = resolve_reminder_time(now_utc)
    now_value = serialize_reminder_time(now)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        contest = connection.execute(
            "SELECT is_active FROM contests WHERE id = ?", (contest_id,)
        ).fetchone()
        if contest is None:
            raise PredictionReminderStoreError("Contest was not found.")
        if not bool(contest["is_active"]):
            raise PredictionReminderStoreError("Contest is not active.")
        row = connection.execute(
            """
            SELECT enabled, lead_time_minutes, revision
            FROM contest_prediction_reminder_settings
            WHERE contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO contest_prediction_reminder_settings (
                    contest_id, enabled, lead_time_minutes, revision,
                    updated_by_user_id, created_at, updated_at
                )
                VALUES (?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    contest_id,
                    int(enabled),
                    lead_time_minutes,
                    actor_user_id,
                    now_value,
                    now_value,
                ),
            )
        elif (
            bool(row["enabled"]) != enabled
            or int(row["lead_time_minutes"]) != lead_time_minutes
        ):
            connection.execute(
                """
                UPDATE contest_prediction_reminder_settings
                SET enabled = ?,
                    lead_time_minutes = ?,
                    revision = revision + 1,
                    updated_by_user_id = ?,
                    updated_at = ?
                WHERE contest_id = ?
                """,
                (
                    int(enabled),
                    lead_time_minutes,
                    actor_user_id,
                    now_value,
                    contest_id,
                ),
            )
        refreshed = connection.execute(
            """
            SELECT enabled, lead_time_minutes, revision
            FROM contest_prediction_reminder_settings
            WHERE contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
    if refreshed is None:
        raise RuntimeError("Saved reminder settings were not found.")
    return _settings_from_row(contest_id=contest_id, row=refreshed)


def get_reminder_preference(
    *, database_path: Path, chat_id: int, user_id: int
) -> ReminderPreference:
    with database_connection(database_path) as connection:
        _require_chat_and_user(connection, chat_id=chat_id, user_id=user_id)
        row = connection.execute(
            """
            SELECT mention_in_prediction_reminders, revision
            FROM chat_user_prediction_reminder_preferences
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        ).fetchone()
    if row is None:
        return ReminderPreference(
            chat_id=chat_id,
            user_id=user_id,
            mention_in_prediction_reminders=False,
            revision=0,
        )
    return _preference_from_row(chat_id=chat_id, user_id=user_id, row=row)


def save_reminder_preference(
    *,
    database_path: Path,
    chat_id: int,
    user_id: int,
    mention_in_prediction_reminders: bool,
    now_utc: datetime | None = None,
) -> ReminderPreference:
    _require_bool(
        mention_in_prediction_reminders,
        field_name="mention_in_prediction_reminders",
    )
    now_value = serialize_reminder_time(resolve_reminder_time(now_utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_chat_and_user(connection, chat_id=chat_id, user_id=user_id)
        row = connection.execute(
            """
            SELECT mention_in_prediction_reminders, revision
            FROM chat_user_prediction_reminder_preferences
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO chat_user_prediction_reminder_preferences (
                    chat_id, user_id, mention_in_prediction_reminders,
                    revision, created_at, updated_at
                )
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    int(mention_in_prediction_reminders),
                    now_value,
                    now_value,
                ),
            )
        elif bool(row["mention_in_prediction_reminders"]) != (
            mention_in_prediction_reminders
        ):
            connection.execute(
                """
                UPDATE chat_user_prediction_reminder_preferences
                SET mention_in_prediction_reminders = ?,
                    revision = revision + 1,
                    updated_at = ?
                WHERE chat_id = ? AND user_id = ?
                """,
                (
                    int(mention_in_prediction_reminders),
                    now_value,
                    chat_id,
                    user_id,
                ),
            )
        refreshed = connection.execute(
            """
            SELECT mention_in_prediction_reminders, revision
            FROM chat_user_prediction_reminder_preferences
            WHERE chat_id = ? AND user_id = ?
            """,
            (chat_id, user_id),
        ).fetchone()
    if refreshed is None:
        raise RuntimeError("Saved reminder preference was not found.")
    return _preference_from_row(chat_id=chat_id, user_id=user_id, row=refreshed)


def queue_manual_prediction_reminder(
    *,
    database_path: Path,
    contest_id: int,
    actor_user_id: int,
    idempotency_key: str,
    now_utc: datetime | None = None,
) -> ManualReminderQueueResult:
    """Queue one broad manual reminder and return the idempotent request.

    Manual rendering intentionally receives no frozen match list.  Occurrences
    which are already inside their automatic lead window are attached only as
    suppression markers: a successfully or ambiguously delivered manual
    reminder covers them, while a definite pre-send failure releases them.
    """

    normalized_key = _normalize_manual_idempotency_key(idempotency_key)
    now = resolve_reminder_time(now_utc)
    now_value = serialize_reminder_time(now)
    request_fingerprint = hashlib.sha256(b"broad-manual-reminder-v1").hexdigest()

    with database_connection(database_path) as connection:
        existing = connection.execute(
            """
            SELECT id, delivery_id, request_fingerprint
            FROM prediction_reminder_manual_requests
            WHERE original_contest_id = ?
              AND original_actor_user_id = ?
              AND idempotency_key = ?
            """,
            (contest_id, actor_user_id, normalized_key),
        ).fetchone()
    if existing is not None:
        if str(existing["request_fingerprint"]) != request_fingerprint:
            raise PredictionReminderStoreError(
                "The idempotency key was already used for another request."
            )
        return ManualReminderQueueResult(
            request_id=int(existing["id"]),
            delivery_id=int(existing["delivery_id"]),
            was_created=False,
        )

    # Materialize any newly-due automatic occurrences before taking the write
    # lock used for idempotent manual enqueue/suppression.
    reconcile_prediction_reminder_occurrences(database_path=database_path, now_utc=now)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT id, delivery_id, request_fingerprint
            FROM prediction_reminder_manual_requests
            WHERE original_contest_id = ?
              AND original_actor_user_id = ?
              AND idempotency_key = ?
            """,
            (contest_id, actor_user_id, normalized_key),
        ).fetchone()
        if existing is not None:
            if str(existing["request_fingerprint"]) != request_fingerprint:
                raise PredictionReminderStoreError(
                    "The idempotency key was already used for another request."
                )
            return ManualReminderQueueResult(
                request_id=int(existing["id"]),
                delivery_id=int(existing["delivery_id"]),
                was_created=False,
            )
        contest = connection.execute(
            "SELECT is_active FROM contests WHERE id = ?", (contest_id,)
        ).fetchone()
        if contest is None:
            raise PredictionReminderStoreError("Contest was not found.")
        if not bool(contest["is_active"]):
            raise PredictionReminderStoreError("Contest is not active.")
        actor = connection.execute(
            "SELECT 1 FROM users WHERE id = ?", (actor_user_id,)
        ).fetchone()
        if actor is None:
            raise PredictionReminderStoreError("Actor was not found.")

        # A queued, not-yet-started auto delivery can be safely replaced by the
        # explicit manual send.  Once any Telegram call may have happened it is
        # deliberately left alone.
        batched_auto_rows = connection.execute(
            """
            SELECT occurrence.delivery_id,
                   matches.starts_at_utc AS current_starts_at_utc,
                   occurrence.observed_starts_at_utc
            FROM prediction_reminder_occurrences AS occurrence
            JOIN prediction_reminder_deliveries AS delivery
              ON delivery.id = occurrence.delivery_id
            JOIN matches ON matches.id = occurrence.match_id
            WHERE occurrence.contest_id = ?
              AND occurrence.status = 'batched'
              AND delivery.source = 'auto'
              AND occurrence.due_at <= ?
              AND occurrence.observed_starts_at_utc > ?
              AND matches.status = 'scheduled'
            ORDER BY occurrence.delivery_id
            """,
            (contest_id, now_value, now_value),
        ).fetchall()
        batched_auto_deadlines = connection.execute(
            """
            SELECT occurrence.delivery_id,
                   occurrence.observed_deadline_at_utc,
                   CASE occurrence.deadline_kind
                     WHEN 'swiss' THEN swiss.enabled
                     ELSE contests.champion_prediction_enabled
                   END AS current_deadline_enabled,
                   CASE occurrence.deadline_kind
                     WHEN 'swiss' THEN swiss.deadline_at
                     ELSE contests.champion_prediction_deadline_at
                   END AS current_deadline_at_utc
            FROM prediction_reminder_deadline_occurrences AS occurrence
            JOIN prediction_reminder_deliveries AS delivery
              ON delivery.id = occurrence.delivery_id
            JOIN contests ON contests.id = occurrence.contest_id
            LEFT JOIN swiss_stage_prediction_settings AS swiss
              ON swiss.contest_id = occurrence.contest_id
            WHERE occurrence.contest_id = ?
              AND occurrence.status = 'batched'
              AND delivery.source = 'auto'
              AND occurrence.due_at <= ?
              AND occurrence.observed_deadline_at_utc > ?
            ORDER BY occurrence.delivery_id
            """,
            (contest_id, now_value, now_value),
        ).fetchall()
        safe_auto_delivery_ids = {
            int(row["delivery_id"])
            for row in batched_auto_rows
            if _same_reminder_instant(
                row["current_starts_at_utc"], row["observed_starts_at_utc"]
            )
        }
        safe_auto_delivery_ids.update(
            int(row["delivery_id"])
            for row in batched_auto_deadlines
            if bool(row["current_deadline_enabled"])
            and row["current_deadline_at_utc"] is not None
            and _same_reminder_instant(
                row["current_deadline_at_utc"],
                row["observed_deadline_at_utc"],
            )
        )
        for delivery_id in sorted(safe_auto_delivery_ids):
            _reset_delivery_if_safe(
                connection,
                delivery_id=delivery_id,
                now_value=now_value,
                reason="Covered by an explicitly queued manual reminder.",
            )

        covered = connection.execute(
            """
            SELECT occurrence.id AS occurrence_id,
                   occurrence.schedule_revision,
                   occurrence.observed_starts_at_utc,
                   matches.id AS match_id,
                   matches.starts_at_utc,
                   home_team.name AS home_team_name,
                   away_team.name AS away_team_name
            FROM prediction_reminder_occurrences AS occurrence
            JOIN matches ON matches.id = occurrence.match_id
            JOIN teams AS home_team ON home_team.id = matches.home_team_id
            JOIN teams AS away_team ON away_team.id = matches.away_team_id
            WHERE occurrence.contest_id = ?
              AND occurrence.status = 'scheduled'
              AND occurrence.due_at <= ?
              AND occurrence.observed_starts_at_utc > ?
              AND matches.status = 'scheduled'
            ORDER BY occurrence.observed_starts_at_utc, occurrence.id
            """,
            (contest_id, now_value, now_value),
        ).fetchall()
        covered = [
            row
            for row in covered
            if _same_reminder_instant(
                row["starts_at_utc"], row["observed_starts_at_utc"]
            )
        ]
        covered_deadlines = connection.execute(
            """
            SELECT occurrence.id AS occurrence_id,
                   occurrence.observed_deadline_at_utc,
                   CASE occurrence.deadline_kind
                     WHEN 'swiss' THEN swiss.enabled
                     ELSE contests.champion_prediction_enabled
                   END AS current_deadline_enabled,
                   CASE occurrence.deadline_kind
                     WHEN 'swiss' THEN swiss.deadline_at
                     ELSE contests.champion_prediction_deadline_at
                   END AS current_deadline_at_utc
            FROM prediction_reminder_deadline_occurrences AS occurrence
            JOIN contests ON contests.id = occurrence.contest_id
            LEFT JOIN swiss_stage_prediction_settings AS swiss
              ON swiss.contest_id = occurrence.contest_id
            WHERE occurrence.contest_id = ?
              AND occurrence.status = 'scheduled'
              AND occurrence.due_at <= ?
              AND occurrence.observed_deadline_at_utc > ?
            ORDER BY occurrence.observed_deadline_at_utc, occurrence.id
            """,
            (contest_id, now_value, now_value),
        ).fetchall()
        covered_deadlines = [
            row
            for row in covered_deadlines
            if bool(row["current_deadline_enabled"])
            and row["current_deadline_at_utc"] is not None
            and _same_reminder_instant(
                row["current_deadline_at_utc"],
                row["observed_deadline_at_utc"],
            )
        ]
        cutoff = now + timedelta(minutes=MANUAL_REMINDER_EXPIRY_MINUTES)
        if covered:
            earliest_start = min(
                _parse_reminder_time(row["starts_at_utc"], field_name="match start")
                for row in covered
            )
            cutoff = min(cutoff, earliest_start)
        if covered_deadlines:
            earliest_deadline = min(
                _parse_reminder_time(
                    row["observed_deadline_at_utc"],
                    field_name="prediction deadline",
                )
                for row in covered_deadlines
            )
            cutoff = min(cutoff, earliest_deadline)
        settings = connection.execute(
            """
            SELECT revision FROM contest_prediction_reminder_settings
            WHERE contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
        settings_revision = int(settings["revision"]) if settings is not None else 1
        sequence = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(supplemental_sequence), 0) + 1
                FROM prediction_reminder_deliveries
                WHERE original_contest_id = ? AND source = 'manual'
                """,
                (contest_id,),
            ).fetchone()[0]
        )
        cursor = connection.execute(
            """
            INSERT INTO prediction_reminder_deliveries (
                contest_id, original_contest_id, source, batch_starts_at_utc,
                supplemental_sequence, expires_at, settings_revision,
                status, next_attempt_at, created_at, updated_at
            )
            VALUES (?, ?, 'manual', NULL, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                contest_id,
                contest_id,
                sequence,
                serialize_reminder_time(cutoff),
                settings_revision,
                now_value,
                now_value,
                now_value,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("Manual reminder delivery was not created.")
        delivery_id = int(cursor.lastrowid)
        for item_order, row in enumerate(covered):
            occurrence_id = int(row["occurrence_id"])
            connection.execute(
                """
                INSERT INTO prediction_reminder_delivery_items (
                    delivery_id, occurrence_id, original_occurrence_id,
                    occurrence_schedule_revision, match_id_snapshot,
                    starts_at_snapshot, home_team_name, away_team_name,
                    item_order
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    occurrence_id,
                    occurrence_id,
                    int(row["schedule_revision"]),
                    int(row["match_id"]),
                    serialize_reminder_time(
                        _parse_reminder_time(
                            row["starts_at_utc"], field_name="match start"
                        )
                    ),
                    str(row["home_team_name"]),
                    str(row["away_team_name"]),
                    item_order,
                ),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'batched', delivery_id = ?, updated_at = ?
                WHERE id = ? AND status = 'scheduled'
                """,
                (delivery_id, now_value, occurrence_id),
            )
        for row in covered_deadlines:
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'batched', delivery_id = ?, updated_at = ?
                WHERE id = ? AND status = 'scheduled'
                """,
                (delivery_id, now_value, int(row["occurrence_id"])),
            )
        request_cursor = connection.execute(
            """
            INSERT INTO prediction_reminder_manual_requests (
                contest_id, original_contest_id, actor_user_id,
                original_actor_user_id, idempotency_key, request_fingerprint,
                delivery_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contest_id,
                contest_id,
                actor_user_id,
                actor_user_id,
                normalized_key,
                request_fingerprint,
                delivery_id,
                now_value,
            ),
        )
        if request_cursor.lastrowid is None:
            raise RuntimeError("Manual reminder request was not created.")
        return ManualReminderQueueResult(
            request_id=int(request_cursor.lastrowid),
            delivery_id=delivery_id,
            was_created=True,
        )


def get_manual_prediction_reminder_request(
    *,
    database_path: Path,
    contest_id: int,
    actor_user_id: int,
    idempotency_key: str,
) -> ManualReminderQueueResult | None:
    """Return an earlier idempotent manual request without creating a delivery."""

    normalized_key = _normalize_manual_idempotency_key(idempotency_key)
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT id, delivery_id
            FROM prediction_reminder_manual_requests
            WHERE original_contest_id = ?
              AND original_actor_user_id = ?
              AND idempotency_key = ?
            """,
            (contest_id, actor_user_id, normalized_key),
        ).fetchone()
    if row is None:
        return None
    return ManualReminderQueueResult(
        request_id=int(row["id"]),
        delivery_id=int(row["delivery_id"]),
        was_created=False,
    )


def reconcile_prediction_reminder_occurrences(
    *, database_path: Path, now_utc: datetime | None = None
) -> int:
    now = resolve_reminder_time(now_utc)
    now_value = serialize_reminder_time(now)
    changed_count = 0
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _recover_abandoned_claims(connection, now=now)
        _expire_unclaimed_deliveries(connection, now=now)
        changed_count += _reconcile_existing_occurrences(
            connection, now=now, now_value=now_value
        )
        changed_count += _reconcile_existing_deadline_occurrences(
            connection, now=now, now_value=now_value
        )
        rows = connection.execute(
            """
            SELECT
                matches.id AS match_id,
                matches.starts_at_utc,
                matches.created_at AS match_created_at,
                competitions.contest_id,
                settings.lead_time_minutes,
                occurrence.id AS occurrence_id,
                occurrence.observed_starts_at_utc
                    AS occurrence_observed_starts_at_utc,
                occurrence.due_at AS occurrence_due_at,
                occurrence.schedule_revision AS occurrence_schedule_revision,
                occurrence.status AS occurrence_status,
                occurrence.delivery_id AS occurrence_delivery_id,
                delivery.source AS occurrence_delivery_source
            FROM matches
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            JOIN contests ON contests.id = competitions.contest_id
            JOIN contest_prediction_reminder_settings AS settings
              ON settings.contest_id = contests.id
            LEFT JOIN prediction_reminder_occurrences AS occurrence
              ON occurrence.match_id = matches.id
            LEFT JOIN prediction_reminder_deliveries AS delivery
              ON delivery.id = occurrence.delivery_id
            WHERE contests.is_active = 1
              AND settings.enabled = 1
              AND matches.status = 'scheduled'
            ORDER BY competitions.contest_id, matches.starts_at_utc, matches.id
            """
        ).fetchall()
        for row in rows:
            starts_at = _parse_reminder_time(
                row["starts_at_utc"], field_name="match start"
            )
            if starts_at <= now:
                continue
            match_id = int(row["match_id"])
            existing = (
                {
                    "id": row["occurrence_id"],
                    "observed_starts_at_utc": row["occurrence_observed_starts_at_utc"],
                    "due_at": row["occurrence_due_at"],
                    "schedule_revision": row["occurrence_schedule_revision"],
                    "status": row["occurrence_status"],
                    "delivery_id": row["occurrence_delivery_id"],
                    "delivery_source": row["occurrence_delivery_source"],
                }
                if row["occurrence_id"] is not None
                else None
            )
            due_at = serialize_reminder_time(
                starts_at - timedelta(minutes=int(row["lead_time_minutes"]))
            )
            starts_value = serialize_reminder_time(starts_at)
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO prediction_reminder_occurrences (
                        contest_id, original_contest_id, match_id,
                        original_match_id, match_created_at_snapshot,
                        observed_starts_at_utc, due_at, schedule_revision,
                        status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'scheduled', ?, ?)
                    """,
                    (
                        int(row["contest_id"]),
                        int(row["contest_id"]),
                        match_id,
                        match_id,
                        str(row["match_created_at"]),
                        starts_value,
                        due_at,
                        now_value,
                        now_value,
                    ),
                )
                changed_count += 1
                continue
            start_changed = str(existing["observed_starts_at_utc"]) != starts_value
            due_changed = str(existing["due_at"]) != due_at
            if not start_changed and not due_changed:
                continue
            if not start_changed and str(existing["status"]) not in (
                "scheduled",
                "batched",
            ):
                # A lead-time setting change never rearms an already finalized match.
                continue
            if str(existing["status"]) == "batched":
                delivery_id = existing["delivery_id"]
                if str(existing["delivery_source"]) == "auto":
                    if delivery_id is None or not _reset_delivery_if_safe(
                        connection,
                        delivery_id=int(delivery_id),
                        now_value=now_value,
                        reason="Match schedule changed before reminder delivery.",
                    ):
                        continue
                elif not start_changed:
                    # A manual reminder is broad and remains queued.  Keep its
                    # coverage marker attached while updating the future auto due.
                    connection.execute(
                        """
                        UPDATE prediction_reminder_occurrences
                        SET due_at = ?, updated_at = ?
                        WHERE id = ? AND status = 'batched'
                        """,
                        (due_at, now_value, int(existing["id"])),
                    )
                    changed_count += 1
                    continue
            if start_changed:
                next_revision = int(existing["schedule_revision"]) + 1
                connection.execute(
                    """
                    UPDATE prediction_reminder_occurrences
                    SET match_id = NULL, status = 'cancelled', delivery_id = NULL,
                        finalized_at = COALESCE(finalized_at, ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (now_value, now_value, int(existing["id"])),
                )
                connection.execute(
                    """
                    INSERT INTO prediction_reminder_occurrences (
                        contest_id, original_contest_id, match_id,
                        original_match_id, match_created_at_snapshot,
                        observed_starts_at_utc, due_at, schedule_revision,
                        status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
                    """,
                    (
                        int(row["contest_id"]),
                        int(row["contest_id"]),
                        match_id,
                        match_id,
                        str(row["match_created_at"]),
                        starts_value,
                        due_at,
                        next_revision,
                        now_value,
                        now_value,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE prediction_reminder_occurrences
                    SET due_at = ?, status = 'scheduled', delivery_id = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (due_at, now_value, int(existing["id"])),
                )
            changed_count += 1
        deadline_rows = connection.execute(
            """
            SELECT contests.id AS contest_id, settings.lead_time_minutes,
                   contests.champion_prediction_enabled,
                   contests.champion_prediction_deadline_at,
                   swiss.enabled AS swiss_enabled,
                   swiss.deadline_at AS swiss_deadline_at
            FROM contests
            JOIN contest_prediction_reminder_settings AS settings
              ON settings.contest_id = contests.id
            LEFT JOIN swiss_stage_prediction_settings AS swiss
              ON swiss.contest_id = contests.id
            WHERE contests.is_active = 1 AND settings.enabled = 1
            ORDER BY contests.id
            """
        ).fetchall()
        for row in deadline_rows:
            deadline_values = (
                (
                    "swiss",
                    bool(row["swiss_enabled"]),
                    row["swiss_deadline_at"],
                ),
                (
                    "champion",
                    bool(row["champion_prediction_enabled"]),
                    row["champion_prediction_deadline_at"],
                ),
            )
            for deadline_kind, enabled, raw_deadline in deadline_values:
                if not enabled or raw_deadline is None:
                    continue
                deadline = _parse_reminder_time(
                    raw_deadline,
                    field_name=f"{deadline_kind} prediction deadline",
                )
                if deadline <= now:
                    continue
                changed_count += _ensure_deadline_occurrence(
                    connection,
                    contest_id=int(row["contest_id"]),
                    deadline_kind=deadline_kind,
                    deadline=deadline,
                    lead_time_minutes=int(row["lead_time_minutes"]),
                    now_value=now_value,
                )
    return changed_count


def claim_next_prediction_reminder_delivery(
    *,
    database_path: Path,
    now_utc: datetime | None = None,
    lease_seconds: int = REMINDER_CLAIM_SECONDS,
) -> ClaimedReminderDelivery | None:
    now = resolve_reminder_time(now_utc)
    now_value = serialize_reminder_time(now)
    expires_value = serialize_reminder_time(now + timedelta(seconds=lease_seconds))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _recover_abandoned_claims(connection, now=now)
        _expire_unclaimed_deliveries(connection, now=now)
        retry = connection.execute(
            """
            SELECT delivery.*, chats.telegram_chat_id
            FROM prediction_reminder_deliveries AS delivery
            JOIN contests ON contests.id = delivery.contest_id
            JOIN chats ON chats.id = contests.chat_id
            LEFT JOIN contest_prediction_reminder_settings AS settings
              ON settings.contest_id = delivery.contest_id
            WHERE delivery.status IN ('pending', 'retry')
              AND (
                  delivery.next_attempt_at IS NULL
                  OR delivery.next_attempt_at <= ?
              )
              AND delivery.expires_at > ?
              AND delivery.claim_token IS NULL
              AND contests.is_active = 1
              AND (
                  delivery.source = 'manual'
                  OR EXISTS (
                      SELECT 1
                      FROM prediction_reminder_delivery_parts AS sent_part
                      WHERE sent_part.delivery_id = delivery.id
                        AND sent_part.status = 'sent'
                  )
                  OR (
                      settings.enabled = 1
                      AND settings.revision = delivery.settings_revision
                  )
              )
            ORDER BY COALESCE(delivery.next_attempt_at, delivery.created_at),
                     delivery.id
            LIMIT 1
            """,
            (now_value, now_value),
        ).fetchone()
        if retry is not None:
            return _claim_delivery_row(
                connection,
                row=retry,
                claim_expires_at=expires_value,
                now_value=now_value,
            )

        match_candidates = connection.execute(
            """
            SELECT occurrence.*, settings.revision AS settings_revision,
                   settings.lead_time_minutes,
                   chats.telegram_chat_id,
                   matches.starts_at_utc AS current_starts_at_utc
            FROM prediction_reminder_occurrences AS occurrence
            JOIN matches ON matches.id = occurrence.match_id
            JOIN contests ON contests.id = occurrence.contest_id
            JOIN chats ON chats.id = contests.chat_id
            JOIN contest_prediction_reminder_settings AS settings
              ON settings.contest_id = occurrence.contest_id
            WHERE occurrence.status = 'scheduled'
              AND occurrence.due_at <= ?
              AND occurrence.observed_starts_at_utc > ?
              AND matches.status = 'scheduled'
              AND contests.is_active = 1
              AND settings.enabled = 1
            ORDER BY occurrence.due_at, occurrence.id
            """,
            (now_value, now_value),
        ).fetchall()
        deadline_candidates = connection.execute(
            """
            SELECT occurrence.*, settings.revision AS settings_revision,
                   settings.lead_time_minutes,
                   chats.telegram_chat_id,
                   CASE occurrence.deadline_kind
                     WHEN 'swiss' THEN swiss.enabled
                     ELSE contests.champion_prediction_enabled
                   END AS current_deadline_enabled,
                   CASE occurrence.deadline_kind
                     WHEN 'swiss' THEN swiss.deadline_at
                     ELSE contests.champion_prediction_deadline_at
                   END AS current_deadline_at_utc
            FROM prediction_reminder_deadline_occurrences AS occurrence
            JOIN contests ON contests.id = occurrence.contest_id
            JOIN chats ON chats.id = contests.chat_id
            JOIN contest_prediction_reminder_settings AS settings
              ON settings.contest_id = occurrence.contest_id
            LEFT JOIN swiss_stage_prediction_settings AS swiss
              ON swiss.contest_id = occurrence.contest_id
            WHERE occurrence.status = 'scheduled'
              AND occurrence.due_at <= ?
              AND occurrence.observed_deadline_at_utc > ?
              AND contests.is_active = 1
              AND settings.enabled = 1
            ORDER BY occurrence.due_at, occurrence.id
            """,
            (now_value, now_value),
        ).fetchall()
        candidates: list[tuple[str, sqlite3.Row]] = []
        candidates.extend(
            ("match", row)
            for row in match_candidates
            if _same_reminder_instant(
                row["current_starts_at_utc"], row["observed_starts_at_utc"]
            )
            and _occurrence_due_matches_current_lead_time(
                due_at=row["due_at"],
                deadline_at=row["current_starts_at_utc"],
                lead_time_minutes=int(row["lead_time_minutes"]),
            )
        )
        candidates.extend(
            ("deadline", row)
            for row in deadline_candidates
            if bool(row["current_deadline_enabled"])
            and row["current_deadline_at_utc"] is not None
            and _same_reminder_instant(
                row["current_deadline_at_utc"],
                row["observed_deadline_at_utc"],
            )
            and _occurrence_due_matches_current_lead_time(
                due_at=row["due_at"],
                deadline_at=row["current_deadline_at_utc"],
                lead_time_minutes=int(row["lead_time_minutes"]),
            )
        )
        if not candidates:
            return None
        first_kind, first = min(
            candidates,
            key=lambda candidate: (
                str(candidate[1]["due_at"]),
                0 if candidate[0] == "match" else 1,
                int(candidate[1]["id"]),
            ),
        )

        contest_id = int(first["contest_id"])
        batch_start = str(
            first[
                "observed_starts_at_utc"
                if first_kind == "match"
                else "observed_deadline_at_utc"
            ]
        )
        sequence = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(supplemental_sequence), 0) + 1
                FROM prediction_reminder_deliveries
                WHERE original_contest_id = ? AND batch_starts_at_utc = ?
                """,
                (contest_id, batch_start),
            ).fetchone()[0]
        )
        claim_token = uuid4().hex
        delivery_cursor = connection.execute(
            """
            INSERT INTO prediction_reminder_deliveries (
                contest_id, original_contest_id, source, batch_starts_at_utc,
                supplemental_sequence, expires_at, settings_revision,
                status, claim_token, claim_expires_at, next_attempt_at,
                created_at, updated_at
            )
            VALUES (?, ?, 'auto', ?, ?, ?, ?, 'preparing', ?, ?, ?, ?, ?)
            """,
            (
                contest_id,
                contest_id,
                batch_start,
                sequence,
                batch_start,
                int(first["settings_revision"]),
                claim_token,
                expires_value,
                now_value,
                now_value,
                now_value,
            ),
        )
        if delivery_cursor.lastrowid is None:
            raise RuntimeError("Reminder delivery was not created.")
        delivery_id = int(delivery_cursor.lastrowid)
        item_rows = connection.execute(
            """
            SELECT occurrence.id AS occurrence_id,
                   occurrence.schedule_revision,
                   matches.id AS match_id,
                   matches.starts_at_utc,
                   home_team.name AS home_team_name,
                   away_team.name AS away_team_name
            FROM prediction_reminder_occurrences AS occurrence
            JOIN matches ON matches.id = occurrence.match_id
            JOIN teams AS home_team ON home_team.id = matches.home_team_id
            JOIN teams AS away_team ON away_team.id = matches.away_team_id
            WHERE occurrence.contest_id = ?
              AND occurrence.status = 'scheduled'
              AND occurrence.due_at <= ?
              AND occurrence.observed_starts_at_utc = ?
              AND matches.status = 'scheduled'
            ORDER BY occurrence.id
            """,
            (contest_id, now_value, batch_start),
        ).fetchall()
        item_rows = [
            row
            for row in item_rows
            if _same_reminder_instant(row["starts_at_utc"], batch_start)
        ]
        deadline_item_rows = connection.execute(
            """
            SELECT occurrence.id AS occurrence_id,
                   occurrence.deadline_kind,
                   occurrence.observed_deadline_at_utc,
                   CASE occurrence.deadline_kind
                     WHEN 'swiss' THEN swiss.enabled
                     ELSE contests.champion_prediction_enabled
                   END AS current_deadline_enabled,
                   CASE occurrence.deadline_kind
                     WHEN 'swiss' THEN swiss.deadline_at
                     ELSE contests.champion_prediction_deadline_at
                   END AS current_deadline_at_utc
            FROM prediction_reminder_deadline_occurrences AS occurrence
            JOIN contests ON contests.id = occurrence.contest_id
            LEFT JOIN swiss_stage_prediction_settings AS swiss
              ON swiss.contest_id = occurrence.contest_id
            WHERE occurrence.contest_id = ?
              AND occurrence.status = 'scheduled'
              AND occurrence.due_at <= ?
              AND occurrence.observed_deadline_at_utc = ?
            ORDER BY occurrence.id
            """,
            (contest_id, now_value, batch_start),
        ).fetchall()
        deadline_item_rows = [
            row
            for row in deadline_item_rows
            if bool(row["current_deadline_enabled"])
            and row["current_deadline_at_utc"] is not None
            and _same_reminder_instant(
                row["current_deadline_at_utc"],
                row["observed_deadline_at_utc"],
            )
        ]
        if not item_rows and not deadline_item_rows:
            raise RuntimeError("Reminder delivery has no due predictions.")
        for item_order, item in enumerate(item_rows):
            connection.execute(
                """
                INSERT INTO prediction_reminder_delivery_items (
                    delivery_id, occurrence_id, original_occurrence_id,
                    occurrence_schedule_revision,
                    match_id_snapshot, starts_at_snapshot,
                    home_team_name, away_team_name, item_order
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    int(item["occurrence_id"]),
                    int(item["occurrence_id"]),
                    int(item["schedule_revision"]),
                    int(item["match_id"]),
                    serialize_reminder_time(
                        _parse_reminder_time(
                            item["starts_at_utc"], field_name="match start"
                        )
                    ),
                    str(item["home_team_name"]),
                    str(item["away_team_name"]),
                    item_order,
                ),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'batched', delivery_id = ?, updated_at = ?
                WHERE id = ? AND status = 'scheduled'
                """,
                (delivery_id, now_value, int(item["occurrence_id"])),
            )
        for item in deadline_item_rows:
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'batched', delivery_id = ?, updated_at = ?
                WHERE id = ? AND status = 'scheduled'
                """,
                (delivery_id, now_value, int(item["occurrence_id"])),
            )
        return ClaimedReminderDelivery(
            id=delivery_id,
            contest_id=contest_id,
            telegram_chat_id=int(first["telegram_chat_id"]),
            kind="automatic",
            batch_starts_at_utc=batch_start,
            supplemental_sequence=sequence,
            settings_revision=int(first["settings_revision"]),
            claim_token=claim_token,
        )


def prepare_prediction_reminder_render_request(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    now_utc: datetime | None = None,
) -> ReminderRenderRequest | None:
    return refresh_prediction_reminder_recipients(
        database_path=database_path,
        delivery=delivery,
        now_utc=now_utc,
    )


def refresh_prediction_reminder_recipients(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    now_utc: datetime | None = None,
) -> ReminderRenderRequest | None:
    """Refresh the render snapshot while no Telegram send may have started.

    Safe retries (notably Telegram 429 responses) return parts to ``pending``.
    The next claim discards those unsent parts, re-evaluates opt-ins and missing
    forecasts, and lets the caller render fresh HTML.  Once a part is sending,
    sent, or unknown the snapshot is immutable.
    """

    now = resolve_reminder_time(now_utc)
    now_value = serialize_reminder_time(now)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not _claim_is_current(
            connection,
            delivery=delivery,
            now_value=now_value,
            required_status="preparing",
        ):
            _settle_stale_claim(connection, delivery=delivery, now_value=now_value)
            return None
        row = connection.execute(
            """
            SELECT contests.name, chats.telegram_chat_id,
                   delivery.snapshot_at
            FROM prediction_reminder_deliveries AS delivery
            JOIN contests ON contests.id = delivery.contest_id
            JOIN chats ON chats.id = contests.chat_id
            WHERE delivery.id = ? AND delivery.claim_token = ?
            """,
            (delivery.id, delivery.claim_token),
        ).fetchone()
        if row is None:
            return None
        stored_items = _load_delivery_items(connection, delivery_id=delivery.id)
        stored_deadlines = _load_delivery_deadlines(connection, delivery_id=delivery.id)
        if delivery.kind == "automatic" and not stored_items and not stored_deadlines:
            _finish_terminal_in_connection(
                connection,
                delivery=delivery,
                error="Reminder delivery has no predictions.",
                now_value=now_value,
            )
            return None
        unsafe_parts = connection.execute(
            """
            SELECT status FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ?
              AND status IN ('sending', 'sent', 'unknown')
            """,
            (delivery.id,),
        ).fetchall()
        unsafe_statuses = {str(part["status"]) for part in unsafe_parts}
        if unsafe_statuses.intersection({"sending", "unknown"}):
            raise PredictionReminderClaimLostError(
                "Reminder recipients cannot change after a send may have started."
            )
        if "sent" in unsafe_statuses:
            # A multipart retry must preserve the exact snapshot once any part
            # was accepted.  The stored parts API resumes only pending parts.
            recipients = _load_delivery_recipients(connection, delivery_id=delivery.id)
        else:
            recipients = (
                _eligible_recipients(
                    connection,
                    delivery_id=delivery.id,
                    contest_id=delivery.contest_id,
                )
                if delivery.kind == "automatic"
                else _opted_in_participants(connection, contest_id=delivery.contest_id)
            )
            connection.execute(
                "DELETE FROM prediction_reminder_delivery_parts WHERE delivery_id = ?",
                (delivery.id,),
            )
            connection.execute(
                "DELETE FROM prediction_reminder_delivery_recipients "
                "WHERE delivery_id = ?",
                (delivery.id,),
            )
            for order, recipient in enumerate(recipients):
                connection.execute(
                    """
                    INSERT INTO prediction_reminder_delivery_recipients (
                        delivery_id, user_id, telegram_user_id, username,
                        first_name, last_name, recipient_order, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        delivery.id,
                        recipient.user_id,
                        recipient.telegram_user_id,
                        recipient.username,
                        recipient.first_name,
                        recipient.last_name,
                        order,
                    ),
                )
        connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET snapshot_at = ?, updated_at = ?
            WHERE id = ? AND claim_token = ? AND status = 'preparing'
            """,
            (now_value, now_value, delivery.id, delivery.claim_token),
        )
        return ReminderRenderRequest(
            delivery_id=delivery.id,
            contest_id=delivery.contest_id,
            contest_name=str(row["name"]),
            telegram_chat_id=int(row["telegram_chat_id"]),
            kind=delivery.kind,
            batch_starts_at_utc=delivery.batch_starts_at_utc,
            supplemental_sequence=delivery.supplemental_sequence,
            items=stored_items if delivery.kind == "automatic" else (),
            deadlines=stored_deadlines if delivery.kind == "automatic" else (),
            recipients=recipients,
        )


def load_prediction_reminder_parts(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    now_utc: datetime | None = None,
) -> tuple[StoredReminderPart, ...]:
    """Resume an immutable multipart delivery after at least one sent part.

    An empty tuple means the caller must refresh recipients, render, and store
    a new unsent snapshot.  A non-empty tuple atomically moves the claimed
    delivery back to ``sending``; callers skip its ``sent`` parts and continue
    only the ``pending`` ones.
    """

    now = resolve_reminder_time(now_utc)
    now_value = serialize_reminder_time(now)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not _claim_is_current(
            connection,
            delivery=delivery,
            now_value=now_value,
            required_status="preparing",
        ):
            _settle_stale_claim(connection, delivery=delivery, now_value=now_value)
            return ()
        rows = connection.execute(
            """
            SELECT * FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery.id,),
        ).fetchall()
        if not rows or not any(str(row["status"]) == "sent" for row in rows):
            return ()
        if any(str(row["status"]) not in {"sent", "pending"} for row in rows):
            raise PredictionReminderClaimLostError(
                "Reminder parts are not safe to resume."
            )
        current_recipients = (
            _eligible_recipients(
                connection,
                delivery_id=delivery.id,
                contest_id=delivery.contest_id,
            )
            if delivery.kind == "automatic"
            else _eligible_manual_recipients(
                connection,
                contest_id=delivery.contest_id,
                now=now,
            )
        )
        allowed_telegram_user_ids = {
            recipient.telegram_user_id for recipient in current_recipients
        }
        stored_recipients = connection.execute(
            """
            SELECT telegram_user_id
            FROM prediction_reminder_delivery_recipients
            WHERE delivery_id = ? AND status != 'suppressed'
            """,
            (delivery.id,),
        ).fetchall()
        for recipient in stored_recipients:
            telegram_user_id = int(recipient["telegram_user_id"])
            if telegram_user_id not in allowed_telegram_user_ids:
                connection.execute(
                    """
                    UPDATE prediction_reminder_delivery_recipients
                    SET status = 'suppressed'
                    WHERE delivery_id = ? AND telegram_user_id = ?
                    """,
                    (delivery.id, telegram_user_id),
                )
        for row in rows:
            if str(row["status"]) != "pending":
                continue
            current_html = str(row["html"])
            sanitized_html = _remove_ineligible_mentions(
                current_html,
                allowed_telegram_user_ids=allowed_telegram_user_ids,
            )
            if sanitized_html != current_html:
                connection.execute(
                    """
                    UPDATE prediction_reminder_delivery_parts
                    SET html = ?, content_hash = ?
                    WHERE delivery_id = ? AND part_number = ?
                      AND status = 'pending'
                    """,
                    (
                        sanitized_html,
                        hashlib.sha256(sanitized_html.encode("utf-8")).hexdigest(),
                        delivery.id,
                        int(row["part_number"]),
                    ),
                )
        update = connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'sending', updated_at = ?
            WHERE id = ? AND claim_token = ? AND status = 'preparing'
            """,
            (now_value, delivery.id, delivery.claim_token),
        )
        if update.rowcount != 1:
            return ()
        refreshed_rows = connection.execute(
            """
            SELECT * FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery.id,),
        ).fetchall()
        return tuple(_stored_part_from_row(row) for row in refreshed_rows)


def store_prediction_reminder_parts(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    parts: Sequence[RenderedReminderPart],
    now_utc: datetime | None = None,
) -> tuple[StoredReminderPart, ...]:
    if not parts:
        raise ValueError("A reminder delivery must contain at least one part.")
    for part in parts:
        if not isinstance(part.html, str) or not part.html:
            raise ValueError("Reminder part HTML must not be empty.")
        _require_bool(part.has_launch_button, field_name="has_launch_button")
    now_value = serialize_reminder_time(resolve_reminder_time(now_utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not _claim_is_current(
            connection,
            delivery=delivery,
            now_value=now_value,
            required_status="preparing",
        ):
            raise PredictionReminderClaimLostError(
                "Reminder claim is no longer current before sending."
            )
        stored = connection.execute(
            """
            SELECT * FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery.id,),
        ).fetchall()
        if not stored:
            for part_number, part in enumerate(parts):
                connection.execute(
                    """
                    INSERT INTO prediction_reminder_delivery_parts (
                        delivery_id, part_number, html, content_hash,
                        has_launch_button, status
                    )
                    VALUES (?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        delivery.id,
                        part_number,
                        part.html,
                        hashlib.sha256(part.html.encode("utf-8")).hexdigest(),
                        int(part.has_launch_button),
                    ),
                )
        connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'sending', updated_at = ?
            WHERE id = ? AND claim_token = ? AND status = 'preparing'
            """,
            (now_value, delivery.id, delivery.claim_token),
        )
        refreshed = connection.execute(
            """
            SELECT * FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery.id,),
        ).fetchall()
    return tuple(_stored_part_from_row(row) for row in refreshed)


def mark_prediction_reminder_part_sending(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    part_number: int,
    now_utc: datetime | None = None,
) -> bool:
    now_value = serialize_reminder_time(resolve_reminder_time(now_utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not _claim_is_current(
            connection,
            delivery=delivery,
            now_value=now_value,
            required_status="sending",
        ):
            _settle_stale_claim(connection, delivery=delivery, now_value=now_value)
            return False
        update = connection.execute(
            """
            UPDATE prediction_reminder_delivery_parts
            SET status = 'sending', send_started_at = ?, last_error = NULL
            WHERE delivery_id = ? AND part_number = ? AND status = 'pending'
              AND EXISTS (
                  SELECT 1 FROM prediction_reminder_deliveries AS delivery
                  WHERE delivery.id = ?
                    AND delivery.claim_token = ?
                    AND delivery.status = 'sending'
              )
            """,
            (
                now_value,
                delivery.id,
                part_number,
                delivery.id,
                delivery.claim_token,
            ),
        )
    return update.rowcount == 1


def record_prediction_reminder_part_sent(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    part_number: int,
    telegram_message_id: int,
    now_utc: datetime | None = None,
) -> bool:
    now_value = serialize_reminder_time(resolve_reminder_time(now_utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        update = connection.execute(
            """
            UPDATE prediction_reminder_delivery_parts
            SET status = 'sent', telegram_message_id = ?, sent_at = ?,
                last_error = NULL
            WHERE delivery_id = ? AND part_number = ? AND status = 'sending'
              AND EXISTS (
                  SELECT 1 FROM prediction_reminder_deliveries AS delivery
                  WHERE delivery.id = ? AND delivery.claim_token = ?
                    AND delivery.status = 'sending'
              )
            """,
            (
                telegram_message_id,
                now_value,
                delivery.id,
                part_number,
                delivery.id,
                delivery.claim_token,
            ),
        )
        if update.rowcount == 1:
            connection.execute(
                """
                UPDATE prediction_reminder_deliveries
                SET first_sent_at = COALESCE(first_sent_at, ?), updated_at = ?
                WHERE id = ? AND claim_token = ?
                """,
                (now_value, now_value, delivery.id, delivery.claim_token),
            )
    return update.rowcount == 1


def finish_prediction_reminder_success(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    now_utc: datetime | None = None,
) -> bool:
    now_value = serialize_reminder_time(resolve_reminder_time(now_utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        incomplete = connection.execute(
            """
            SELECT 1 FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? AND status NOT IN ('sent', 'skipped')
            LIMIT 1
            """,
            (delivery.id,),
        ).fetchone()
        if incomplete is not None:
            return False
        update = connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'sent', claim_token = NULL, claim_expires_at = NULL,
                next_attempt_at = NULL, last_error = NULL,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND claim_token = ? AND status = 'sending'
            """,
            (now_value, now_value, delivery.id, delivery.claim_token),
        )
        if update.rowcount == 1:
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'sent', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery.id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'sent', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery.id),
            )
    return update.rowcount == 1


def finish_prediction_reminder_retry(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    part_number: int | None,
    error: str,
    retry_after_seconds: float | None = None,
    now_utc: datetime | None = None,
) -> bool:
    now = resolve_reminder_time(now_utc)
    now_value = serialize_reminder_time(now)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT attempt_count, expires_at
            FROM prediction_reminder_deliveries
            WHERE id = ? AND claim_token = ?
            """,
            (delivery.id, delivery.claim_token),
        ).fetchone()
        if row is None:
            return False
        attempt_count = int(row["attempt_count"]) + 1
        if retry_after_seconds is None:
            retry_after_seconds = min(
                REMINDER_MAX_BACKOFF_SECONDS, 2 ** max(0, attempt_count - 1)
            )
        next_attempt = now + timedelta(seconds=max(0.0, retry_after_seconds))
        expires_at = _parse_reminder_time(row["expires_at"], field_name="expiry")
        terminal = attempt_count >= REMINDER_MAX_ATTEMPTS or next_attempt >= expires_at
        if part_number is not None:
            connection.execute(
                """
                UPDATE prediction_reminder_delivery_parts
                SET status = CASE WHEN ? THEN 'terminal_failed' ELSE 'pending' END,
                    last_error = ?
                WHERE delivery_id = ? AND part_number = ? AND status = 'sending'
                """,
                (int(terminal), error[:2000], delivery.id, part_number),
            )
        if terminal:
            return _finish_terminal_in_connection(
                connection,
                delivery=delivery,
                error=error,
                now_value=now_value,
                attempt_count=attempt_count,
            )
        next_value = serialize_reminder_time(next_attempt)
        update = connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'retry', attempt_count = ?, next_attempt_at = ?,
                last_error = ?, claim_token = NULL, claim_expires_at = NULL,
                updated_at = ?
            WHERE id = ? AND claim_token = ?
              AND status IN ('preparing', 'sending')
            """,
            (
                attempt_count,
                next_value,
                error[:2000],
                now_value,
                delivery.id,
                delivery.claim_token,
            ),
        )
    return update.rowcount == 1


def finish_prediction_reminder_terminal(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    error: str,
    part_number: int | None = None,
    now_utc: datetime | None = None,
) -> bool:
    now_value = serialize_reminder_time(resolve_reminder_time(now_utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if part_number is not None:
            connection.execute(
                """
                UPDATE prediction_reminder_delivery_parts
                SET status = 'terminal_failed', last_error = ?
                WHERE delivery_id = ? AND part_number = ? AND status = 'sending'
                """,
                (error[:2000], delivery.id, part_number),
            )
        return _finish_terminal_in_connection(
            connection,
            delivery=delivery,
            error=error,
            now_value=now_value,
        )


def finish_prediction_reminder_unknown(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    error: str,
    part_number: int | None = None,
    now_utc: datetime | None = None,
) -> bool:
    now_value = serialize_reminder_time(resolve_reminder_time(now_utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if part_number is not None:
            connection.execute(
                """
                UPDATE prediction_reminder_delivery_parts
                SET status = 'unknown', last_error = ?
                WHERE delivery_id = ? AND part_number = ? AND status = 'sending'
                """,
                (error[:2000], delivery.id, part_number),
            )
        sent_exists = connection.execute(
            """
            SELECT 1 FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? AND status = 'sent' LIMIT 1
            """,
            (delivery.id,),
        ).fetchone()
        update = connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'unknown', last_error = ?, claim_token = NULL,
                claim_expires_at = NULL, next_attempt_at = NULL,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND claim_token = ?
              AND status IN ('preparing', 'sending')
            """,
            (
                error[:2000],
                now_value,
                now_value,
                delivery.id,
                delivery.claim_token,
            ),
        )
        if update.rowcount == 1:
            occurrence_status = "sent" if sent_exists is not None else "unknown"
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = ?, finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (occurrence_status, now_value, now_value, delivery.id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = ?, finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (occurrence_status, now_value, now_value, delivery.id),
            )
    return update.rowcount == 1


def renew_prediction_reminder_claim(
    *,
    database_path: Path,
    delivery: ClaimedReminderDelivery,
    now_utc: datetime | None = None,
    lease_seconds: int = REMINDER_CLAIM_SECONDS,
) -> bool:
    now = resolve_reminder_time(now_utc)
    now_value = serialize_reminder_time(now)
    expires_value = serialize_reminder_time(now + timedelta(seconds=lease_seconds))
    with database_connection(database_path) as connection:
        update = connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET claim_expires_at = ?, updated_at = ?
            WHERE id = ? AND claim_token = ?
              AND status IN ('preparing', 'sending')
              AND expires_at > ?
            """,
            (
                expires_value,
                now_value,
                delivery.id,
                delivery.claim_token,
                now_value,
            ),
        )
    return update.rowcount == 1


def _reconcile_existing_occurrences(
    connection: sqlite3.Connection, *, now: datetime, now_value: str
) -> int:
    changed = 0
    rows = connection.execute(
        """
        SELECT occurrence.*, matches.status AS match_status,
               matches.starts_at_utc AS current_starts_at,
               contests.is_active,
               COALESCE(settings.enabled, 0) AS reminders_enabled,
               settings.revision AS current_settings_revision,
               delivery.source AS delivery_source,
               delivery.settings_revision AS delivery_settings_revision
        FROM prediction_reminder_occurrences AS occurrence
        LEFT JOIN matches ON matches.id = occurrence.match_id
        LEFT JOIN contests ON contests.id = occurrence.contest_id
        LEFT JOIN contest_prediction_reminder_settings AS settings
          ON settings.contest_id = occurrence.contest_id
        LEFT JOIN prediction_reminder_deliveries AS delivery
          ON delivery.id = occurrence.delivery_id
        WHERE occurrence.status IN ('scheduled', 'batched')
        ORDER BY occurrence.id
        """
    ).fetchall()
    for row in rows:
        final_status: str | None = None
        if row["match_id"] is None or row["match_status"] is None:
            final_status = "deleted"
        elif not bool(row["is_active"]):
            final_status = "expired"
        elif str(row["match_status"]) != "scheduled":
            final_status = "expired"
        elif (
            _parse_reminder_time(row["current_starts_at"], field_name="match start")
            <= now
        ):
            final_status = "expired"
        elif (
            str(row["status"]) == "batched"
            and str(row["delivery_source"]) == "auto"
            and (
                not bool(row["reminders_enabled"])
                or row["current_settings_revision"] is None
                or int(row["current_settings_revision"])
                != int(row["delivery_settings_revision"])
            )
        ):
            if _reset_delivery_if_safe(
                connection,
                delivery_id=int(row["delivery_id"]),
                now_value=now_value,
                reason="Automatic reminder settings changed.",
            ):
                changed += 1
            continue
        if final_status is None:
            continue
        if str(row["status"]) == "batched":
            if str(row["delivery_source"]) == "auto" and not (
                _reset_delivery_if_safe(
                    connection,
                    delivery_id=int(row["delivery_id"]),
                    now_value=now_value,
                    reason=f"Reminder occurrence became {final_status}.",
                )
            ):
                continue
            if str(row["delivery_source"]) == "manual":
                connection.execute(
                    """
                    UPDATE prediction_reminder_occurrences
                    SET status = ?, delivery_id = NULL,
                        finalized_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'batched'
                    """,
                    (final_status, now_value, now_value, int(row["id"])),
                )
                changed += 1
                continue
        connection.execute(
            """
            UPDATE prediction_reminder_occurrences
            SET status = ?, delivery_id = NULL, finalized_at = ?, updated_at = ?
            WHERE id = ? AND status = 'scheduled'
            """,
            (final_status, now_value, now_value, int(row["id"])),
        )
        changed += 1
    return changed


def _reconcile_existing_deadline_occurrences(
    connection: sqlite3.Connection,
    *,
    now: datetime,
    now_value: str,
) -> int:
    changed = 0
    rows = connection.execute(
        """
        SELECT occurrence.*, contests.is_active,
               COALESCE(settings.enabled, 0) AS reminders_enabled,
               settings.revision AS current_settings_revision,
               contests.champion_prediction_enabled,
               contests.champion_prediction_deadline_at,
               swiss.enabled AS swiss_enabled,
               swiss.deadline_at AS swiss_deadline_at,
               delivery.source AS delivery_source,
               delivery.settings_revision AS delivery_settings_revision
        FROM prediction_reminder_deadline_occurrences AS occurrence
        LEFT JOIN contests ON contests.id = occurrence.contest_id
        LEFT JOIN contest_prediction_reminder_settings AS settings
          ON settings.contest_id = occurrence.contest_id
        LEFT JOIN swiss_stage_prediction_settings AS swiss
          ON swiss.contest_id = occurrence.contest_id
        LEFT JOIN prediction_reminder_deliveries AS delivery
          ON delivery.id = occurrence.delivery_id
        WHERE occurrence.status IN ('scheduled', 'batched')
        ORDER BY occurrence.id
        """
    ).fetchall()
    for row in rows:
        deadline_kind = str(row["deadline_kind"])
        final_status: str | None = None
        if row["contest_id"] is None or row["is_active"] is None:
            final_status = "deleted"
        else:
            if deadline_kind == "swiss":
                deadline_enabled = bool(row["swiss_enabled"])
                raw_deadline = row["swiss_deadline_at"]
            else:
                deadline_enabled = bool(row["champion_prediction_enabled"])
                raw_deadline = row["champion_prediction_deadline_at"]
            if (
                not bool(row["is_active"])
                or not bool(row["reminders_enabled"])
                or not deadline_enabled
                or raw_deadline is None
            ):
                final_status = "expired"
            elif (
                _parse_reminder_time(
                    raw_deadline,
                    field_name=f"{deadline_kind} prediction deadline",
                )
                <= now
            ):
                final_status = "expired"
        if (
            final_status is None
            and str(row["status"]) == "batched"
            and str(row["delivery_source"]) == "auto"
            and (
                row["current_settings_revision"] is None
                or int(row["current_settings_revision"])
                != int(row["delivery_settings_revision"])
            )
        ):
            if _reset_delivery_if_safe(
                connection,
                delivery_id=int(row["delivery_id"]),
                now_value=now_value,
                reason="Automatic reminder settings changed.",
            ):
                changed += 1
            continue
        if final_status is None:
            continue
        if str(row["status"]) == "batched":
            if str(row["delivery_source"]) == "auto" and not (
                _reset_delivery_if_safe(
                    connection,
                    delivery_id=int(row["delivery_id"]),
                    now_value=now_value,
                    reason=f"Reminder deadline became {final_status}.",
                )
            ):
                continue
            if str(row["delivery_source"]) == "manual":
                connection.execute(
                    """
                    UPDATE prediction_reminder_deadline_occurrences
                    SET contest_id = NULL, status = ?, delivery_id = NULL,
                        finalized_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'batched'
                    """,
                    (final_status, now_value, now_value, int(row["id"])),
                )
                changed += 1
                continue
        connection.execute(
            """
            UPDATE prediction_reminder_deadline_occurrences
            SET contest_id = NULL, status = ?, delivery_id = NULL,
                finalized_at = ?, updated_at = ?
            WHERE id = ? AND status = 'scheduled'
            """,
            (final_status, now_value, now_value, int(row["id"])),
        )
        changed += 1
    return changed


def _ensure_deadline_occurrence(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    deadline_kind: ReminderDeadlineKind,
    deadline: datetime,
    lead_time_minutes: int,
    now_value: str,
) -> int:
    deadline_value = serialize_reminder_time(deadline)
    due_at = serialize_reminder_time(deadline - timedelta(minutes=lead_time_minutes))
    existing = connection.execute(
        """
        SELECT occurrence.*, delivery.source AS delivery_source
        FROM prediction_reminder_deadline_occurrences AS occurrence
        LEFT JOIN prediction_reminder_deliveries AS delivery
          ON delivery.id = occurrence.delivery_id
        WHERE occurrence.contest_id = ? AND occurrence.deadline_kind = ?
        """,
        (contest_id, deadline_kind),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO prediction_reminder_deadline_occurrences (
                contest_id, original_contest_id, deadline_kind,
                observed_deadline_at_utc, due_at, schedule_revision,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, 'scheduled', ?, ?)
            """,
            (
                contest_id,
                contest_id,
                deadline_kind,
                deadline_value,
                due_at,
                now_value,
                now_value,
            ),
        )
        return 1
    deadline_changed = str(existing["observed_deadline_at_utc"]) != deadline_value
    due_changed = str(existing["due_at"]) != due_at
    if not deadline_changed and not due_changed:
        return 0
    if not deadline_changed and str(existing["status"]) not in (
        "scheduled",
        "batched",
    ):
        return 0
    if str(existing["status"]) == "batched":
        delivery_id = existing["delivery_id"]
        if str(existing["delivery_source"]) == "auto":
            if delivery_id is None or not _reset_delivery_if_safe(
                connection,
                delivery_id=int(delivery_id),
                now_value=now_value,
                reason="Prediction deadline changed before reminder delivery.",
            ):
                return 0
        elif not deadline_changed:
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET due_at = ?, updated_at = ?
                WHERE id = ? AND status = 'batched'
                """,
                (due_at, now_value, int(existing["id"])),
            )
            return 1
    if deadline_changed:
        next_revision = int(existing["schedule_revision"]) + 1
        connection.execute(
            """
            UPDATE prediction_reminder_deadline_occurrences
            SET contest_id = NULL, status = 'cancelled', delivery_id = NULL,
                finalized_at = COALESCE(finalized_at, ?), updated_at = ?
            WHERE id = ?
            """,
            (now_value, now_value, int(existing["id"])),
        )
        connection.execute(
            """
            INSERT INTO prediction_reminder_deadline_occurrences (
                contest_id, original_contest_id, deadline_kind,
                observed_deadline_at_utc, due_at, schedule_revision,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
            """,
            (
                contest_id,
                contest_id,
                deadline_kind,
                deadline_value,
                due_at,
                next_revision,
                now_value,
                now_value,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE prediction_reminder_deadline_occurrences
            SET due_at = ?, status = 'scheduled', delivery_id = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (due_at, now_value, int(existing["id"])),
        )
    return 1


def _recover_abandoned_claims(connection: sqlite3.Connection, *, now: datetime) -> None:
    now_value = serialize_reminder_time(now)
    rows = connection.execute(
        """
        SELECT id, status, expires_at
        FROM prediction_reminder_deliveries
        WHERE claim_token IS NOT NULL
          AND claim_expires_at <= ?
          AND status IN ('preparing', 'sending')
        ORDER BY id
        """,
        (now_value,),
    ).fetchall()
    for row in rows:
        delivery_id = int(row["id"])
        part_rows = connection.execute(
            """
            SELECT status FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery_id,),
        ).fetchall()
        part_statuses = {str(part["status"]) for part in part_rows}
        if part_statuses.intersection({"sending", "unknown"}):
            connection.execute(
                """
                UPDATE prediction_reminder_delivery_parts
                SET status = 'unknown',
                    last_error = 'Worker stopped during Telegram delivery.'
                WHERE delivery_id = ? AND status = 'sending'
                """,
                (delivery_id,),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deliveries
                SET status = 'unknown', claim_token = NULL,
                    claim_expires_at = NULL, next_attempt_at = NULL,
                    last_error = 'Worker stopped during Telegram delivery.',
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_value, now_value, delivery_id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'unknown', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery_id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'unknown', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery_id),
            )
            continue
        if "sent" in part_statuses and part_statuses.issubset({"sent", "skipped"}):
            connection.execute(
                """
                UPDATE prediction_reminder_deliveries
                SET status = 'sent', claim_token = NULL,
                    claim_expires_at = NULL, next_attempt_at = NULL,
                    last_error = NULL, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_value, now_value, delivery_id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'sent', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery_id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'sent', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery_id),
            )
            continue
        delivery_expired = (
            _parse_reminder_time(row["expires_at"], field_name="expiry") <= now
        )
        if "sent" in part_statuses and delivery_expired:
            connection.execute(
                """
                UPDATE prediction_reminder_delivery_parts
                SET status = 'skipped',
                    last_error = 'Reminder deadline passed after another part was sent.'
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (delivery_id,),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deliveries
                SET status = 'partial', claim_token = NULL,
                    claim_expires_at = NULL, next_attempt_at = NULL,
                    last_error = 'Reminder deadline passed after a recorded part.',
                    finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_value, now_value, delivery_id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'sent', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery_id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'sent', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery_id),
            )
            continue
        error = (
            "Reminder claim expired after recorded parts; resuming unsent parts."
            if "sent" in part_statuses
            else "Reminder claim expired before Telegram send."
        )
        connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'retry', claim_token = NULL,
                claim_expires_at = NULL, next_attempt_at = ?,
                last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (now_value, error, now_value, delivery_id),
        )


def _expire_unclaimed_deliveries(
    connection: sqlite3.Connection, *, now: datetime
) -> None:
    now_value = serialize_reminder_time(now)
    rows = connection.execute(
        """
        SELECT id, source
        FROM prediction_reminder_deliveries
        WHERE status IN ('pending', 'retry')
          AND claim_token IS NULL
          AND expires_at <= ?
        ORDER BY id
        """,
        (now_value,),
    ).fetchall()
    for row in rows:
        delivery_id = int(row["id"])
        sent_exists = connection.execute(
            """
            SELECT 1 FROM prediction_reminder_delivery_parts
            WHERE delivery_id = ? AND status = 'sent'
            LIMIT 1
            """,
            (delivery_id,),
        ).fetchone()
        if sent_exists is not None:
            connection.execute(
                """
                UPDATE prediction_reminder_delivery_parts
                SET status = 'skipped',
                    last_error = 'Reminder deadline passed after another part was sent.'
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (delivery_id,),
            )
            update = connection.execute(
                """
                UPDATE prediction_reminder_deliveries
                SET status = 'partial', next_attempt_at = NULL,
                    last_error = 'Reminder deadline passed after a recorded part.',
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'retry')
                  AND claim_token IS NULL
                """,
                (now_value, now_value, delivery_id),
            )
            if update.rowcount == 1:
                connection.execute(
                    """
                    UPDATE prediction_reminder_occurrences
                    SET status = 'sent', finalized_at = ?, updated_at = ?
                    WHERE delivery_id = ? AND status = 'batched'
                    """,
                    (now_value, now_value, delivery_id),
                )
                connection.execute(
                    """
                    UPDATE prediction_reminder_deadline_occurrences
                    SET status = 'sent', finalized_at = ?, updated_at = ?
                    WHERE delivery_id = ? AND status = 'batched'
                    """,
                    (now_value, now_value, delivery_id),
                )
            continue
        connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'expired', next_attempt_at = NULL,
                last_error = 'Reminder deadline passed before delivery.',
                finished_at = ?, updated_at = ?
            WHERE id = ? AND status IN ('pending', 'retry')
              AND claim_token IS NULL
            """,
            (now_value, now_value, delivery_id),
        )
        if str(row["source"]) == "manual":
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'scheduled', delivery_id = NULL,
                    finalized_at = NULL, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, delivery_id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'scheduled', delivery_id = NULL,
                    finalized_at = NULL, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, delivery_id),
            )
        else:
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'expired', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery_id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'expired', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery_id),
            )


def _claim_delivery_row(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    claim_expires_at: str,
    now_value: str,
) -> ClaimedReminderDelivery | None:
    claim_token = uuid4().hex
    update = connection.execute(
        """
        UPDATE prediction_reminder_deliveries
        SET status = 'preparing', claim_token = ?, claim_expires_at = ?,
            updated_at = ?
        WHERE id = ? AND claim_token IS NULL
          AND status IN ('pending', 'retry')
        """,
        (claim_token, claim_expires_at, now_value, int(row["id"])),
    )
    if update.rowcount != 1:
        return None
    return ClaimedReminderDelivery(
        id=int(row["id"]),
        contest_id=int(row["contest_id"]),
        telegram_chat_id=int(row["telegram_chat_id"]),
        kind="manual" if str(row["source"]) == "manual" else "automatic",
        batch_starts_at_utc=(
            str(row["batch_starts_at_utc"])
            if row["batch_starts_at_utc"] is not None
            else None
        ),
        supplemental_sequence=int(row["supplemental_sequence"]),
        settings_revision=int(row["settings_revision"]),
        claim_token=claim_token,
    )


def _claim_is_current(
    connection: sqlite3.Connection,
    *,
    delivery: ClaimedReminderDelivery,
    now_value: str,
    required_status: str | None,
) -> bool:
    status_predicate = "" if required_status is None else "AND delivery.status = ?"
    parameters: list[object] = [
        delivery.id,
        delivery.claim_token,
        now_value,
        delivery.settings_revision,
    ]
    if required_status is not None:
        parameters.append(required_status)
    row = connection.execute(
        f"""
        SELECT 1
        FROM prediction_reminder_deliveries AS delivery
        JOIN contests ON contests.id = delivery.contest_id
        LEFT JOIN contest_prediction_reminder_settings AS settings
          ON settings.contest_id = delivery.contest_id
        WHERE delivery.id = ?
          AND delivery.claim_token = ?
          AND delivery.expires_at > ?
          AND contests.is_active = 1
          AND (
              delivery.source = 'manual'
              OR EXISTS (
                  SELECT 1
                  FROM prediction_reminder_delivery_parts AS sent_part
                  WHERE sent_part.delivery_id = delivery.id
                    AND sent_part.status = 'sent'
              )
              OR (settings.enabled = 1 AND settings.revision = ?)
          )
          {status_predicate}
          AND (
              delivery.source = 'manual'
              OR NOT EXISTS (
                  SELECT 1
                  FROM prediction_reminder_delivery_items AS item
                  LEFT JOIN prediction_reminder_occurrences AS occurrence
                    ON occurrence.id = item.occurrence_id
                  LEFT JOIN matches ON matches.id = occurrence.match_id
                  WHERE item.delivery_id = delivery.id
                    AND (
                        occurrence.id IS NULL
                        OR occurrence.status != 'batched'
                        OR occurrence.delivery_id != delivery.id
                        OR occurrence.schedule_revision !=
                           item.occurrence_schedule_revision
                        OR matches.id IS NULL
                        OR matches.status != 'scheduled'
                  )
              )
          )
          AND (
              delivery.source = 'manual'
              OR NOT EXISTS (
                  SELECT 1
                  FROM prediction_reminder_deadline_occurrences AS occurrence
                  WHERE occurrence.delivery_id = delivery.id
                    AND (
                        occurrence.status != 'batched'
                        OR occurrence.delivery_id != delivery.id
                    )
              )
          )
        """,
        parameters,
    ).fetchone()
    if row is None:
        return False
    if delivery.kind == "manual":
        return True
    starts = connection.execute(
        """
        SELECT matches.starts_at_utc AS current_starts_at_utc,
               item.starts_at_snapshot
        FROM prediction_reminder_delivery_items AS item
        JOIN prediction_reminder_occurrences AS occurrence
          ON occurrence.id = item.occurrence_id
        JOIN matches ON matches.id = occurrence.match_id
        WHERE item.delivery_id = ?
        """,
        (delivery.id,),
    ).fetchall()
    deadlines = connection.execute(
        """
        SELECT occurrence.observed_deadline_at_utc,
               CASE occurrence.deadline_kind
                 WHEN 'swiss' THEN swiss.enabled
                 ELSE contests.champion_prediction_enabled
               END AS current_deadline_enabled,
               CASE occurrence.deadline_kind
                 WHEN 'swiss' THEN swiss.deadline_at
                 ELSE contests.champion_prediction_deadline_at
               END AS current_deadline_at_utc
        FROM prediction_reminder_deadline_occurrences AS occurrence
        JOIN contests ON contests.id = occurrence.contest_id
        LEFT JOIN swiss_stage_prediction_settings AS swiss
          ON swiss.contest_id = occurrence.contest_id
        WHERE occurrence.delivery_id = ? AND occurrence.status = 'batched'
        """,
        (delivery.id,),
    ).fetchall()
    matches_current = all(
        _same_reminder_instant(
            item["current_starts_at_utc"], item["starts_at_snapshot"]
        )
        for item in starts
    )
    deadlines_current = all(
        bool(item["current_deadline_enabled"])
        and item["current_deadline_at_utc"] is not None
        and _same_reminder_instant(
            item["current_deadline_at_utc"],
            item["observed_deadline_at_utc"],
        )
        for item in deadlines
    )
    return bool(starts or deadlines) and matches_current and deadlines_current


def _settle_stale_claim(
    connection: sqlite3.Connection,
    *,
    delivery: ClaimedReminderDelivery,
    now_value: str,
) -> None:
    row = connection.execute(
        """
        SELECT expires_at, source FROM prediction_reminder_deliveries
        WHERE id = ? AND claim_token = ?
        """,
        (delivery.id, delivery.claim_token),
    ).fetchone()
    if row is None:
        return
    expired = _parse_reminder_time(
        row["expires_at"], field_name="expiry"
    ) <= _parse_reminder_time(now_value, field_name="current time")
    part_rows = connection.execute(
        """
        SELECT status FROM prediction_reminder_delivery_parts
        WHERE delivery_id = ? ORDER BY part_number
        """,
        (delivery.id,),
    ).fetchall()
    part_statuses = {str(part["status"]) for part in part_rows}
    if part_statuses.intersection({"sending", "unknown"}):
        update = connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = 'unknown', claim_token = NULL,
                claim_expires_at = NULL, next_attempt_at = NULL,
                last_error = 'Reminder state changed during delivery.',
                finished_at = ?, updated_at = ?
            WHERE id = ? AND claim_token = ?
            """,
            (now_value, now_value, delivery.id, delivery.claim_token),
        )
        if update.rowcount != 1:
            return
        connection.execute(
            """
            UPDATE prediction_reminder_delivery_parts
            SET status = 'unknown',
                last_error = 'Reminder state changed during Telegram delivery.'
            WHERE delivery_id = ? AND status = 'sending'
            """,
            (delivery.id,),
        )
        connection.execute(
            """
            UPDATE prediction_reminder_occurrences
            SET status = 'unknown', finalized_at = ?, updated_at = ?
            WHERE delivery_id = ? AND status = 'batched'
            """,
            (now_value, now_value, delivery.id),
        )
        connection.execute(
            """
            UPDATE prediction_reminder_deadline_occurrences
            SET status = 'unknown', finalized_at = ?, updated_at = ?
            WHERE delivery_id = ? AND status = 'batched'
            """,
            (now_value, now_value, delivery.id),
        )
        return
    if "sent" in part_statuses:
        all_sent = part_statuses == {"sent"}
        if not all_sent:
            connection.execute(
                """
                UPDATE prediction_reminder_delivery_parts
                SET status = 'skipped',
                    last_error = 'Reminder became stale after another part was sent.'
                WHERE delivery_id = ? AND status = 'pending'
                """,
                (delivery.id,),
            )
        delivery_status = "sent" if all_sent else "partial"
        error = None if all_sent else "Reminder became stale after a recorded part."
        update = connection.execute(
            """
            UPDATE prediction_reminder_deliveries
            SET status = ?, claim_token = NULL, claim_expires_at = NULL,
                next_attempt_at = NULL, last_error = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND claim_token = ?
            """,
            (
                delivery_status,
                error,
                now_value,
                now_value,
                delivery.id,
                delivery.claim_token,
            ),
        )
        if update.rowcount == 1:
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'sent', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery.id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'sent', finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, now_value, delivery.id),
            )
        return
    status = "expired" if expired else "cancelled"
    update = connection.execute(
        """
        UPDATE prediction_reminder_deliveries
        SET status = ?, claim_token = NULL, claim_expires_at = NULL,
            next_attempt_at = NULL,
            last_error = 'Reminder state changed before delivery.',
            finished_at = ?, updated_at = ?
        WHERE id = ? AND claim_token = ?
        """,
        (status, now_value, now_value, delivery.id, delivery.claim_token),
    )
    if update.rowcount != 1:
        return
    if str(row["source"]) == "manual":
        connection.execute(
            """
            UPDATE prediction_reminder_occurrences
            SET status = 'scheduled', delivery_id = NULL,
                finalized_at = NULL, updated_at = ?
            WHERE delivery_id = ? AND status = 'batched'
            """,
            (now_value, delivery.id),
        )
        connection.execute(
            """
            UPDATE prediction_reminder_deadline_occurrences
            SET status = 'scheduled', delivery_id = NULL,
                finalized_at = NULL, updated_at = ?
            WHERE delivery_id = ? AND status = 'batched'
            """,
            (now_value, delivery.id),
        )
    else:
        connection.execute(
            """
            UPDATE prediction_reminder_occurrences
            SET status = CASE
                    WHEN ? = 'expired' THEN 'expired' ELSE 'scheduled' END,
                delivery_id = NULL,
                finalized_at = CASE WHEN ? = 'expired' THEN ? ELSE NULL END,
                updated_at = ?
            WHERE delivery_id = ? AND status = 'batched'
            """,
            (status, status, now_value, now_value, delivery.id),
        )
        connection.execute(
            """
            UPDATE prediction_reminder_deadline_occurrences
            SET status = CASE
                    WHEN ? = 'expired' THEN 'expired' ELSE 'scheduled' END,
                delivery_id = NULL,
                finalized_at = CASE WHEN ? = 'expired' THEN ? ELSE NULL END,
                updated_at = ?
            WHERE delivery_id = ? AND status = 'batched'
            """,
            (status, status, now_value, now_value, delivery.id),
        )


def _reset_delivery_if_safe(
    connection: sqlite3.Connection,
    *,
    delivery_id: int,
    now_value: str,
    reason: str,
) -> bool:
    unsafe = connection.execute(
        """
        SELECT 1 FROM prediction_reminder_delivery_parts
        WHERE delivery_id = ? AND status IN ('sending', 'sent', 'unknown')
        LIMIT 1
        """,
        (delivery_id,),
    ).fetchone()
    if unsafe is not None:
        return False
    update = connection.execute(
        """
        UPDATE prediction_reminder_deliveries
        SET status = 'cancelled', claim_token = NULL, claim_expires_at = NULL,
            next_attempt_at = NULL, last_error = ?, finished_at = ?, updated_at = ?
        WHERE id = ?
          AND status IN ('pending', 'preparing', 'sending', 'retry')
        """,
        (reason[:2000], now_value, now_value, delivery_id),
    )
    if update.rowcount != 1:
        return False
    connection.execute(
        """
        UPDATE prediction_reminder_occurrences
        SET status = 'scheduled', delivery_id = NULL, updated_at = ?
        WHERE delivery_id = ? AND status = 'batched'
        """,
        (now_value, delivery_id),
    )
    connection.execute(
        """
        UPDATE prediction_reminder_deadline_occurrences
        SET status = 'scheduled', delivery_id = NULL, updated_at = ?
        WHERE delivery_id = ? AND status = 'batched'
        """,
        (now_value, delivery_id),
    )
    return True


def _finish_terminal_in_connection(
    connection: sqlite3.Connection,
    *,
    delivery: ClaimedReminderDelivery,
    error: str,
    now_value: str,
    attempt_count: int | None = None,
) -> bool:
    delivery_row = connection.execute(
        """
        SELECT source FROM prediction_reminder_deliveries
        WHERE id = ? AND claim_token = ?
        """,
        (delivery.id, delivery.claim_token),
    ).fetchone()
    if delivery_row is None:
        return False
    sent_exists = connection.execute(
        """
        SELECT 1 FROM prediction_reminder_delivery_parts
        WHERE delivery_id = ? AND status = 'sent' LIMIT 1
        """,
        (delivery.id,),
    ).fetchone()
    delivery_status = "partial" if sent_exists is not None else "terminal_failed"
    update = connection.execute(
        """
        UPDATE prediction_reminder_deliveries
        SET status = ?,
            attempt_count = COALESCE(?, attempt_count),
            claim_token = NULL, claim_expires_at = NULL,
            next_attempt_at = NULL, last_error = ?,
            finished_at = ?, updated_at = ?
        WHERE id = ? AND claim_token = ?
          AND status IN ('preparing', 'sending')
        """,
        (
            delivery_status,
            attempt_count,
            error[:2000],
            now_value,
            now_value,
            delivery.id,
            delivery.claim_token,
        ),
    )
    if update.rowcount == 1:
        if str(delivery_row["source"]) == "manual" and sent_exists is None:
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = 'scheduled', delivery_id = NULL,
                    finalized_at = NULL, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, delivery.id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = 'scheduled', delivery_id = NULL,
                    finalized_at = NULL, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (now_value, delivery.id),
            )
        else:
            occurrence_status = "sent" if sent_exists is not None else "terminal_failed"
            connection.execute(
                """
                UPDATE prediction_reminder_occurrences
                SET status = ?, finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (occurrence_status, now_value, now_value, delivery.id),
            )
            connection.execute(
                """
                UPDATE prediction_reminder_deadline_occurrences
                SET status = ?, finalized_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'batched'
                """,
                (occurrence_status, now_value, now_value, delivery.id),
            )
    return update.rowcount == 1


def _load_delivery_items(
    connection: sqlite3.Connection, *, delivery_id: int
) -> tuple[ReminderDeliveryItem, ...]:
    rows = connection.execute(
        """
        SELECT match_id_snapshot, starts_at_snapshot,
               home_team_name, away_team_name
        FROM prediction_reminder_delivery_items
        WHERE delivery_id = ?
        ORDER BY item_order
        """,
        (delivery_id,),
    ).fetchall()
    return tuple(
        ReminderDeliveryItem(
            match_id=int(row["match_id_snapshot"]),
            starts_at_utc=str(row["starts_at_snapshot"]),
            home_team_name=str(row["home_team_name"]),
            away_team_name=str(row["away_team_name"]),
        )
        for row in rows
    )


def _load_delivery_deadlines(
    connection: sqlite3.Connection, *, delivery_id: int
) -> tuple[ReminderDeadlineItem, ...]:
    rows = connection.execute(
        """
        SELECT deadline_kind, observed_deadline_at_utc
        FROM prediction_reminder_deadline_occurrences
        WHERE delivery_id = ? AND status = 'batched'
        ORDER BY deadline_kind, id
        """,
        (delivery_id,),
    ).fetchall()
    return tuple(
        ReminderDeadlineItem(
            kind=str(row["deadline_kind"]),  # type: ignore[arg-type]
            deadline_at_utc=str(row["observed_deadline_at_utc"]),
        )
        for row in rows
    )


def _eligible_recipients(
    connection: sqlite3.Connection, *, delivery_id: int, contest_id: int
) -> tuple[ReminderRecipient, ...]:
    rows = connection.execute(
        """
        WITH contest_participants AS (
            SELECT prediction.user_id
            FROM match_predictions AS prediction
            JOIN matches ON matches.id = prediction.match_id
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?

            UNION

            SELECT prediction.user_id
            FROM tie_predictions AS prediction
            JOIN ties ON ties.id = prediction.tie_id
            JOIN stages ON stages.id = ties.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?

            UNION

            SELECT user_id FROM champion_predictions WHERE contest_id = ?

            UNION

            SELECT user_id FROM swiss_stage_predictions WHERE contest_id = ?
        )
        SELECT users.id, users.telegram_user_id, users.username,
               users.first_name, users.last_name
        FROM contest_participants
        JOIN users ON users.id = contest_participants.user_id
        JOIN contests ON contests.id = ?
        JOIN chat_user_prediction_reminder_preferences AS preference
          ON preference.chat_id = contests.chat_id
         AND preference.user_id = users.id
         AND preference.mention_in_prediction_reminders = 1
        WHERE EXISTS (
                SELECT 1
                FROM prediction_reminder_delivery_items AS item
                JOIN prediction_reminder_occurrences AS occurrence
                  ON occurrence.id = item.occurrence_id
                JOIN matches ON matches.id = occurrence.match_id
                LEFT JOIN ties ON ties.id = matches.tie_id
                WHERE item.delivery_id = ?
                  AND (
                      NOT EXISTS (
                          SELECT 1 FROM match_predictions
                          WHERE match_predictions.match_id = matches.id
                            AND match_predictions.user_id = users.id
                      )
                      OR (
                          matches.tie_id IS NOT NULL
                          AND (
                              COALESCE(ties.is_two_legged, 0) = 0
                              OR (
                                  ties.is_two_legged = 1
                                  AND matches.leg_number = 1
                              )
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM tie_predictions
                              WHERE tie_predictions.tie_id = matches.tie_id
                                AND tie_predictions.user_id = users.id
                          )
                      )
                  )
            )
           OR EXISTS (
                SELECT 1
                FROM prediction_reminder_deadline_occurrences AS deadline
                WHERE deadline.delivery_id = ? AND deadline.status = 'batched'
                  AND (
                      (
                          deadline.deadline_kind = 'champion'
                          AND NOT EXISTS (
                              SELECT 1 FROM champion_predictions
                              WHERE champion_predictions.contest_id = ?
                                AND champion_predictions.user_id = users.id
                          )
                      )
                      OR (
                          deadline.deadline_kind = 'swiss'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM swiss_stage_predictions AS prediction
                              JOIN swiss_stage_prediction_settings AS settings
                                ON settings.contest_id = prediction.contest_id
                              WHERE prediction.contest_id = ?
                                AND prediction.user_id = users.id
                                AND (
                                    SELECT COUNT(*)
                                    FROM swiss_stage_prediction_selections AS selection
                                    WHERE selection.prediction_id = prediction.id
                                      AND selection.category = 'direct'
                                ) = settings.direct_qualifier_count
                                AND (
                                    SELECT COUNT(*)
                                    FROM swiss_stage_prediction_selections AS selection
                                    WHERE selection.prediction_id = prediction.id
                                      AND selection.category = 'elimination'
                                ) = settings.elimination_qualifier_count
                          )
                      )
                  )
            )
        ORDER BY users.telegram_user_id, users.id
        """,
        (
            contest_id,
            contest_id,
            contest_id,
            contest_id,
            contest_id,
            delivery_id,
            delivery_id,
            contest_id,
            contest_id,
        ),
    ).fetchall()
    return tuple(
        ReminderRecipient(
            user_id=int(row["id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            username=str(row["username"]) if row["username"] is not None else None,
            first_name=str(row["first_name"]),
            last_name=(str(row["last_name"]) if row["last_name"] is not None else None),
        )
        for row in rows
    )


def _opted_in_participants(
    connection: sqlite3.Connection, *, contest_id: int
) -> tuple[ReminderRecipient, ...]:
    rows = connection.execute(
        """
        WITH contest_participants AS (
            SELECT prediction.user_id
            FROM match_predictions AS prediction
            JOIN matches ON matches.id = prediction.match_id
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?

            UNION

            SELECT prediction.user_id
            FROM tie_predictions AS prediction
            JOIN ties ON ties.id = prediction.tie_id
            JOIN stages ON stages.id = ties.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            WHERE competitions.contest_id = ?

            UNION

            SELECT user_id FROM champion_predictions WHERE contest_id = ?

            UNION

            SELECT user_id FROM swiss_stage_predictions WHERE contest_id = ?
        )
        SELECT users.id, users.telegram_user_id, users.username,
               users.first_name, users.last_name
        FROM contest_participants
        JOIN users ON users.id = contest_participants.user_id
        JOIN contests ON contests.id = ?
        JOIN chat_user_prediction_reminder_preferences AS preference
          ON preference.chat_id = contests.chat_id
         AND preference.user_id = users.id
         AND preference.mention_in_prediction_reminders = 1
        ORDER BY users.telegram_user_id, users.id
        """,
        (contest_id, contest_id, contest_id, contest_id, contest_id),
    ).fetchall()
    return tuple(
        ReminderRecipient(
            user_id=int(row["id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            username=str(row["username"]) if row["username"] is not None else None,
            first_name=str(row["first_name"]),
            last_name=(str(row["last_name"]) if row["last_name"] is not None else None),
        )
        for row in rows
    )


def _eligible_manual_recipients(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    now: datetime,
) -> tuple[ReminderRecipient, ...]:
    candidates = _opted_in_participants(connection, contest_id=contest_id)
    if not candidates:
        return ()
    user_ids = tuple(recipient.user_id for recipient in candidates)
    user_marks = ",".join("?" for _ in user_ids)
    match_rows = connection.execute(
        """
        SELECT matches.id, matches.starts_at_utc, matches.tie_id,
               matches.leg_number,
               COALESCE(ties.is_two_legged, 0) AS is_two_legged
        FROM matches
        JOIN stages ON stages.id = matches.stage_id
        JOIN competitions ON competitions.id = stages.competition_id
        LEFT JOIN ties ON ties.id = matches.tie_id
        WHERE competitions.contest_id = ? AND matches.status = 'scheduled'
        ORDER BY matches.id
        """,
        (contest_id,),
    ).fetchall()
    open_matches = tuple(
        row
        for row in match_rows
        if _parse_reminder_time(
            row["starts_at_utc"], field_name=f"match {row['id']} start"
        )
        > now
    )
    score_predictions: set[tuple[int, int]] = set()
    match_ids = tuple(int(row["id"]) for row in open_matches)
    if match_ids:
        match_marks = ",".join("?" for _ in match_ids)
        score_predictions = {
            (int(row["match_id"]), int(row["user_id"]))
            for row in connection.execute(
                f"""
                SELECT match_id, user_id FROM match_predictions
                WHERE match_id IN ({match_marks})
                  AND user_id IN ({user_marks})
                """,
                (*match_ids, *user_ids),
            ).fetchall()
        }
    tie_ids = tuple(
        dict.fromkeys(
            int(row["tie_id"]) for row in open_matches if row["tie_id"] is not None
        )
    )
    tie_predictions: set[tuple[int, int]] = set()
    if tie_ids:
        tie_marks = ",".join("?" for _ in tie_ids)
        tie_predictions = {
            (int(row["tie_id"]), int(row["user_id"]))
            for row in connection.execute(
                f"""
                SELECT tie_id, user_id FROM tie_predictions
                WHERE tie_id IN ({tie_marks})
                  AND user_id IN ({user_marks})
                """,
                (*tie_ids, *user_ids),
            ).fetchall()
        }
    settings = connection.execute(
        """
        SELECT contests.champion_prediction_enabled,
               contests.champion_prediction_deadline_at,
               swiss.enabled AS swiss_enabled,
               swiss.deadline_at AS swiss_deadline_at,
               swiss.direct_qualifier_count,
               swiss.elimination_qualifier_count
        FROM contests
        LEFT JOIN swiss_stage_prediction_settings AS swiss
          ON swiss.contest_id = contests.id
        WHERE contests.id = ?
        """,
        (contest_id,),
    ).fetchone()
    if settings is None:
        return ()
    champion_open = bool(settings["champion_prediction_enabled"]) and (
        settings["champion_prediction_deadline_at"] is not None
        and _parse_reminder_time(
            settings["champion_prediction_deadline_at"],
            field_name="champion prediction deadline",
        )
        > now
    )
    swiss_open = bool(settings["swiss_enabled"]) and (
        settings["swiss_deadline_at"] is not None
        and _parse_reminder_time(
            settings["swiss_deadline_at"],
            field_name="Swiss prediction deadline",
        )
        > now
    )
    champion_users: set[int] = set()
    if champion_open:
        champion_users = {
            int(row["user_id"])
            for row in connection.execute(
                f"""
                SELECT user_id FROM champion_predictions
                WHERE contest_id = ? AND user_id IN ({user_marks})
                """,
                (contest_id, *user_ids),
            ).fetchall()
        }
    complete_swiss_users: set[int] = set()
    if swiss_open:
        direct_count = int(settings["direct_qualifier_count"])
        elimination_count = int(settings["elimination_qualifier_count"])
        complete_swiss_users = {
            int(row["user_id"])
            for row in connection.execute(
                f"""
                SELECT predictions.user_id,
                  SUM(CASE WHEN selections.category = 'direct' THEN 1 ELSE 0 END)
                    AS direct_count,
                  SUM(CASE WHEN selections.category = 'elimination' THEN 1 ELSE 0 END)
                    AS elimination_count
                FROM swiss_stage_predictions AS predictions
                LEFT JOIN swiss_stage_prediction_selections AS selections
                  ON selections.prediction_id = predictions.id
                 AND selections.contest_id = predictions.contest_id
                WHERE predictions.contest_id = ?
                  AND predictions.user_id IN ({user_marks})
                GROUP BY predictions.user_id
                """,
                (contest_id, *user_ids),
            ).fetchall()
            if int(row["direct_count"] or 0) == direct_count
            and int(row["elimination_count"] or 0) == elimination_count
        }

    eligible: list[ReminderRecipient] = []
    for recipient in candidates:
        user_id = recipient.user_id
        missing_match = any(
            (int(match["id"]), user_id) not in score_predictions
            or (
                match["tie_id"] is not None
                and (
                    not bool(match["is_two_legged"])
                    or int(match["leg_number"] or 0) == 1
                )
                and (int(match["tie_id"]), user_id) not in tie_predictions
            )
            for match in open_matches
        )
        missing_long_term = (champion_open and user_id not in champion_users) or (
            swiss_open and user_id not in complete_swiss_users
        )
        if missing_match or missing_long_term:
            eligible.append(recipient)
    return tuple(eligible)


def _load_delivery_recipients(
    connection: sqlite3.Connection, *, delivery_id: int
) -> tuple[ReminderRecipient, ...]:
    rows = connection.execute(
        """
        SELECT user_id, telegram_user_id, username, first_name, last_name
        FROM prediction_reminder_delivery_recipients
        WHERE delivery_id = ? AND status != 'suppressed'
        ORDER BY recipient_order
        """,
        (delivery_id,),
    ).fetchall()
    return tuple(
        ReminderRecipient(
            user_id=int(row["user_id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            username=str(row["username"]) if row["username"] is not None else None,
            first_name=str(row["first_name"]),
            last_name=(str(row["last_name"]) if row["last_name"] is not None else None),
        )
        for row in rows
        if row["user_id"] is not None
    )


def _stored_part_from_row(row: sqlite3.Row) -> StoredReminderPart:
    return StoredReminderPart(
        part_number=int(row["part_number"]),
        html=str(row["html"]),
        content_hash=str(row["content_hash"]),
        has_launch_button=bool(row["has_launch_button"]),
        status=str(row["status"]),
        telegram_message_id=(
            int(row["telegram_message_id"])
            if row["telegram_message_id"] is not None
            else None
        ),
    )


def _settings_from_row(*, contest_id: int, row: sqlite3.Row) -> ReminderSettings:
    return ReminderSettings(
        contest_id=contest_id,
        enabled=bool(row["enabled"]),
        lead_time_minutes=int(row["lead_time_minutes"]),
        revision=int(row["revision"]),
    )


def _preference_from_row(
    *, chat_id: int, user_id: int, row: sqlite3.Row
) -> ReminderPreference:
    return ReminderPreference(
        chat_id=chat_id,
        user_id=user_id,
        mention_in_prediction_reminders=bool(row["mention_in_prediction_reminders"]),
        revision=int(row["revision"]),
    )


def _require_chat_and_user(
    connection: sqlite3.Connection, *, chat_id: int, user_id: int
) -> None:
    if (
        connection.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone()
        is None
    ):
        raise PredictionReminderStoreError("Chat was not found.")
    if (
        connection.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
        is None
    ):
        raise PredictionReminderStoreError("User was not found.")


def _require_bool(value: object, *, field_name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean.")


def _validate_lead_time(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("lead_time_minutes must be an integer.")
    if not MIN_REMINDER_LEAD_TIME_MINUTES <= value <= MAX_REMINDER_LEAD_TIME_MINUTES:
        raise ValueError(
            "lead_time_minutes must be between "
            f"{MIN_REMINDER_LEAD_TIME_MINUTES} and "
            f"{MAX_REMINDER_LEAD_TIME_MINUTES}."
        )


def _normalize_manual_idempotency_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("idempotency_key must be a string.")
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("idempotency_key must contain between 1 and 200 characters.")
    return normalized


def _parse_reminder_time(value: object, *, field_name: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PredictionReminderStoreError(
            f"{field_name.capitalize()} does not include a timezone."
        )
    return parsed.astimezone(timezone.utc)


def _same_reminder_instant(left: object, right: object) -> bool:
    return _parse_reminder_time(left, field_name="timestamp") == _parse_reminder_time(
        right, field_name="timestamp"
    )


def _occurrence_due_matches_current_lead_time(
    *, due_at: object, deadline_at: object, lead_time_minutes: int
) -> bool:
    expected_due_at = _parse_reminder_time(
        deadline_at,
        field_name="prediction deadline",
    ) - timedelta(minutes=lead_time_minutes)
    return _same_reminder_instant(due_at, expected_due_at)


def _remove_ineligible_mentions(
    html: str, *, allowed_telegram_user_ids: set[int]
) -> str:
    def replace_block(block_match: re.Match[str]) -> str:
        anchors = [
            anchor.group(0)
            for anchor in _MENTION_ANCHOR_PATTERN.finditer(block_match.group("body"))
            if int(anchor.group("telegram_user_id")) in allowed_telegram_user_ids
        ]
        if not anchors:
            return ""
        return "<p><b>Ждём прогнозы от:</b><br>" + ", ".join(anchors) + "</p>"

    return _MENTION_BLOCK_PATTERN.sub(replace_block, html)

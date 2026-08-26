from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from app.database import database_connection


PublicationType = Literal[
    "match_result",
    "champion_predictions",
    "champion_result",
    "swiss_predictions",
    "swiss_result",
    "leaderboard_snapshot",
    "contest_completed",
]
PublicationAction = Literal["publish", "withdraw"]
PublicationClaimStatus = Literal["current", "stale", "lost"]

PUBLICATION_CLAIM_SECONDS = 90
PUBLICATION_MAX_ATTEMPTS = 96
PUBLICATION_MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True, slots=True)
class ClaimedPublication:
    id: int
    contest_id: int
    publication_type: PublicationType
    entity_id: int
    desired_revision: int
    desired_action: PublicationAction
    claim_token: str


@dataclass(frozen=True, slots=True)
class PublicationClaimState:
    status: PublicationClaimStatus
    desired_revision: int | None = None
    desired_action: PublicationAction | None = None
    entity_id: int | None = None
    publication_enabled: bool | None = None


class StalePublicationRevision(RuntimeError):
    pass


def serialize_service_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Service timestamps must include a timezone.")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def resolve_service_time(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Service timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


def create_publication_if_enabled(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    publication_type: PublicationType,
    entity_id: int,
    event_id: int,
    now_utc: datetime | None = None,
    desired_action: PublicationAction = "publish",
    reconcile_at: str | None = None,
) -> bool:
    enabled_row = connection.execute(
        """
        SELECT match_prediction_publication_enabled
        FROM contests
        WHERE id = ?
        """,
        (contest_id,),
    ).fetchone()
    if enabled_row is None:
        raise RuntimeError("Contest was not found while creating a publication.")
    if not bool(enabled_row["match_prediction_publication_enabled"]):
        return False

    now_value = serialize_service_time(resolve_service_time(now_utc))
    insertion = connection.execute(
        """
        INSERT OR IGNORE INTO contest_publications (
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
            next_attempt_at,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, 1, 0, ?, 'pending', ?, ?, ?, ?, ?, ?)
        """,
        (
            contest_id,
            publication_type,
            entity_id,
            desired_action,
            event_id,
            event_id,
            reconcile_at,
            now_value,
            now_value,
            now_value,
        ),
    )
    return insertion.rowcount == 1


def create_manual_leaderboard_publication(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    snapshot_id: int,
    event_id: int,
    now_utc: datetime | None = None,
) -> None:
    now_value = serialize_service_time(resolve_service_time(now_utc))
    insertion = connection.execute(
        """
        INSERT INTO contest_publications (
            contest_id,
            publication_type,
            entity_id,
            desired_revision,
            settled_revision,
            desired_action,
            delivery_status,
            first_event_id,
            latest_event_id,
            next_attempt_at,
            created_at,
            updated_at
        )
        VALUES (?, 'leaderboard_snapshot', ?, 1, 0, 'publish', 'pending',
                ?, ?, ?, ?, ?)
        """,
        (
            contest_id,
            snapshot_id,
            event_id,
            event_id,
            now_value,
            now_value,
            now_value,
        ),
    )
    if insertion.rowcount != 1:
        raise RuntimeError("Could not queue the leaderboard publication.")


def revise_existing_publication(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    publication_type: PublicationType,
    entity_id: int,
    event_id: int,
    desired_action: PublicationAction,
    reconcile_at: str | None = None,
    now_utc: datetime | None = None,
) -> bool:
    now_value = serialize_service_time(resolve_service_time(now_utc))
    update = connection.execute(
        """
        UPDATE contest_publications
        SET
            desired_revision = desired_revision + 1,
            desired_action = ?,
            delivery_status = 'pending',
            latest_event_id = ?,
            reconcile_at = ?,
            attempt_count = 0,
            next_attempt_at = ?,
            last_error = NULL,
            updated_at = ?
        WHERE contest_id = ?
          AND publication_type = ?
          AND entity_id = ?
          AND EXISTS (
              SELECT 1 FROM contests
              WHERE id = ?
                AND match_prediction_publication_enabled = 1
          )
        """,
        (
            desired_action,
            event_id,
            reconcile_at,
            now_value,
            now_value,
            contest_id,
            publication_type,
            entity_id,
            contest_id,
        ),
    )
    return update.rowcount == 1


def create_or_revise_match_result_publication(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    match_id: int,
    event_id: int,
    was_created: bool,
    now_utc: datetime | None = None,
) -> None:
    if was_created:
        create_publication_if_enabled(
            connection,
            contest_id=contest_id,
            publication_type="match_result",
            entity_id=match_id,
            event_id=event_id,
            now_utc=now_utc,
        )
        return

    revise_existing_publication(
        connection,
        contest_id=contest_id,
        publication_type="match_result",
        entity_id=match_id,
        event_id=event_id,
        desired_action="publish",
        now_utc=now_utc,
    )


def create_or_revise_champion_publication(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    event_id: int,
    was_created: bool,
    now_utc: datetime | None = None,
) -> None:
    now = resolve_service_time(now_utc)
    desired_action, reconcile_at = _champion_desired_state(
        connection,
        contest_id=contest_id,
        now_utc=now,
    )
    if was_created:
        created = create_publication_if_enabled(
            connection,
            contest_id=contest_id,
            publication_type="champion_result",
            entity_id=contest_id,
            event_id=event_id,
            desired_action=desired_action,
            reconcile_at=reconcile_at,
            now_utc=now,
        )
        if created:
            return

    revise_existing_publication(
        connection,
        contest_id=contest_id,
        publication_type="champion_result",
        entity_id=contest_id,
        event_id=event_id,
        desired_action=desired_action,
        reconcile_at=reconcile_at,
        now_utc=now,
    )


def create_or_revise_champion_predictions_publication(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    event_id: int,
    now_utc: datetime | None = None,
) -> None:
    now = resolve_service_time(now_utc)
    desired_action, reconcile_at = _champion_predictions_desired_state(
        connection,
        contest_id=contest_id,
        now_utc=now,
    )
    existing = connection.execute(
        """
        SELECT desired_action
        FROM contest_publications
        WHERE contest_id = ?
          AND publication_type = 'champion_predictions'
          AND entity_id = ?
        """,
        (contest_id, contest_id),
    ).fetchone()
    if existing is None:
        if desired_action == "withdraw" and reconcile_at is None:
            return
        create_publication_if_enabled(
            connection,
            contest_id=contest_id,
            publication_type="champion_predictions",
            entity_id=contest_id,
            event_id=event_id,
            desired_action=desired_action,
            reconcile_at=reconcile_at,
            now_utc=now,
        )
        return

    needs_revision = (
        str(existing["desired_action"]) != desired_action or desired_action == "publish"
    )
    if needs_revision:
        revise_existing_publication(
            connection,
            contest_id=contest_id,
            publication_type="champion_predictions",
            entity_id=contest_id,
            event_id=event_id,
            desired_action=desired_action,
            reconcile_at=reconcile_at,
            now_utc=now,
        )
        return

    connection.execute(
        """
        UPDATE contest_publications
        SET
            latest_event_id = ?,
            reconcile_at = ?,
            updated_at = ?
        WHERE contest_id = ?
          AND publication_type = 'champion_predictions'
          AND entity_id = ?
          AND EXISTS (
              SELECT 1 FROM contests
              WHERE id = ?
                AND match_prediction_publication_enabled = 1
          )
        """,
        (
            event_id,
            reconcile_at,
            serialize_service_time(now),
            contest_id,
            contest_id,
            contest_id,
        ),
    )


def create_or_revise_swiss_predictions_publication(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    event_id: int,
    now_utc: datetime | None = None,
) -> None:
    now = resolve_service_time(now_utc)
    desired_action, reconcile_at = _swiss_predictions_desired_state(
        connection,
        contest_id=contest_id,
        now_utc=now,
    )
    existing = connection.execute(
        """
        SELECT desired_action
        FROM contest_publications
        WHERE contest_id = ?
          AND publication_type = 'swiss_predictions'
          AND entity_id = ?
        """,
        (contest_id, contest_id),
    ).fetchone()
    if existing is None:
        if desired_action == "withdraw" and reconcile_at is None:
            return
        create_publication_if_enabled(
            connection,
            contest_id=contest_id,
            publication_type="swiss_predictions",
            entity_id=contest_id,
            event_id=event_id,
            desired_action=desired_action,
            reconcile_at=reconcile_at,
            now_utc=now,
        )
        return

    needs_revision = (
        str(existing["desired_action"]) != desired_action or desired_action == "publish"
    )
    if needs_revision:
        revise_existing_publication(
            connection,
            contest_id=contest_id,
            publication_type="swiss_predictions",
            entity_id=contest_id,
            event_id=event_id,
            desired_action=desired_action,
            reconcile_at=reconcile_at,
            now_utc=now,
        )
        return

    connection.execute(
        """
        UPDATE contest_publications
        SET
            latest_event_id = ?,
            reconcile_at = ?,
            updated_at = ?
        WHERE contest_id = ?
          AND publication_type = 'swiss_predictions'
          AND entity_id = ?
          AND EXISTS (
              SELECT 1 FROM contests
              WHERE id = ?
                AND match_prediction_publication_enabled = 1
          )
        """,
        (
            event_id,
            reconcile_at,
            serialize_service_time(now),
            contest_id,
            contest_id,
            contest_id,
        ),
    )


def create_or_revise_swiss_result_publication(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    event_id: int,
    was_created: bool,
    now_utc: datetime | None = None,
) -> None:
    if was_created:
        create_publication_if_enabled(
            connection,
            contest_id=contest_id,
            publication_type="swiss_result",
            entity_id=contest_id,
            event_id=event_id,
            now_utc=now_utc,
        )
        return
    revise_existing_publication(
        connection,
        contest_id=contest_id,
        publication_type="swiss_result",
        entity_id=contest_id,
        event_id=event_id,
        desired_action="publish",
        now_utc=now_utc,
    )


def revise_champion_publication_for_related_change(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    event_id: int,
    now_utc: datetime | None = None,
) -> None:
    now = resolve_service_time(now_utc)
    desired_action, reconcile_at = _champion_desired_state(
        connection,
        contest_id=contest_id,
        now_utc=now,
    )
    revise_existing_publication(
        connection,
        contest_id=contest_id,
        publication_type="champion_result",
        entity_id=contest_id,
        event_id=event_id,
        desired_action=desired_action,
        reconcile_at=reconcile_at,
        now_utc=now,
    )


def transition_contest_publications_for_master_switch(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    enabled: bool,
    event_id: int,
    now_utc: datetime | None = None,
) -> None:
    if enabled:
        now = resolve_service_time(now_utc)
        for desired_state, create_scheduled in (
            (
                _champion_predictions_desired_state,
                create_or_revise_champion_predictions_publication,
            ),
            (
                _swiss_predictions_desired_state,
                create_or_revise_swiss_predictions_publication,
            ),
        ):
            desired_action, reconcile_at = desired_state(
                connection,
                contest_id=contest_id,
                now_utc=now,
            )
            if desired_action == "withdraw" and reconcile_at is not None:
                create_scheduled(
                    connection,
                    contest_id=contest_id,
                    event_id=event_id,
                    now_utc=now,
                )
        return
    now_value = serialize_service_time(resolve_service_time(now_utc))
    connection.execute(
        """
        UPDATE contest_publications
        SET
            desired_revision = desired_revision + 1,
            settled_revision = desired_revision + 1,
            delivery_status = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM contest_publication_messages AS message
                    WHERE message.publication_id = contest_publications.id
                      AND message.part_status IN ('active', 'terminal_failed')
                ) THEN 'published'
                ELSE 'withdrawn'
            END,
            latest_event_id = ?,
            reconcile_at = NULL,
            attempt_count = 0,
            next_attempt_at = NULL,
            last_error = NULL,
            updated_at = ?
        WHERE contest_id = ?
          AND publication_type <> 'leaderboard_snapshot'
        """,
        (event_id, now_value, contest_id),
    )


def create_contest_completed_publication(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    event_id: int,
    now_utc: datetime | None = None,
) -> None:
    create_publication_if_enabled(
        connection,
        contest_id=contest_id,
        publication_type="contest_completed",
        entity_id=contest_id,
        event_id=event_id,
        now_utc=now_utc,
    )


def handle_match_publication_deletion(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    match_id: int,
    event_id: int,
    now_utc: datetime | None = None,
) -> None:
    publication = connection.execute(
        """
        SELECT id, claim_token
        FROM contest_publications
        WHERE contest_id = ?
          AND publication_type = 'match_result'
          AND entity_id = ?
        """,
        (contest_id, match_id),
    ).fetchone()
    if publication is None:
        return

    message_exists = connection.execute(
        """
        SELECT 1
        FROM contest_publication_messages
        WHERE publication_id = ?
        LIMIT 1
        """,
        (int(publication["id"]),),
    ).fetchone()
    if message_exists is None and publication["claim_token"] is None:
        connection.execute(
            "DELETE FROM contest_publications WHERE id = ?",
            (int(publication["id"]),),
        )
        return

    enabled_row = connection.execute(
        """
        SELECT match_prediction_publication_enabled
        FROM contests
        WHERE id = ?
        """,
        (contest_id,),
    ).fetchone()
    if enabled_row is None:
        raise RuntimeError("Contest was not found while deleting a publication.")

    now_value = serialize_service_time(resolve_service_time(now_utc))
    if not bool(enabled_row["match_prediction_publication_enabled"]):
        connection.execute(
            """
            UPDATE contest_publications
            SET
                entity_id = -id,
                latest_event_id = ?,
                updated_at = ?
            WHERE id = ?
              AND contest_id = ?
              AND publication_type = 'match_result'
              AND entity_id = ?
            """,
            (
                event_id,
                now_value,
                int(publication["id"]),
                contest_id,
                match_id,
            ),
        )
        return

    connection.execute(
        """
        UPDATE contest_publications
        SET
            entity_id = -id,
            desired_revision = desired_revision + 1,
            desired_action = 'withdraw',
            delivery_status = 'pending',
            latest_event_id = ?,
            reconcile_at = NULL,
            attempt_count = 0,
            next_attempt_at = ?,
            last_error = NULL,
            updated_at = ?
        WHERE id = ?
          AND contest_id = ?
          AND publication_type = 'match_result'
          AND entity_id = ?
        """,
        (
            event_id,
            now_value,
            now_value,
            int(publication["id"]),
            contest_id,
            match_id,
        ),
    )


def claim_next_publication(
    *,
    database_path: Path,
    now_utc: datetime | None = None,
    lease_seconds: int = PUBLICATION_CLAIM_SECONDS,
) -> ClaimedPublication | None:
    now = resolve_service_time(now_utc)
    now_value = serialize_service_time(now)
    expires_value = serialize_service_time(now + timedelta(seconds=lease_seconds))

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        while True:
            row = connection.execute(
                """
                SELECT publication.*
                FROM contest_publications AS publication
                WHERE (
                    (
                        publication.desired_revision >
                            publication.settled_revision
                        AND (
                            publication.next_attempt_at IS NULL
                            OR publication.next_attempt_at <= ?
                        )
                    )
                    OR (
                        publication.reconcile_at IS NOT NULL
                        AND publication.reconcile_at <= ?
                    )
                )
                  AND EXISTS (
                      SELECT 1 FROM contests AS enabled_contest
                      WHERE enabled_contest.id = publication.contest_id
                        AND (
                            publication.publication_type = 'leaderboard_snapshot'
                            OR enabled_contest.match_prediction_publication_enabled = 1
                        )
                  )
                  AND (
                      publication.claim_token IS NULL
                      OR publication.claim_expires_at <= ?
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM contest_publications AS earlier
                      WHERE earlier.contest_id = publication.contest_id
                        AND (
                            earlier.first_event_id < publication.first_event_id
                            OR (
                                earlier.first_event_id = publication.first_event_id
                                AND earlier.id < publication.id
                            )
                        )
                        AND (
                            earlier.desired_revision > earlier.settled_revision
                            OR (
                                earlier.reconcile_at IS NOT NULL
                                AND earlier.reconcile_at <= ?
                            )
                        )
                        AND (
                            earlier.publication_type = 'leaderboard_snapshot'
                            OR EXISTS (
                                SELECT 1
                                FROM contests AS earlier_enabled_contest
                                WHERE earlier_enabled_contest.id = earlier.contest_id
                                  AND earlier_enabled_contest.match_prediction_publication_enabled = 1
                            )
                        )
                        AND NOT (
                            EXISTS (
                                SELECT 1
                                FROM contests AS completed_contest
                                WHERE completed_contest.id = publication.contest_id
                                  AND completed_contest.is_active = 0
                            )
                            AND publication.publication_type IN (
                                'champion_predictions',
                                'champion_result',
                                'contest_completed'
                            )
                            AND earlier.publication_type IN (
                                'champion_predictions',
                                'champion_result',
                                'contest_completed'
                            )
                            AND CASE earlier.publication_type
                                WHEN 'champion_predictions' THEN 1
                                WHEN 'champion_result' THEN 2
                                ELSE 3
                            END > CASE publication.publication_type
                                WHEN 'champion_predictions' THEN 1
                                WHEN 'champion_result' THEN 2
                                ELSE 3
                            END
                        )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM contests AS completed_contest
                      JOIN contest_publications AS dependency
                        ON dependency.contest_id = completed_contest.id
                      WHERE completed_contest.id = publication.contest_id
                        AND completed_contest.is_active = 0
                        AND (
                            dependency.desired_revision >
                                dependency.settled_revision
                            OR dependency.reconcile_at IS NOT NULL
                        )
                        AND (
                            (
                                publication.publication_type = 'champion_result'
                                AND dependency.publication_type =
                                    'champion_predictions'
                            )
                            OR (
                                publication.publication_type = 'contest_completed'
                                AND dependency.publication_type IN (
                                    'champion_predictions',
                                    'champion_result'
                                )
                            )
                        )
                  )
                ORDER BY publication.first_event_id ASC, publication.id ASC
                LIMIT 1
                """,
                (now_value, now_value, now_value, now_value),
            ).fetchone()
            if row is None:
                return None

            claim_token = uuid4().hex
            claimed = connection.execute(
                """
                UPDATE contest_publications
                SET
                    claim_token = ?,
                    claim_expires_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND EXISTS (
                      SELECT 1 FROM contests
                      WHERE id = contest_publications.contest_id
                        AND (
                            contest_publications.publication_type = 'leaderboard_snapshot'
                            OR match_prediction_publication_enabled = 1
                        )
                  )
                  AND (
                      claim_token IS NULL
                      OR claim_expires_at <= ?
                  )
                  AND (
                      (
                          desired_revision > settled_revision
                          AND (
                              next_attempt_at IS NULL
                              OR next_attempt_at <= ?
                          )
                      )
                      OR (
                          reconcile_at IS NOT NULL
                          AND reconcile_at <= ?
                      )
                  )
                """,
                (
                    claim_token,
                    expires_value,
                    now_value,
                    int(row["id"]),
                    now_value,
                    now_value,
                    now_value,
                ),
            )
            if claimed.rowcount != 1:
                continue

            return _claimed_publication_from_row(row, claim_token=claim_token)


def prepare_scheduled_reconciliation(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    now_utc: datetime | None = None,
) -> ClaimedPublication | None:
    now = resolve_service_time(now_utc)
    now_value = serialize_service_time(now)
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM contest_publications
            WHERE id = ? AND claim_token = ?
            """,
            (publication.id, publication.claim_token),
        ).fetchone()
        if row is None:
            return None
        if (
            int(row["desired_revision"]) != publication.desired_revision
            or str(row["desired_action"]) != publication.desired_action
            or int(row["entity_id"]) != publication.entity_id
        ):
            return publication
        reconcile_at = row["reconcile_at"]
        if reconcile_at is None or str(reconcile_at) > now_value:
            return publication
        if publication.publication_type not in (
            "champion_predictions",
            "champion_result",
            "swiss_predictions",
        ):
            cleared = connection.execute(
                """
                UPDATE contest_publications
                SET reconcile_at = NULL, updated_at = ?
                WHERE id = ?
                  AND claim_token = ?
                  AND desired_revision = ?
                  AND reconcile_at = ?
                """,
                (
                    now_value,
                    publication.id,
                    publication.claim_token,
                    int(row["desired_revision"]),
                    reconcile_at,
                ),
            )
            if cleared.rowcount != 1:
                return None
            refreshed = connection.execute(
                "SELECT * FROM contest_publications WHERE id = ? AND claim_token = ?",
                (publication.id, publication.claim_token),
            ).fetchone()
            return (
                _claimed_publication_from_row(
                    refreshed,
                    claim_token=publication.claim_token,
                )
                if refreshed is not None
                else None
            )

        if publication.publication_type == "champion_predictions":
            desired_action, next_reconcile_at = _champion_predictions_desired_state(
                connection,
                contest_id=publication.contest_id,
                now_utc=now,
            )
        elif publication.publication_type == "swiss_predictions":
            desired_action, next_reconcile_at = _swiss_predictions_desired_state(
                connection,
                contest_id=publication.contest_id,
                now_utc=now,
            )
        else:
            desired_action, next_reconcile_at = _champion_desired_state(
                connection,
                contest_id=publication.contest_id,
                now_utc=now,
            )
        needs_revision = desired_action != str(row["desired_action"])
        update = connection.execute(
            """
            UPDATE contest_publications
            SET
                desired_revision = desired_revision + ?,
                desired_action = ?,
                delivery_status = CASE
                    WHEN ? = 1 THEN 'pending'
                    ELSE delivery_status
                END,
                reconcile_at = ?,
                attempt_count = CASE WHEN ? = 1 THEN 0 ELSE attempt_count END,
                next_attempt_at = CASE WHEN ? = 1 THEN ? ELSE next_attempt_at END,
                last_error = CASE WHEN ? = 1 THEN NULL ELSE last_error END,
                updated_at = ?
            WHERE id = ?
              AND claim_token = ?
              AND desired_revision = ?
              AND reconcile_at = ?
            """,
            (
                int(needs_revision),
                desired_action,
                int(needs_revision),
                next_reconcile_at,
                int(needs_revision),
                int(needs_revision),
                now_value,
                int(needs_revision),
                now_value,
                publication.id,
                publication.claim_token,
                int(row["desired_revision"]),
                reconcile_at,
            ),
        )
        if update.rowcount != 1:
            return None
        refreshed = connection.execute(
            "SELECT * FROM contest_publications WHERE id = ? AND claim_token = ?",
            (publication.id, publication.claim_token),
        ).fetchone()
        return (
            _claimed_publication_from_row(
                refreshed,
                claim_token=publication.claim_token,
            )
            if refreshed is not None
            else None
        )


def renew_current_claim(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    now_utc: datetime | None = None,
    lease_seconds: int = PUBLICATION_CLAIM_SECONDS,
) -> PublicationClaimState:
    now = resolve_service_time(now_utc)
    with database_connection(database_path) as connection:
        update = connection.execute(
            """
            UPDATE contest_publications
            SET claim_expires_at = ?, updated_at = ?
            WHERE id = ?
              AND claim_token = ?
              AND desired_revision = ?
              AND desired_action = ?
              AND entity_id = ?
              AND EXISTS (
                  SELECT 1 FROM contests
                  WHERE id = ?
                    AND (
                        contest_publications.publication_type = 'leaderboard_snapshot'
                        OR match_prediction_publication_enabled = 1
                    )
              )
            """,
            (
                serialize_service_time(now + timedelta(seconds=lease_seconds)),
                serialize_service_time(now),
                publication.id,
                publication.claim_token,
                publication.desired_revision,
                publication.desired_action,
                publication.entity_id,
                publication.contest_id,
            ),
        )
        if update.rowcount == 1:
            return _current_claim_state(publication)
        return _inspect_claim_state(connection, publication=publication)


def inspect_claim_state(
    *,
    database_path: Path,
    publication: ClaimedPublication,
) -> PublicationClaimState:
    with database_connection(database_path) as connection:
        return _inspect_claim_state(connection, publication=publication)


def finish_publication_success(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    status: Literal["published", "withdrawn"],
    now_utc: datetime | None = None,
) -> bool:
    now_value = serialize_service_time(resolve_service_time(now_utc))
    with database_connection(database_path) as connection:
        update = connection.execute(
            """
            UPDATE contest_publications
            SET
                settled_revision = MAX(settled_revision, ?),
                delivery_status = CASE
                    WHEN desired_revision > MAX(settled_revision, ?)
                    THEN 'pending'
                    WHEN settled_revision > ? THEN delivery_status
                    ELSE ?
                END,
                attempt_count = CASE
                    WHEN desired_revision > ? THEN attempt_count
                    ELSE 0
                END,
                next_attempt_at = CASE
                    WHEN desired_revision > ? THEN next_attempt_at
                    ELSE NULL
                END,
                last_error = CASE
                    WHEN desired_revision > ? THEN last_error
                    ELSE NULL
                END,
                claim_token = NULL,
                claim_expires_at = NULL,
                updated_at = ?
            WHERE id = ? AND claim_token = ?
            """,
            (
                publication.desired_revision,
                publication.desired_revision,
                publication.desired_revision,
                status,
                publication.desired_revision,
                publication.desired_revision,
                publication.desired_revision,
                now_value,
                publication.id,
                publication.claim_token,
            ),
        )
        return update.rowcount == 1


def finish_publication_failure(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    error: str,
    permanent: bool,
    retry_after_seconds: float | None = None,
    now_utc: datetime | None = None,
) -> bool:
    now = resolve_service_time(now_utc)
    now_value = serialize_service_time(now)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT desired_revision, attempt_count
            FROM contest_publications
            WHERE id = ? AND claim_token = ?
            """,
            (publication.id, publication.claim_token),
        ).fetchone()
        if row is None:
            return False

        newer_revision_exists = int(row["desired_revision"]) > (
            publication.desired_revision
        )
        attempt_count = int(row["attempt_count"]) + 1
        terminal = permanent or attempt_count >= PUBLICATION_MAX_ATTEMPTS
        if retry_after_seconds is None:
            retry_after_seconds = min(
                PUBLICATION_MAX_BACKOFF_SECONDS,
                2 ** max(0, attempt_count - 1),
            )
        next_attempt_at = serialize_service_time(
            now + timedelta(seconds=max(0.0, retry_after_seconds))
        )

        update = connection.execute(
            """
            UPDATE contest_publications
            SET
                settled_revision = CASE
                    WHEN ? = 1 THEN MAX(settled_revision, ?)
                    ELSE settled_revision
                END,
                delivery_status = CASE
                    WHEN ? = 1 THEN 'pending'
                    WHEN ? = 1 THEN 'terminal_failed'
                    ELSE 'pending'
                END,
                attempt_count = CASE
                    WHEN ? = 1 THEN attempt_count
                    WHEN ? = 1 THEN ?
                    ELSE ?
                END,
                next_attempt_at = CASE
                    WHEN ? = 1 THEN next_attempt_at
                    WHEN ? = 1 THEN NULL
                    ELSE ?
                END,
                last_error = CASE
                    WHEN ? = 1 THEN last_error
                    ELSE ?
                END,
                claim_token = NULL,
                claim_expires_at = NULL,
                updated_at = ?
            WHERE id = ? AND claim_token = ?
            """,
            (
                int(terminal and not newer_revision_exists),
                publication.desired_revision,
                int(newer_revision_exists),
                int(terminal),
                int(newer_revision_exists),
                int(terminal),
                attempt_count,
                attempt_count,
                int(newer_revision_exists),
                int(terminal),
                next_attempt_at,
                int(newer_revision_exists),
                error[:2000],
                now_value,
                publication.id,
                publication.claim_token,
            ),
        )
        return update.rowcount == 1


def _inspect_claim_state(
    connection: sqlite3.Connection,
    *,
    publication: ClaimedPublication,
) -> PublicationClaimState:
    row = connection.execute(
        """
        SELECT
            publication.desired_revision,
            publication.desired_action,
            publication.entity_id,
            publication.publication_type,
            contests.match_prediction_publication_enabled
                AS publication_enabled
        FROM contest_publications AS publication
        JOIN contests ON contests.id = publication.contest_id
        WHERE publication.id = ? AND publication.claim_token = ?
        """,
        (publication.id, publication.claim_token),
    ).fetchone()
    if row is None:
        return PublicationClaimState(status="lost")

    desired_revision = int(row["desired_revision"])
    desired_action = str(row["desired_action"])
    entity_id = int(row["entity_id"])
    publication_enabled = str(
        row["publication_type"]
    ) == "leaderboard_snapshot" or bool(row["publication_enabled"])
    status: PublicationClaimStatus = (
        "current"
        if (
            desired_revision == publication.desired_revision
            and desired_action == publication.desired_action
            and entity_id == publication.entity_id
            and publication_enabled
        )
        else "stale"
    )
    return PublicationClaimState(
        status=status,
        desired_revision=desired_revision,
        desired_action=desired_action,  # type: ignore[arg-type]
        entity_id=entity_id,
        publication_enabled=publication_enabled,
    )


def _current_claim_state(
    publication: ClaimedPublication,
) -> PublicationClaimState:
    return PublicationClaimState(
        status="current",
        desired_revision=publication.desired_revision,
        desired_action=publication.desired_action,
        entity_id=publication.entity_id,
        publication_enabled=True,
    )


def _champion_desired_state(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    now_utc: datetime,
) -> tuple[PublicationAction, str | None]:
    row = connection.execute(
        """
        SELECT
            champion_prediction_enabled,
            champion_prediction_deadline_at,
            champion_team_id
        FROM contests
        WHERE id = ?
        """,
        (contest_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Contest was not found while reconciling champion results.")

    deadline_value = row["champion_prediction_deadline_at"]
    if (
        row["champion_team_id"] is None
        or not bool(row["champion_prediction_enabled"])
        or deadline_value is None
    ):
        return "withdraw", None

    deadline = datetime.fromisoformat(str(deadline_value).replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise RuntimeError("Champion prediction deadline does not include a timezone.")
    deadline = deadline.astimezone(timezone.utc)
    if deadline > now_utc:
        return "withdraw", serialize_service_time(deadline)
    return "publish", None


def _champion_predictions_desired_state(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    now_utc: datetime,
) -> tuple[PublicationAction, str | None]:
    row = connection.execute(
        """
        SELECT
            champion_prediction_enabled,
            champion_prediction_deadline_at
        FROM contests
        WHERE id = ?
        """,
        (contest_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "Contest was not found while reconciling champion predictions."
        )
    deadline_value = row["champion_prediction_deadline_at"]
    if not bool(row["champion_prediction_enabled"]) or deadline_value is None:
        return "withdraw", None
    deadline = datetime.fromisoformat(str(deadline_value).replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise RuntimeError("Champion prediction deadline does not include a timezone.")
    deadline = deadline.astimezone(timezone.utc)
    if deadline > now_utc:
        return "withdraw", serialize_service_time(deadline)
    return "publish", None


def _swiss_predictions_desired_state(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    now_utc: datetime,
) -> tuple[PublicationAction, str | None]:
    row = connection.execute(
        """
        SELECT enabled, deadline_at
        FROM swiss_stage_prediction_settings
        WHERE contest_id = ?
        """,
        (contest_id,),
    ).fetchone()
    if row is None or not bool(row["enabled"]) or row["deadline_at"] is None:
        return "withdraw", None
    deadline = datetime.fromisoformat(str(row["deadline_at"]).replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise RuntimeError("Swiss prediction deadline does not include a timezone.")
    deadline = deadline.astimezone(timezone.utc)
    if deadline > now_utc:
        return "withdraw", serialize_service_time(deadline)
    return "publish", None


def _claimed_publication_from_row(
    row: sqlite3.Row,
    *,
    claim_token: str,
) -> ClaimedPublication:
    return ClaimedPublication(
        id=int(row["id"]),
        contest_id=int(row["contest_id"]),
        publication_type=str(row["publication_type"]),  # type: ignore[arg-type]
        entity_id=int(row["entity_id"]),
        desired_revision=int(row["desired_revision"]),
        desired_action=str(row["desired_action"]),  # type: ignore[arg-type]
        claim_token=claim_token,
    )

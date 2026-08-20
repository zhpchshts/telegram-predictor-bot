from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path

from app.database import database_connection


LOGGER = logging.getLogger(__name__)
MATCH_LIFECYCLE_POLL_INTERVAL_SECONDS = 15.0


def start_due_matches(
    *,
    database_path: Path,
    contest_id: int | None = None,
    now_utc: datetime | None = None,
) -> int:
    resolved_now_utc = _resolve_now_utc(now_utc)
    serialized_now_utc = _serialize_match_time(resolved_now_utc)

    contest_filter = ""
    parameters: tuple[object, ...] = (serialized_now_utc,)
    if contest_id is not None:
        contest_filter = "AND contests.id = ?"
        parameters = (*parameters, contest_id)

    with database_connection(database_path) as connection:
        update = connection.execute(
            f"""
            UPDATE matches
            SET status = 'started'
            WHERE status = 'scheduled'
              AND starts_at_utc <= ?
              AND EXISTS (
                  SELECT 1
                  FROM stages
                  JOIN competitions
                    ON competitions.id = stages.competition_id
                  JOIN contests
                    ON contests.id = competitions.contest_id
                  WHERE stages.id = matches.stage_id
                    AND contests.is_active = 1
                    {contest_filter}
              )
            """,
            parameters,
        )
        shared_update_count = 0
        if contest_id is None:
            shared_update = connection.execute(
                """
                UPDATE shared_matches
                SET status = 'started', version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'scheduled' AND starts_at_utc <= ?
                """,
                (serialized_now_utc,),
            )
            shared_update_count = shared_update.rowcount

    return update.rowcount + shared_update_count


async def run_match_lifecycle_worker(
    *,
    database_path: Path,
    poll_interval_seconds: float = MATCH_LIFECYCLE_POLL_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            start_due_matches(database_path=database_path)
        except Exception:
            LOGGER.exception("Could not start due matches.")
        await asyncio.sleep(poll_interval_seconds)


def _resolve_now_utc(now_utc: datetime | None) -> datetime:
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must include a timezone.")
    return now_utc.astimezone(timezone.utc)


def _serialize_match_time(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from app.database import database_connection


class TelegramHealthcheckClient(Protocol):
    async def send_message(self, chat_id: int, *, text: str) -> object: ...


@dataclass(frozen=True, slots=True)
class HealthcheckSnapshot:
    active_contests_count: int
    active_matches_count: int
    saved_predictions_count: int


def get_healthcheck_snapshot(*, database_path: Path) -> HealthcheckSnapshot:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT
                (
                    SELECT COUNT(*)
                    FROM contests
                    WHERE is_active = 1
                ) AS active_contests_count,
                (
                    SELECT COUNT(*)
                    FROM matches
                    JOIN stages ON stages.id = matches.stage_id
                    JOIN competitions
                      ON competitions.id = stages.competition_id
                    JOIN contests ON contests.id = competitions.contest_id
                    WHERE contests.is_active = 1
                      AND matches.status IN ('scheduled', 'started')
                ) AS active_matches_count,
                (
                    SELECT COUNT(*)
                    FROM match_predictions
                    JOIN matches ON matches.id = match_predictions.match_id
                    JOIN stages ON stages.id = matches.stage_id
                    JOIN competitions
                      ON competitions.id = stages.competition_id
                    JOIN contests ON contests.id = competitions.contest_id
                    WHERE contests.is_active = 1
                ) + (
                    SELECT COUNT(*)
                    FROM champion_predictions
                    JOIN contests
                      ON contests.id = champion_predictions.contest_id
                    WHERE contests.is_active = 1
                ) + (
                    SELECT COUNT(*)
                    FROM swiss_stage_predictions AS predictions
                    JOIN contests
                      ON contests.id = predictions.contest_id
                    WHERE contests.is_active = 1
                      AND EXISTS (
                        SELECT 1
                        FROM swiss_stage_prediction_selections AS selections
                        WHERE selections.prediction_id = predictions.id
                      )
                ) AS saved_predictions_count
            """
        ).fetchone()

    if row is None:
        raise RuntimeError("Could not read healthcheck statistics.")
    return HealthcheckSnapshot(
        active_contests_count=int(row["active_contests_count"]),
        active_matches_count=int(row["active_matches_count"]),
        saved_predictions_count=int(row["saved_predictions_count"]),
    )


async def send_healthcheck_notification(
    *,
    bot: TelegramHealthcheckClient,
    database_path: Path,
    chat_id: int,
) -> None:
    snapshot = get_healthcheck_snapshot(database_path=database_path)
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ Клевер работает.\n\n"
            f"Время сервера UTC: {now_utc}\n"
            f"Активных конкурсов: {snapshot.active_contests_count}\n"
            f"Предстоящих и идущих матчей: {snapshot.active_matches_count}\n"
            f"Сохранённых прогнозов: {snapshot.saved_predictions_count}"
        ),
    )

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Protocol

from aiogram.types import InputRichMessage

from app.database import database_connection
from app.rich_publications import (
    RICH_MESSAGE_MAX_LENGTH,
    RICH_MESSAGE_MAX_TABLE_ROWS,
    escape_rich_text,
    rich_message,
    split_rich_table_messages,
    table_row,
)

LOGGER = logging.getLogger(__name__)

MATCH_PREDICTION_PUBLICATION_POLL_INTERVAL_SECONDS = 15.0
MATCH_PREDICTION_PUBLICATION_MAX_MESSAGE_LENGTH = RICH_MESSAGE_MAX_LENGTH
MATCH_PREDICTION_PUBLICATION_MAX_TABLE_ROWS = RICH_MESSAGE_MAX_TABLE_ROWS


class SentTelegramMessage(Protocol):
    message_id: int


class TelegramMessageSender(Protocol):
    async def send_rich_message(
        self,
        chat_id: int,
        *,
        rich_message: InputRichMessage,
    ) -> SentTelegramMessage: ...


@dataclass(frozen=True, slots=True)
class PendingMatchPredictionPublication:
    match_id: int
    telegram_chat_id: int
    contest_name: str
    home_team_name: str
    away_team_name: str
    starts_at_utc: datetime


async def run_match_prediction_publication_worker(
    *,
    bot: TelegramMessageSender,
    database_path: Path,
    poll_interval_seconds: float = MATCH_PREDICTION_PUBLICATION_POLL_INTERVAL_SECONDS,
) -> None:
    while True:
        try:
            await publish_due_match_predictions(
                bot=bot,
                database_path=database_path,
            )
        except Exception:
            LOGGER.exception("Could not publish due match predictions.")
        await asyncio.sleep(poll_interval_seconds)


async def publish_due_match_predictions(
    *,
    bot: TelegramMessageSender,
    database_path: Path,
    now_utc: datetime | None = None,
    max_message_length: int = MATCH_PREDICTION_PUBLICATION_MAX_MESSAGE_LENGTH,
    max_table_rows: int = MATCH_PREDICTION_PUBLICATION_MAX_TABLE_ROWS,
) -> None:
    resolved_now_utc = _resolve_now_utc(now_utc)
    pending_publications = _get_pending_publications(
        database_path=database_path,
        now_utc=resolved_now_utc,
    )

    for publication in pending_publications:
        try:
            await _publish_match_predictions(
                bot=bot,
                database_path=database_path,
                publication=publication,
                max_message_length=max_message_length,
                max_table_rows=max_table_rows,
            )
        except Exception:
            LOGGER.exception(
                "Could not publish predictions for match %s.",
                publication.match_id,
            )


def _get_pending_publications(
    *,
    database_path: Path,
    now_utc: datetime,
) -> tuple[PendingMatchPredictionPublication, ...]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                matches.id AS match_id,
                chats.telegram_chat_id,
                contests.name AS contest_name,
                contests.match_prediction_publication_enabled_at,
                home_team.name AS home_team_name,
                away_team.name AS away_team_name,
                matches.starts_at_utc
            FROM matches
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            JOIN contests ON contests.id = competitions.contest_id
            JOIN chats ON chats.id = contests.chat_id
            JOIN teams AS home_team ON home_team.id = matches.home_team_id
            JOIN teams AS away_team ON away_team.id = matches.away_team_id
            LEFT JOIN match_prediction_publications
                ON match_prediction_publications.match_id = matches.id
            WHERE contests.is_active = 1
              AND contests.match_prediction_publication_enabled = 1
              AND matches.status IN ('scheduled', 'started')
              AND match_prediction_publications.match_id IS NULL
            """
        ).fetchall()

    publications: list[PendingMatchPredictionPublication] = []
    for row in rows:
        enabled_at_value = row["match_prediction_publication_enabled_at"]
        if enabled_at_value is None:
            continue

        starts_at_utc = _parse_datetime_utc(str(row["starts_at_utc"]))
        enabled_at_utc = _parse_datetime_utc(str(enabled_at_value))
        if starts_at_utc <= enabled_at_utc or starts_at_utc > now_utc:
            continue
        publications.append(
            PendingMatchPredictionPublication(
                match_id=int(row["match_id"]),
                telegram_chat_id=int(row["telegram_chat_id"]),
                contest_name=str(row["contest_name"]),
                home_team_name=str(row["home_team_name"]),
                away_team_name=str(row["away_team_name"]),
                starts_at_utc=starts_at_utc,
            )
        )

    return tuple(
        sorted(
            publications,
            key=lambda publication: (
                publication.starts_at_utc,
                publication.match_id,
            ),
        )
    )


async def _publish_match_predictions(
    *,
    bot: TelegramMessageSender,
    database_path: Path,
    publication: PendingMatchPredictionPublication,
    max_message_length: int,
    max_table_rows: int,
) -> None:
    messages = _build_messages(
        publication=publication,
        predictions=_get_prediction_rows(
            database_path=database_path,
            match_id=publication.match_id,
        ),
        max_message_length=max_message_length,
        max_table_rows=max_table_rows,
    )
    sent_part_numbers = _get_sent_part_numbers(
        database_path=database_path,
        match_id=publication.match_id,
    )

    for part_number, rich_html in enumerate(messages):
        if part_number in sent_part_numbers:
            continue
        telegram_message = await bot.send_rich_message(
            chat_id=publication.telegram_chat_id,
            rich_message=rich_message(rich_html),
        )
        _save_sent_message(
            database_path=database_path,
            match_id=publication.match_id,
            part_number=part_number,
            telegram_message_id=int(telegram_message.message_id),
        )

    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO match_prediction_publications (match_id)
            VALUES (?)
            """,
            (publication.match_id,),
        )


def _get_prediction_rows(*, database_path: Path, match_id: int):
    with database_connection(database_path) as connection:
        return connection.execute(
            """
            SELECT
                users.id AS user_id,
                users.first_name,
                users.last_name,
                match_predictions.predicted_home_score,
                match_predictions.predicted_away_score,
                advancing_team.name AS advancing_team_name
            FROM match_predictions
            JOIN users ON users.id = match_predictions.user_id
            JOIN matches ON matches.id = match_predictions.match_id
            LEFT JOIN tie_predictions
                ON tie_predictions.tie_id = matches.tie_id
               AND tie_predictions.user_id = match_predictions.user_id
            LEFT JOIN teams AS advancing_team
                ON advancing_team.id = tie_predictions.predicted_advancing_team_id
            WHERE match_predictions.match_id = ?
            ORDER BY
                users.first_name COLLATE NOCASE ASC,
                COALESCE(users.last_name, '') COLLATE NOCASE ASC,
                users.id ASC
            """,
            (match_id,),
        ).fetchall()


def _get_sent_part_numbers(*, database_path: Path, match_id: int) -> frozenset[int]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT part_number
            FROM match_prediction_publication_messages
            WHERE match_id = ?
            """,
            (match_id,),
        ).fetchall()
    return frozenset(int(row["part_number"]) for row in rows)


def _save_sent_message(
    *,
    database_path: Path,
    match_id: int,
    part_number: int,
    telegram_message_id: int,
) -> None:
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO match_prediction_publication_messages (
                match_id,
                part_number,
                telegram_message_id
            )
            VALUES (?, ?, ?)
            """,
            (match_id, part_number, telegram_message_id),
        )


def _build_messages(
    *,
    publication: PendingMatchPredictionPublication,
    predictions,
    max_message_length: int,
    max_table_rows: int = MATCH_PREDICTION_PUBLICATION_MAX_TABLE_ROWS,
) -> tuple[str, ...]:
    title = (
        f"<p><b>⚽ {escape_rich_text(publication.home_team_name)} — "
        f"{escape_rich_text(publication.away_team_name)}</b></p>"
    )
    contest_line = f"Конкурс: «{escape_rich_text(publication.contest_name)}»"
    first_header = f"{title}<p>Матч начался<br>{contest_line}</p>"
    continuation_header = f"{title}<p>{contest_line}</p>"

    if not predictions:
        message = f"{first_header}<p>Пока никто не оставил прогноз.</p>"
        if len(message) > max_message_length:
            raise ValueError(
                "Maximum Rich Message length is too small for the publication."
            )
        return (message,)

    prediction_rows = tuple(
        _format_prediction_row(prediction) for prediction in predictions
    )
    return split_rich_table_messages(
        first_header=first_header,
        continuation_header=continuation_header,
        first_caption=f"Прогнозы участников · {len(prediction_rows)}",
        continuation_caption="Прогнозы участников · продолжение",
        column_names=("Участник", "Прогноз"),
        alignments=("left", "center"),
        rows=prediction_rows,
        max_message_length=max_message_length,
        max_table_rows=max_table_rows,
    )


def _format_prediction_row(prediction) -> str:
    participant_name = " ".join(
        part
        for part in (
            str(prediction["first_name"]).strip(),
            str(prediction["last_name"] or "").strip(),
        )
        if part
    )
    if not participant_name:
        participant_name = f"Участник {int(prediction['user_id'])}"

    home_score = int(prediction["predicted_home_score"])
    away_score = int(prediction["predicted_away_score"])
    result = f"{home_score}:{away_score}"
    advancing_team_name = prediction["advancing_team_name"]
    if home_score == away_score and advancing_team_name is not None:
        result += f" → {escape_rich_text(advancing_team_name)}"
    return table_row(
        (escape_rich_text(participant_name), result),
        alignments=("left", "center"),
    )


def _resolve_now_utc(now_utc: datetime | None) -> datetime:
    if now_utc is None:
        return datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must include a timezone.")
    return now_utc.astimezone(timezone.utc)


def _parse_datetime_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

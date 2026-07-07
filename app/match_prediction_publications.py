from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
import logging
from pathlib import Path
from typing import Protocol

from app.database import database_connection

LOGGER = logging.getLogger(__name__)

MATCH_PREDICTION_PUBLICATION_POLL_INTERVAL_SECONDS = 15.0
MATCH_PREDICTION_PUBLICATION_MAX_MESSAGE_LENGTH = 3900


class SentTelegramMessage(Protocol):
    message_id: int


class TelegramMessageSender(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str,
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
) -> None:
    resolved_now_utc = _resolve_now_utc(now_utc)
    activated_at_utc = _get_or_create_activation_time(
        database_path=database_path,
        now_utc=resolved_now_utc,
    )
    pending_publications = _get_pending_publications(
        database_path=database_path,
        activated_at_utc=activated_at_utc,
        now_utc=resolved_now_utc,
    )

    for publication in pending_publications:
        try:
            await _publish_match_predictions(
                bot=bot,
                database_path=database_path,
                publication=publication,
                max_message_length=max_message_length,
            )
        except Exception:
            LOGGER.exception(
                "Could not publish predictions for match %s.",
                publication.match_id,
            )


def _get_or_create_activation_time(
    *,
    database_path: Path,
    now_utc: datetime,
) -> datetime:
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO match_prediction_publication_settings (
                singleton,
                activated_at_utc
            )
            VALUES (1, ?)
            """,
            (_serialize_datetime_utc(now_utc),),
        )
        row = connection.execute(
            """
            SELECT activated_at_utc
            FROM match_prediction_publication_settings
            WHERE singleton = 1
            """
        ).fetchone()

    if row is None:
        raise RuntimeError("Could not initialize prediction publication settings.")
    return _parse_datetime_utc(str(row["activated_at_utc"]))


def _get_pending_publications(
    *,
    database_path: Path,
    activated_at_utc: datetime,
    now_utc: datetime,
) -> tuple[PendingMatchPredictionPublication, ...]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                matches.id AS match_id,
                chats.telegram_chat_id,
                contests.name AS contest_name,
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
              AND matches.status IN ('scheduled', 'started')
              AND match_prediction_publications.match_id IS NULL
            """
        ).fetchall()

    publications: list[PendingMatchPredictionPublication] = []
    for row in rows:
        starts_at_utc = _parse_datetime_utc(str(row["starts_at_utc"]))
        if starts_at_utc < activated_at_utc or starts_at_utc > now_utc:
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
) -> None:
    messages = _build_messages(
        publication=publication,
        predictions=_get_prediction_rows(
            database_path=database_path,
            match_id=publication.match_id,
        ),
        max_message_length=max_message_length,
    )
    sent_part_numbers = _get_sent_part_numbers(
        database_path=database_path,
        match_id=publication.match_id,
    )

    for part_number, message_text in enumerate(messages):
        if part_number in sent_part_numbers:
            continue
        telegram_message = await bot.send_message(
            chat_id=publication.telegram_chat_id,
            text=message_text,
            parse_mode="HTML",
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
) -> tuple[str, ...]:
    title = (
        f"⚽ <b>{escape(publication.home_team_name)} — "
        f"{escape(publication.away_team_name)}</b>"
    )
    header = (
        f"{title}\n"
        f"Конкурс: «{escape(publication.contest_name)}»\n\n"
        "Матч начался. Прогнозы участников:"
    )
    continuation_header = f"{title}\nПродолжение прогнозов участников:"
    if len(header) > max_message_length or len(continuation_header) > max_message_length:
        raise ValueError("Maximum Telegram message length is too small for the header.")

    prediction_lines = tuple(
        _format_prediction_line(prediction) for prediction in predictions
    )
    if not prediction_lines:
        return (f"{header}\n\nПока никто не оставил прогноз.",)

    messages: list[str] = []
    current_message = header
    for prediction_line in prediction_lines:
        candidate = f"{current_message}\n{prediction_line}"
        if len(candidate) <= max_message_length:
            current_message = candidate
            continue

        messages.append(current_message)
        current_message = f"{continuation_header}\n{prediction_line}"
        if len(current_message) > max_message_length:
            raise ValueError(
                "A prediction line does not fit into one Telegram message."
            )

    messages.append(current_message)
    return tuple(messages)


def _format_prediction_line(prediction) -> str:
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
    result = f"{escape(participant_name)} — {home_score}:{away_score}"
    advancing_team_name = prediction["advancing_team_name"]
    if home_score == away_score and advancing_team_name is not None:
        return f"• {result}, проходит {escape(str(advancing_team_name))}"
    return f"• {result}"


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


def _serialize_datetime_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()

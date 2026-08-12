from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from aiogram.types import InputRichMessage

from app.contest_service import ContestCompletedError, ContestNotFoundError
from app.database import database_connection
from app.rich_publications import (
    RICH_MESSAGE_MAX_LENGTH,
    escape_rich_text,
    rich_message,
)


class TelegramPredictionReminderClient(Protocol):
    async def send_rich_message(
        self,
        chat_id: int,
        *,
        rich_message: InputRichMessage,
    ) -> object: ...


class NoOpenPredictionRemindersError(RuntimeError):
    pass


class PredictionReminderMessageTooLongError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PredictionReminderMessage:
    telegram_chat_id: int
    html: str
    reminder_count: int
    match_count: int


async def publish_prediction_reminders(
    *,
    bot: TelegramPredictionReminderClient,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    now_utc: datetime | None = None,
) -> PredictionReminderMessage:
    message = build_prediction_reminder_message(
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        contest_id=contest_id,
        now_utc=now_utc,
    )
    await bot.send_rich_message(
        chat_id=message.telegram_chat_id,
        rich_message=rich_message(message.html),
    )
    return message


def build_prediction_reminder_message(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    now_utc: datetime | None = None,
    max_message_length: int = RICH_MESSAGE_MAX_LENGTH,
) -> PredictionReminderMessage:
    resolved_now_utc = _resolve_now_utc(now_utc)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN")
        contest = connection.execute(
            """
            SELECT
                contests.name,
                contests.is_active,
                contests.champion_prediction_enabled,
                contests.champion_prediction_deadline_at,
                chats.telegram_chat_id,
                swiss_settings.enabled AS swiss_prediction_enabled,
                swiss_settings.deadline_at AS swiss_prediction_deadline_at
            FROM contests
            JOIN chats ON chats.id = contests.chat_id
            LEFT JOIN swiss_stage_prediction_settings AS swiss_settings
                ON swiss_settings.contest_id = contests.id
            WHERE contests.id = ?
              AND chats.telegram_chat_id = ?
            """,
            (contest_id, telegram_chat_id),
        ).fetchone()
        if contest is None:
            raise ContestNotFoundError("Конкурс не найден в этом чате.")
        if not bool(contest["is_active"]):
            raise ContestCompletedError(
                "Для завершённого конкурса нельзя публиковать напоминания."
            )

        matches = connection.execute(
            """
            SELECT
                matches.id,
                matches.starts_at_utc,
                home_team.name AS home_team_name,
                away_team.name AS away_team_name
            FROM matches
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            JOIN teams AS home_team ON home_team.id = matches.home_team_id
            JOIN teams AS away_team ON away_team.id = matches.away_team_id
            WHERE competitions.contest_id = ?
              AND matches.status = 'scheduled'
            ORDER BY matches.starts_at_utc ASC, matches.id ASC
            """,
            (contest_id,),
        ).fetchall()

    reminder_sections: list[str] = []
    reminder_count = 0

    swiss_deadline = _open_deadline(
        enabled=contest["swiss_prediction_enabled"],
        value=contest["swiss_prediction_deadline_at"],
        now_utc=resolved_now_utc,
        field_name="Swiss prediction deadline",
    )
    champion_deadline = _open_deadline(
        enabled=contest["champion_prediction_enabled"],
        value=contest["champion_prediction_deadline_at"],
        now_utc=resolved_now_utc,
        field_name="Champion prediction deadline",
    )

    tournament_lines: list[str] = []
    if swiss_deadline is not None:
        tournament_lines.append(
            "<p>🔮 <b>Прогноз на швейцарский этап</b><br>"
            f"Дедлайн: {_format_datetime(swiss_deadline)}</p>"
        )
        reminder_count += 1
    if champion_deadline is not None:
        tournament_lines.append(
            "<p>🏆 <b>Прогноз на чемпиона</b><br>"
            f"Дедлайн: {_format_datetime(champion_deadline)}</p>"
        )
        reminder_count += 1
    if tournament_lines:
        reminder_sections.append(
            "<p><b>Турнирные прогнозы</b></p>" + "".join(tournament_lines)
        )

    upcoming_match_lines: list[str] = []
    for match in matches:
        starts_at_utc = _parse_datetime(
            match["starts_at_utc"],
            field_name=f"Match {match['id']} start",
        )
        if starts_at_utc <= resolved_now_utc:
            continue
        upcoming_match_lines.append(
            "<p>⚽ <b>"
            f"{escape_rich_text(match['home_team_name'])} — "
            f"{escape_rich_text(match['away_team_name'])}"
            "</b><br>"
            f"Начало: {_format_datetime(starts_at_utc)}</p>"
        )
    match_count = len(upcoming_match_lines)
    reminder_count += match_count
    if upcoming_match_lines:
        reminder_sections.append(
            "<p><b>Предстоящие матчи</b></p>" + "".join(upcoming_match_lines)
        )

    if not reminder_sections:
        raise NoOpenPredictionRemindersError(
            "Нет открытых прогнозов или предстоящих матчей для публикации."
        )

    html = (
        "<p>⏰ <b>Не забудьте сделать прогнозы</b></p>"
        f"<p>Конкурс: «{escape_rich_text(contest['name'])}»</p>"
        + "".join(reminder_sections)
    )
    if len(html) > max_message_length:
        raise PredictionReminderMessageTooLongError(
            "Все напоминания не помещаются в одно сообщение."
        )
    return PredictionReminderMessage(
        telegram_chat_id=int(contest["telegram_chat_id"]),
        html=html,
        reminder_count=reminder_count,
        match_count=match_count,
    )


def _open_deadline(
    *,
    enabled: object,
    value: object,
    now_utc: datetime,
    field_name: str,
) -> datetime | None:
    if not bool(enabled) or value is None:
        return None
    deadline = _parse_datetime(value, field_name=field_name)
    return deadline if deadline > now_utc else None


def _parse_datetime(value: object, *, field_name: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{field_name} does not include a timezone.")
    return parsed.astimezone(timezone.utc)


def _resolve_now_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Current time must include a timezone.")
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%d.%m.%Y, %H:%M UTC")

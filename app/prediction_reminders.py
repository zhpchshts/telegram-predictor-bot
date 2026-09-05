from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, Sequence

from aiogram.types import InlineKeyboardMarkup, InputRichMessage

from app.contest_service import ContestCompletedError, ContestNotFoundError
from app.database import database_connection
from app.rich_publications import (
    RICH_MESSAGE_MAX_LENGTH,
    escape_rich_text,
    rich_message,
)
from app.tournament_catalog import CHAMPIONS_LEAGUE_2026_27_TEMPLATE_KEY

if TYPE_CHECKING:
    from app.prediction_reminder_store import (
        ReminderRecipient,
        ReminderRenderRequest,
        RenderedReminderPart,
    )


class TelegramPredictionReminderClient(Protocol):
    async def send_rich_message(
        self,
        chat_id: int,
        *,
        rich_message: InputRichMessage,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> object: ...


class NoOpenPredictionRemindersError(RuntimeError):
    pass


class PredictionReminderMessageTooLongError(RuntimeError):
    pass


_MENTION_LABEL_MAX_LENGTH = 160


@dataclass(frozen=True, slots=True)
class PredictionReminderMessage:
    telegram_chat_id: int
    html_parts: tuple[str, ...]
    reminder_count: int
    match_count: int
    mention_count: int = 0

    @property
    def html(self) -> str:
        """Compatibility view for callers of the original one-part renderer."""
        return "".join(self.html_parts)


@dataclass(frozen=True, slots=True)
class AutomaticReminderMatch:
    match_id: int
    starts_at_utc: datetime
    home_team_name: str
    away_team_name: str


@dataclass(frozen=True, slots=True)
class AutomaticReminderDeadline:
    kind: Literal["swiss", "champion"]
    deadline_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ReminderMentionRecipient:
    user_id: int
    telegram_user_id: int
    first_name: str
    last_name: str | None


@dataclass(frozen=True, slots=True)
class _MatchReminder:
    match_id: int
    starts_at_utc: datetime
    home_team_name: str
    away_team_name: str
    tie_id: int | None
    is_two_legged: bool
    leg_number: int | None


@dataclass(frozen=True, slots=True)
class _TournamentReminder:
    kind: str
    label: str
    deadline_at: datetime
    direct_count: int | None = None
    elimination_count: int | None = None


class PredictionReminderRenderer:
    """Durable-worker adapter; the worker supplies an explicit match snapshot."""

    def __init__(
        self,
        *,
        database_path: Path,
        max_message_length: int = RICH_MESSAGE_MAX_LENGTH,
    ) -> None:
        self._database_path = database_path
        self._max_message_length = max_message_length

    def render(
        self,
        request: ReminderRenderRequest,
    ) -> tuple[RenderedReminderPart, ...]:
        from app.prediction_reminder_store import RenderedReminderPart

        if request.kind == "manual":
            message = build_prediction_reminder_message(
                database_path=self._database_path,
                telegram_chat_id=request.telegram_chat_id,
                contest_id=request.contest_id,
                eligible_recipients=tuple(
                    _recipient_from_worker(item) for item in request.recipients
                ),
                max_message_length=self._max_message_length,
            )
        else:
            message = build_automatic_prediction_reminder_message(
                database_path=self._database_path,
                telegram_chat_id=request.telegram_chat_id,
                contest_id=request.contest_id,
                contest_name=request.contest_name,
                matches=tuple(
                    AutomaticReminderMatch(
                        match_id=item.match_id,
                        starts_at_utc=_parse_datetime(
                            item.starts_at_utc,
                            field_name=f"Reminder match {item.match_id} start",
                        ),
                        home_team_name=item.home_team_name,
                        away_team_name=item.away_team_name,
                    )
                    for item in request.items
                ),
                deadlines=tuple(
                    AutomaticReminderDeadline(
                        kind=item.kind,
                        deadline_at_utc=_parse_datetime(
                            item.deadline_at_utc,
                            field_name=f"Reminder {item.kind} deadline",
                        ),
                    )
                    for item in request.deadlines
                ),
                eligible_recipients=tuple(
                    _recipient_from_worker(item) for item in request.recipients
                ),
                max_message_length=self._max_message_length,
            )
        return tuple(
            RenderedReminderPart(
                html=html,
                has_launch_button=index == len(message.html_parts) - 1,
            )
            for index, html in enumerate(message.html_parts)
        )


async def publish_prediction_reminders(
    *,
    bot: TelegramPredictionReminderClient,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    reply_markup: InlineKeyboardMarkup,
    now_utc: datetime | None = None,
) -> PredictionReminderMessage:
    """Legacy direct publisher retained for non-HTTP compatibility."""
    message = build_prediction_reminder_message(
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        contest_id=contest_id,
        now_utc=now_utc,
    )
    for index, html in enumerate(message.html_parts):
        await bot.send_rich_message(
            chat_id=message.telegram_chat_id,
            rich_message=rich_message(html),
            reply_markup=reply_markup if index == len(message.html_parts) - 1 else None,
        )
    return message


def build_prediction_reminder_message(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    now_utc: datetime | None = None,
    max_message_length: int = RICH_MESSAGE_MAX_LENGTH,
    eligible_recipients: Sequence[ReminderMentionRecipient] | None = None,
) -> PredictionReminderMessage:
    """Build the broad manual reminder (matches plus long-term forecasts)."""
    resolved_now = _resolve_now_utc(now_utc)
    with database_connection(database_path) as connection:
        contest = _load_contest(
            connection,
            contest_id=contest_id,
            telegram_chat_id=telegram_chat_id,
        )
        matches = _load_manual_matches(connection, contest_id, resolved_now)
        tournament = _load_tournament_reminders(contest, resolved_now)
        recipients = (
            _load_eligible_recipients(connection, contest_id)
            if eligible_recipients is None
            else tuple(eligible_recipients)
        )
        recipients = _filter_missing_recipients(
            connection,
            contest_id=contest_id,
            matches=matches,
            tournament=tournament,
            recipients=recipients,
        )
    return _render_message(
        telegram_chat_id=int(contest["telegram_chat_id"]),
        contest_name=str(contest["name"]),
        tournament=tournament,
        matches=matches,
        recipients=recipients,
        max_message_length=max_message_length,
    )


def build_automatic_prediction_reminder_message(
    *,
    database_path: Path,
    telegram_chat_id: int,
    contest_id: int,
    contest_name: str,
    matches: Sequence[AutomaticReminderMatch],
    eligible_recipients: Sequence[ReminderMentionRecipient],
    deadlines: Sequence[AutomaticReminderDeadline] = (),
    now_utc: datetime | None = None,
    max_message_length: int = RICH_MESSAGE_MAX_LENGTH,
) -> PredictionReminderMessage:
    """Build an automatic reminder without broadening the supplied batch."""
    resolved_now = _resolve_now_utc(now_utc)
    supplied = tuple(
        sorted(matches, key=lambda item: (item.starts_at_utc, item.match_id))
    )
    supplied_deadlines = tuple(
        sorted(deadlines, key=lambda item: (item.deadline_at_utc, item.kind))
    )
    if not supplied and not supplied_deadlines:
        raise NoOpenPredictionRemindersError(
            "В автоматическом напоминании нет прогнозов."
        )
    with database_connection(database_path) as connection:
        contest = _load_contest(
            connection,
            contest_id=contest_id,
            telegram_chat_id=telegram_chat_id,
        )
        metadata = _load_match_metadata(
            connection,
            contest_id=contest_id,
            match_ids=tuple(item.match_id for item in supplied),
            now_utc=resolved_now,
        )
        actionable = tuple(
            _MatchReminder(
                match_id=item.match_id,
                starts_at_utc=item.starts_at_utc,
                home_team_name=item.home_team_name,
                away_team_name=item.away_team_name,
                tie_id=metadata[item.match_id][0],
                is_two_legged=metadata[item.match_id][1],
                leg_number=metadata[item.match_id][2],
            )
            for item in supplied
            if item.match_id in metadata and item.starts_at_utc > resolved_now
        )
        tournament_by_kind = {
            item.kind: item
            for item in _load_tournament_reminders(contest, resolved_now)
        }
        actionable_tournament = tuple(
            tournament_by_kind[item.kind]
            for item in supplied_deadlines
            if item.kind in tournament_by_kind
            and tournament_by_kind[item.kind].deadline_at == item.deadline_at_utc
        )
        if not actionable and not actionable_tournament:
            raise NoOpenPredictionRemindersError(
                "Прогнозы автоматического напоминания уже недоступны."
            )
        recipients = _filter_missing_recipients(
            connection,
            contest_id=contest_id,
            matches=actionable,
            tournament=actionable_tournament,
            recipients=eligible_recipients,
        )
    return _render_message(
        telegram_chat_id=int(contest["telegram_chat_id"]),
        contest_name=contest_name,
        tournament=actionable_tournament,
        matches=actionable,
        recipients=recipients,
        max_message_length=max_message_length,
    )


def _load_contest(connection, *, contest_id: int, telegram_chat_id: int):
    row = connection.execute(
        """
        SELECT contests.name, contests.template_key, contests.is_active,
               contests.champion_prediction_enabled,
               contests.champion_prediction_deadline_at,
               chats.telegram_chat_id,
               swiss.enabled AS swiss_enabled,
               swiss.deadline_at AS swiss_deadline_at,
               swiss.direct_qualifier_count,
               swiss.elimination_qualifier_count
        FROM contests
        JOIN chats ON chats.id = contests.chat_id
        LEFT JOIN swiss_stage_prediction_settings AS swiss
          ON swiss.contest_id = contests.id
        WHERE contests.id = ? AND chats.telegram_chat_id = ?
        """,
        (contest_id, telegram_chat_id),
    ).fetchone()
    if row is None:
        raise ContestNotFoundError("Конкурс не найден в этом чате.")
    if not bool(row["is_active"]):
        raise ContestCompletedError(
            "Для завершённого конкурса нельзя публиковать напоминания."
        )
    return row


def _load_manual_matches(
    connection, contest_id: int, now_utc: datetime
) -> tuple[_MatchReminder, ...]:
    rows = connection.execute(
        """
        SELECT matches.id, matches.starts_at_utc, matches.tie_id,
               matches.leg_number, COALESCE(ties.is_two_legged, 0) AS is_two_legged,
               home.name AS home_team_name, away.name AS away_team_name
        FROM matches
        JOIN stages ON stages.id = matches.stage_id
        JOIN competitions ON competitions.id = stages.competition_id
        LEFT JOIN ties ON ties.id = matches.tie_id
        JOIN teams AS home ON home.id = matches.home_team_id
        JOIN teams AS away ON away.id = matches.away_team_id
        WHERE competitions.contest_id = ? AND matches.status = 'scheduled'
        ORDER BY matches.starts_at_utc, matches.id
        """,
        (contest_id,),
    ).fetchall()
    result: list[_MatchReminder] = []
    for row in rows:
        starts_at = _parse_datetime(
            row["starts_at_utc"], field_name=f"Match {row['id']} start"
        )
        if starts_at <= now_utc:
            continue
        result.append(
            _MatchReminder(
                match_id=int(row["id"]),
                starts_at_utc=starts_at,
                home_team_name=str(row["home_team_name"]),
                away_team_name=str(row["away_team_name"]),
                tie_id=int(row["tie_id"]) if row["tie_id"] is not None else None,
                is_two_legged=bool(row["is_two_legged"]),
                leg_number=int(row["leg_number"])
                if row["leg_number"] is not None
                else None,
            )
        )
    return tuple(result)


def _load_match_metadata(
    connection,
    *,
    contest_id: int,
    match_ids: tuple[int, ...],
    now_utc: datetime,
) -> dict[int, tuple[int | None, bool, int | None]]:
    if not match_ids:
        return {}
    marks = ",".join("?" for _ in match_ids)
    rows = connection.execute(
        f"""
        SELECT matches.id, matches.tie_id, matches.leg_number,
               matches.starts_at_utc, COALESCE(ties.is_two_legged, 0) AS is_two_legged
        FROM matches
        JOIN stages ON stages.id = matches.stage_id
        JOIN competitions ON competitions.id = stages.competition_id
        LEFT JOIN ties ON ties.id = matches.tie_id
        WHERE competitions.contest_id = ? AND matches.id IN ({marks})
          AND matches.status = 'scheduled'
        """,
        (contest_id, *match_ids),
    ).fetchall()
    return {
        int(row["id"]): (
            int(row["tie_id"]) if row["tie_id"] is not None else None,
            bool(row["is_two_legged"]),
            int(row["leg_number"]) if row["leg_number"] is not None else None,
        )
        for row in rows
        if _parse_datetime(row["starts_at_utc"], field_name=f"Match {row['id']} start")
        > now_utc
    }


def _load_tournament_reminders(
    contest, now_utc: datetime
) -> tuple[_TournamentReminder, ...]:
    result: list[_TournamentReminder] = []
    swiss_deadline = _open_deadline(
        enabled=contest["swiss_enabled"],
        value=contest["swiss_deadline_at"],
        now_utc=now_utc,
        field_name="Swiss prediction deadline",
    )
    if swiss_deadline is not None:
        stage = (
            "общий этап"
            if contest["template_key"] == CHAMPIONS_LEAGUE_2026_27_TEMPLATE_KEY
            else "швейцарский этап"
        )
        result.append(
            _TournamentReminder(
                kind="swiss",
                label=f"🔮 <b>Прогноз на {stage}</b>",
                deadline_at=swiss_deadline,
                direct_count=int(contest["direct_qualifier_count"]),
                elimination_count=int(contest["elimination_qualifier_count"]),
            )
        )
    champion_deadline = _open_deadline(
        enabled=contest["champion_prediction_enabled"],
        value=contest["champion_prediction_deadline_at"],
        now_utc=now_utc,
        field_name="Champion prediction deadline",
    )
    if champion_deadline is not None:
        result.append(
            _TournamentReminder(
                "champion", "🏆 <b>Прогноз на чемпиона</b>", champion_deadline
            )
        )
    return tuple(result)


def _load_eligible_recipients(
    connection, contest_id: int
) -> tuple[ReminderMentionRecipient, ...]:
    rows = connection.execute(
        """
        WITH participants(user_id) AS (
          SELECT mp.user_id FROM match_predictions mp
          JOIN matches m ON m.id = mp.match_id JOIN stages s ON s.id = m.stage_id
          JOIN competitions c ON c.id = s.competition_id WHERE c.contest_id = ?
          UNION
          SELECT tp.user_id FROM tie_predictions tp
          JOIN ties t ON t.id = tp.tie_id JOIN stages s ON s.id = t.stage_id
          JOIN competitions c ON c.id = s.competition_id WHERE c.contest_id = ?
          UNION SELECT user_id FROM champion_predictions WHERE contest_id = ?
          UNION SELECT user_id FROM swiss_stage_predictions WHERE contest_id = ?
        )
        SELECT users.id AS user_id, users.telegram_user_id,
               users.first_name, users.last_name
        FROM contests
        JOIN participants ON 1 = 1
        JOIN users ON users.id = participants.user_id
        JOIN chat_user_prediction_reminder_preferences preferences
          ON preferences.chat_id = contests.chat_id AND preferences.user_id = users.id
        WHERE contests.id = ? AND preferences.mention_in_prediction_reminders = 1
        ORDER BY LOWER(users.first_name), LOWER(COALESCE(users.last_name, '')),
                 users.telegram_user_id
        """,
        (contest_id, contest_id, contest_id, contest_id, contest_id),
    ).fetchall()
    return tuple(
        ReminderMentionRecipient(
            user_id=int(row["user_id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            first_name=str(row["first_name"]),
            last_name=str(row["last_name"]) if row["last_name"] is not None else None,
        )
        for row in rows
    )


def _filter_missing_recipients(
    connection,
    *,
    contest_id: int,
    matches: Sequence[_MatchReminder],
    tournament: Sequence[_TournamentReminder],
    recipients: Sequence[ReminderMentionRecipient],
) -> tuple[ReminderMentionRecipient, ...]:
    if not recipients:
        return ()
    user_ids = tuple(dict.fromkeys(item.user_id for item in recipients))
    user_marks = ",".join("?" for _ in user_ids)
    match_ids = tuple(dict.fromkeys(item.match_id for item in matches))
    scores: set[tuple[int, int]] = set()
    if match_ids:
        match_marks = ",".join("?" for _ in match_ids)
        scores = {
            (int(row["match_id"]), int(row["user_id"]))
            for row in connection.execute(
                f"SELECT match_id, user_id FROM match_predictions "
                f"WHERE match_id IN ({match_marks}) AND user_id IN ({user_marks})",
                (*match_ids, *user_ids),
            ).fetchall()
        }
    tie_ids = tuple(
        dict.fromkeys(item.tie_id for item in matches if item.tie_id is not None)
    )
    ties: set[tuple[int, int]] = set()
    if tie_ids:
        tie_marks = ",".join("?" for _ in tie_ids)
        ties = {
            (int(row["tie_id"]), int(row["user_id"]))
            for row in connection.execute(
                f"SELECT tie_id, user_id FROM tie_predictions "
                f"WHERE tie_id IN ({tie_marks}) AND user_id IN ({user_marks})",
                (*tie_ids, *user_ids),
            ).fetchall()
        }
    champion_users: set[int] = set()
    if any(item.kind == "champion" for item in tournament):
        champion_users = {
            int(row["user_id"])
            for row in connection.execute(
                f"SELECT user_id FROM champion_predictions "
                f"WHERE contest_id = ? AND user_id IN ({user_marks})",
                (contest_id, *user_ids),
            ).fetchall()
        }
    swiss = next((item for item in tournament if item.kind == "swiss"), None)
    complete_swiss: set[int] = set()
    if swiss is not None:
        rows = connection.execute(
            f"""
            SELECT predictions.user_id,
              SUM(CASE WHEN selections.category = 'direct' THEN 1 ELSE 0 END) direct_count,
              SUM(CASE WHEN selections.category = 'elimination' THEN 1 ELSE 0 END) elimination_count
            FROM swiss_stage_predictions predictions
            LEFT JOIN swiss_stage_prediction_selections selections
              ON selections.prediction_id = predictions.id
             AND selections.contest_id = predictions.contest_id
            WHERE predictions.contest_id = ? AND predictions.user_id IN ({user_marks})
            GROUP BY predictions.user_id
            """,
            (contest_id, *user_ids),
        ).fetchall()
        complete_swiss = {
            int(row["user_id"])
            for row in rows
            if int(row["direct_count"] or 0) == swiss.direct_count
            and int(row["elimination_count"] or 0) == swiss.elimination_count
        }

    result: list[ReminderMentionRecipient] = []
    seen: set[int] = set()
    for recipient in sorted(
        recipients,
        key=lambda item: (_display_name(item).casefold(), item.telegram_user_id),
    ):
        if recipient.telegram_user_id in seen:
            continue
        user_id = recipient.user_id
        missing_match = any(
            (match.match_id, user_id) not in scores
            or (
                _requires_tie_prediction(match)
                and match.tie_id is not None
                and (match.tie_id, user_id) not in ties
            )
            for match in matches
        )
        missing_long_term = (
            any(item.kind == "champion" for item in tournament)
            and user_id not in champion_users
        ) or (swiss is not None and user_id not in complete_swiss)
        if missing_match or missing_long_term:
            result.append(recipient)
            seen.add(recipient.telegram_user_id)
    return tuple(result)


def _requires_tie_prediction(match: _MatchReminder) -> bool:
    return match.tie_id is not None and (
        not match.is_two_legged or match.leg_number == 1
    )


def _render_message(
    *,
    telegram_chat_id: int,
    contest_name: str,
    tournament: Sequence[_TournamentReminder],
    matches: Sequence[_MatchReminder],
    recipients: Sequence[ReminderMentionRecipient],
    max_message_length: int,
) -> PredictionReminderMessage:
    if max_message_length <= 0:
        raise ValueError("Maximum message length must be positive.")
    if not tournament and not matches:
        raise NoOpenPredictionRemindersError(
            "Нет открытых прогнозов или предстоящих матчей для публикации."
        )
    fragments: list[str] = []
    if tournament:
        fragments.append("<p><b>Турнирные прогнозы</b></p>")
        fragments.extend(
            f"<p>{item.label}<br>Дедлайн: {_format_datetime(item.deadline_at)}</p>"
            for item in tournament
        )
    if matches:
        fragments.append("<p><b>Предстоящие матчи</b></p>")
        fragments.extend(
            "<p>⚽ <b>"
            f"{escape_rich_text(item.home_team_name)} — {escape_rich_text(item.away_team_name)}"
            f"</b><br>Начало: {_format_datetime(item.starts_at_utc)}</p>"
            for item in matches
        )
    first_prefix = (
        "<p>⏰ <b>Не забудьте сделать или дозаполнить прогнозы</b></p>"
        f"<p>Конкурс: «{escape_rich_text(contest_name)}»</p>"
    )
    next_prefix = (
        "<p>⏰ <b>Напоминания — продолжение</b></p>"
        f"<p>Конкурс: «{escape_rich_text(contest_name)}»</p>"
    )
    fragments.extend(
        _mention_fragments(
            recipients,
            max_fragment_length=max_message_length
            - max(len(first_prefix), len(next_prefix)),
        )
    )
    parts = _pack_fragments(
        fragments,
        first_prefix=first_prefix,
        next_prefix=next_prefix,
        max_message_length=max_message_length,
    )
    return PredictionReminderMessage(
        telegram_chat_id=telegram_chat_id,
        html_parts=parts,
        reminder_count=len(tournament) + len(matches),
        match_count=len(matches),
        mention_count=len(recipients),
    )


def _mention_fragments(
    recipients: Sequence[ReminderMentionRecipient],
    *,
    max_fragment_length: int,
) -> tuple[str, ...]:
    if not recipients:
        return ()
    opening, closing = "<p><b>Ждём прогнозы от:</b><br>", "</p>"
    parts: list[str] = []
    anchors: list[str] = []
    length = len(opening) + len(closing)
    for recipient in recipients:
        anchor = _mention_anchor(recipient)
        extra = len(anchor) + (2 if anchors else 0)
        if anchors and length + extra > max_fragment_length:
            parts.append(opening + ", ".join(anchors) + closing)
            anchors, length, extra = [], len(opening) + len(closing), len(anchor)
        if length + extra > max_fragment_length:
            raise PredictionReminderMessageTooLongError(
                "Одно упоминание не помещается в сообщение."
            )
        anchors.append(anchor)
        length += extra
    if anchors:
        parts.append(opening + ", ".join(anchors) + closing)
    return tuple(parts)


def _pack_fragments(
    fragments: Sequence[str],
    *,
    first_prefix: str,
    next_prefix: str,
    max_message_length: int,
) -> tuple[str, ...]:
    if len(first_prefix) > max_message_length:
        raise PredictionReminderMessageTooLongError(
            "Все напоминания не помещаются в одно сообщение: заголовок слишком длинный."
        )
    result: list[str] = []
    current = first_prefix
    for fragment in fragments:
        if len(current) + len(fragment) <= max_message_length:
            current += fragment
            continue
        if current not in (first_prefix, next_prefix):
            result.append(current)
            current = next_prefix
        if len(current) + len(fragment) > max_message_length:
            raise PredictionReminderMessageTooLongError(
                "Все напоминания не помещаются в одно сообщение: один пункт слишком длинный."
            )
        current += fragment
    result.append(current)
    return tuple(result)


def _mention_anchor(recipient: ReminderMentionRecipient) -> str:
    label = _display_name(recipient)[:_MENTION_LABEL_MAX_LENGTH].strip() or "Участник"
    return (
        f'<a href="tg://user?id={recipient.telegram_user_id}">'
        f"{escape_rich_text(label)}</a>"
    )


def _display_name(recipient: ReminderMentionRecipient) -> str:
    return (
        " ".join(
            value.strip()
            for value in (recipient.first_name, recipient.last_name)
            if value and value.strip()
        )
        or "Участник"
    )


def _recipient_from_worker(recipient: ReminderRecipient) -> ReminderMentionRecipient:
    return ReminderMentionRecipient(
        user_id=recipient.user_id,
        telegram_user_id=recipient.telegram_user_id,
        first_name=recipient.first_name,
        last_name=recipient.last_name,
    )


def _open_deadline(
    *, enabled: object, value: object, now_utc: datetime, field_name: str
) -> datetime | None:
    if not bool(enabled) or value is None:
        return None
    deadline = _parse_datetime(value, field_name=field_name)
    return deadline if deadline > now_utc else None


def _parse_datetime(value: object, *, field_name: str) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
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

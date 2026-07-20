from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

from app.contest_service import get_contest_details
from app.database import database_connection
from app.publication_outbox import (
    ClaimedPublication,
    StalePublicationRevision,
    resolve_service_time,
)


PUBLICATION_MAX_MESSAGE_LENGTH = 3900


def render_publication_messages(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    max_message_length: int = PUBLICATION_MAX_MESSAGE_LENGTH,
    now_utc: datetime | None = None,
) -> tuple[str, ...]:
    if publication.desired_action == "withdraw":
        return ()
    if publication.publication_type == "match_result":
        return _render_match_result(
            database_path=database_path,
            match_id=publication.entity_id,
            max_message_length=max_message_length,
        )
    if publication.publication_type == "champion_result":
        return _render_champion_result(
            database_path=database_path,
            contest_id=publication.contest_id,
            max_message_length=max_message_length,
            now_utc=now_utc,
        )
    if publication.publication_type == "contest_completed":
        return _render_contest_completed(
            database_path=database_path,
            contest_id=publication.contest_id,
            max_message_length=max_message_length,
        )
    raise RuntimeError(f"Unknown publication type: {publication.publication_type}")


def get_publication_chat_id(*, database_path: Path, contest_id: int) -> int:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT chats.telegram_chat_id
            FROM contests
            JOIN chats ON chats.id = contests.chat_id
            WHERE contests.id = ?
            """,
            (contest_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("Contest chat was not found for publication.")
    return int(row["telegram_chat_id"])


def withdrawal_fallback_text(publication: ClaimedPublication) -> str:
    if publication.publication_type == "match_result":
        return "Эта публикация больше не актуальна: матч удалён из конкурса."
    return "Эта публикация больше не актуальна."


def retired_part_fallback_text() -> str:
    return "Продолжение этой публикации больше не актуально."


def _render_match_result(
    *, database_path: Path, match_id: int, max_message_length: int
) -> tuple[str, ...]:
    with database_connection(database_path) as connection:
        match = connection.execute(
            """
            SELECT
                contests.name AS contest_name,
                home_team.name AS home_team_name,
                away_team.name AS away_team_name,
                matches.home_score_final,
                matches.away_score_final,
                advancing_team.name AS advancing_team_name
            FROM matches
            JOIN stages ON stages.id = matches.stage_id
            JOIN competitions ON competitions.id = stages.competition_id
            JOIN contests ON contests.id = competitions.contest_id
            JOIN teams AS home_team ON home_team.id = matches.home_team_id
            JOIN teams AS away_team ON away_team.id = matches.away_team_id
            LEFT JOIN ties ON ties.id = matches.tie_id
            LEFT JOIN teams AS advancing_team
                ON advancing_team.id = ties.advancing_team_id
            WHERE matches.id = ?
            """,
            (match_id,),
        ).fetchone()
        if (
            match is None
            or match["home_score_final"] is None
            or match["away_score_final"] is None
        ):
            raise RuntimeError("Finished match data was not found for publication.")

        predictions = connection.execute(
            """
            SELECT
                users.id AS user_id,
                users.first_name,
                users.last_name,
                match_predictions.predicted_home_score,
                match_predictions.predicted_away_score,
                predicted_advancing_team.name AS advancing_team_name,
                COALESCE((
                    SELECT SUM(match_prediction_scores.points)
                    FROM match_prediction_scores
                    WHERE match_prediction_scores.match_prediction_id =
                        match_predictions.id
                ), 0) AS match_points,
                COALESCE((
                    SELECT SUM(tie_prediction_scores.points)
                    FROM tie_prediction_scores
                    WHERE tie_prediction_scores.tie_prediction_id =
                        tie_predictions.id
                ), 0) AS advancing_points
            FROM match_predictions
            JOIN users ON users.id = match_predictions.user_id
            JOIN matches ON matches.id = match_predictions.match_id
            LEFT JOIN tie_predictions
                ON tie_predictions.tie_id = matches.tie_id
               AND tie_predictions.user_id = match_predictions.user_id
            LEFT JOIN teams AS predicted_advancing_team
                ON predicted_advancing_team.id =
                    tie_predictions.predicted_advancing_team_id
            WHERE match_predictions.match_id = ?
            ORDER BY
                users.first_name COLLATE NOCASE ASC,
                COALESCE(users.last_name, '') COLLATE NOCASE ASC,
                users.id ASC
            """,
            (match_id,),
        ).fetchall()

    home_score = int(match["home_score_final"])
    away_score = int(match["away_score_final"])
    title = (
        f"🏁 <b>{escape(str(match['home_team_name']))} — "
        f"{escape(str(match['away_team_name']))}: {home_score}:{away_score}</b>"
    )
    header_lines = [title, f"Конкурс: «{escape(str(match['contest_name']))}»"]
    if home_score == away_score and match["advancing_team_name"] is not None:
        header_lines.append(f"Проходит: {escape(str(match['advancing_team_name']))}")
    header_lines.extend(("", "Результаты прогнозов:"))
    header = "\n".join(header_lines)
    continuation = f"{title}\nПродолжение результатов прогнозов:"

    if not predictions:
        return _split_lines(
            header=header,
            continuation_header=continuation,
            body_lines=("Прогнозов на этот матч не было.",),
            max_message_length=max_message_length,
        )

    body_lines = tuple(_format_match_prediction(row) for row in predictions)
    return _split_lines(
        header=header,
        continuation_header=continuation,
        body_lines=body_lines,
        max_message_length=max_message_length,
    )


def _render_champion_result(
    *,
    database_path: Path,
    contest_id: int,
    max_message_length: int,
    now_utc: datetime | None,
) -> tuple[str, ...]:
    with database_connection(database_path) as connection:
        contest = connection.execute(
            """
            SELECT
                contests.name,
                contests.is_active,
                contests.champion_prediction_enabled,
                contests.champion_prediction_deadline_at,
                contests.champion_prediction_points,
                champion.name AS champion_name
            FROM contests
            LEFT JOIN teams AS champion
                ON champion.id = contests.champion_team_id
            WHERE contests.id = ?
            """,
            (contest_id,),
        ).fetchone()
        if contest is None or contest["champion_name"] is None:
            raise RuntimeError("Champion data was not found for publication.")
        if bool(contest["is_active"]):
            deadline_value = contest["champion_prediction_deadline_at"]
            if not bool(contest["champion_prediction_enabled"]):
                raise StalePublicationRevision(
                    "Champion prediction was disabled before rendering."
                )
            if deadline_value is None:
                raise StalePublicationRevision(
                    "Champion prediction deadline is not configured."
                )
            deadline = datetime.fromisoformat(
                str(deadline_value).replace("Z", "+00:00")
            )
            if deadline.tzinfo is None or deadline.utcoffset() is None:
                raise RuntimeError(
                    "Champion prediction deadline does not include a timezone."
                )
            if deadline > resolve_service_time(now_utc):
                raise StalePublicationRevision(
                    "Champion prediction is still open and cannot be rendered."
                )

        predictions = connection.execute(
            """
            SELECT
                users.id AS user_id,
                users.first_name,
                users.last_name,
                predicted_team.name AS predicted_team_name,
                champion_predictions.predicted_team_id,
                contests.champion_team_id,
                contests.champion_prediction_points
            FROM champion_predictions
            JOIN users ON users.id = champion_predictions.user_id
            JOIN teams AS predicted_team
                ON predicted_team.id = champion_predictions.predicted_team_id
            JOIN contests ON contests.id = champion_predictions.contest_id
            WHERE champion_predictions.contest_id = ?
            ORDER BY
                users.first_name COLLATE NOCASE ASC,
                COALESCE(users.last_name, '') COLLATE NOCASE ASC,
                users.id ASC
            """,
            (contest_id,),
        ).fetchall()

    title = f"🏆 <b>Чемпион турнира — {escape(str(contest['champion_name']))}</b>"
    header = (
        f"{title}\nКонкурс: «{escape(str(contest['name']))}»\n\nРезультаты прогнозов:"
    )
    continuation = f"{title}\nПродолжение результатов прогнозов:"
    if not predictions:
        body_lines = ("Никто не сделал прогноз на чемпиона.",)
    else:
        body_lines = tuple(_format_champion_prediction(row) for row in predictions)
    return _split_lines(
        header=header,
        continuation_header=continuation,
        body_lines=body_lines,
        max_message_length=max_message_length,
    )


def _render_contest_completed(
    *, database_path: Path, contest_id: int, max_message_length: int
) -> tuple[str, ...]:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT contests.name, chats.telegram_chat_id
            FROM contests
            JOIN chats ON chats.id = contests.chat_id
            WHERE contests.id = ?
            """,
            (contest_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("Contest was not found for final publication.")

    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=int(row["telegram_chat_id"]),
        contest_id=contest_id,
    )
    title = f"🍀 <b>Конкурс «{escape(str(row['name']))}» завершён</b>"
    header = f"{title}\n\nИтоговый рейтинг:"
    continuation = f"{title}\nПродолжение итогового рейтинга:"
    if not details.leaderboard:
        body_lines = ("В рейтинге нет участников.",)
    else:
        rating_lines = tuple(
            _format_leaderboard_entry(entry) for entry in details.leaderboard
        )
        winners = tuple(
            entry.participant_name for entry in details.leaderboard if entry.place == 1
        )
        winner_label = "Победитель" if len(winners) == 1 else "Победители"
        body_lines = rating_lines + (
            "",
            f"{winner_label} — {escape(', '.join(winners))}!",
        )
    return _split_lines(
        header=header,
        continuation_header=continuation,
        body_lines=body_lines,
        max_message_length=max_message_length,
    )


def _format_match_prediction(row) -> str:
    participant = _participant_name(row)
    home_score = int(row["predicted_home_score"])
    away_score = int(row["predicted_away_score"])
    prediction = f"{escape(participant)} — {home_score}:{away_score}"
    if home_score == away_score and row["advancing_team_name"] is not None:
        prediction += f", проходит {escape(str(row['advancing_team_name']))}"
    points = int(row["match_points"]) + int(row["advancing_points"])
    return f"• {prediction} — {points} {_points_label(points)}"


def _format_champion_prediction(row) -> str:
    participant = escape(_participant_name(row))
    team = escape(str(row["predicted_team_name"]))
    points = (
        int(row["champion_prediction_points"])
        if int(row["predicted_team_id"]) == int(row["champion_team_id"])
        else 0
    )
    formatted_points = f"+{points}" if points > 0 else "0"
    return f"• {participant} — {team} — {formatted_points} {_points_label(points)}"


def _format_leaderboard_entry(entry) -> str:
    place_label = {1: "🥇", 2: "🥈", 3: "🥉"}.get(
        entry.place,
        f"{entry.place}.",
    )
    return (
        f"{place_label} {escape(entry.participant_name)} — "
        f"{entry.total_points} {_points_label(entry.total_points)}"
    )


def _participant_name(row) -> str:
    name = " ".join(
        part
        for part in (
            str(row["first_name"] or "").strip(),
            str(row["last_name"] or "").strip(),
        )
        if part
    )
    return name or f"Участник {int(row['user_id'])}"


def _points_label(points: int) -> str:
    absolute = abs(points)
    if absolute % 100 in range(11, 15):
        return "баллов"
    if absolute % 10 == 1:
        return "балл"
    if absolute % 10 in range(2, 5):
        return "балла"
    return "баллов"


def _split_lines(
    *,
    header: str,
    continuation_header: str,
    body_lines: tuple[str, ...],
    max_message_length: int,
) -> tuple[str, ...]:
    if (
        len(header) > max_message_length
        or len(continuation_header) > max_message_length
    ):
        raise ValueError("Maximum Telegram message length is too small for the header.")

    messages: list[str] = []
    current = header
    for line in body_lines:
        candidate = f"{current}\n{line}"
        if len(candidate) <= max_message_length:
            current = candidate
            continue
        messages.append(current)
        current = f"{continuation_header}\n{line}"
        if len(current) > max_message_length:
            raise ValueError("A publication line does not fit into one message.")
    messages.append(current)
    return tuple(messages)

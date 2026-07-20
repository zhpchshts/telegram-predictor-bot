from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.contest_service import (
    LeaderboardTiebreakReason,
    get_contest_details,
    resolve_leaderboard_tiebreak_reason,
)
from app.database import database_connection
from app.publication_outbox import (
    ClaimedPublication,
    StalePublicationRevision,
    resolve_service_time,
)
from app.rich_publications import (
    RICH_MESSAGE_MAX_LENGTH,
    RICH_MESSAGE_MAX_TABLE_ROWS,
    escape_rich_text,
    format_awarded_points,
    split_rich_table_messages,
    table_row,
)


PUBLICATION_MAX_MESSAGE_LENGTH = RICH_MESSAGE_MAX_LENGTH
PUBLICATION_MAX_TABLE_ROWS = RICH_MESSAGE_MAX_TABLE_ROWS
MATCH_RESULT_COLUMN_SPANS = (4, 3, 1)
CHAMPION_RESULT_COLUMN_SPANS = (3, 3, 1)
LEADERBOARD_COLUMN_SPANS = (1, 5, 1)


def render_publication_messages(
    *,
    database_path: Path,
    publication: ClaimedPublication,
    max_message_length: int = PUBLICATION_MAX_MESSAGE_LENGTH,
    max_table_rows: int = PUBLICATION_MAX_TABLE_ROWS,
    now_utc: datetime | None = None,
) -> tuple[str, ...]:
    if publication.desired_action == "withdraw":
        return ()
    if publication.publication_type == "match_result":
        return _render_match_result(
            database_path=database_path,
            match_id=publication.entity_id,
            max_message_length=max_message_length,
            max_table_rows=max_table_rows,
        )
    if publication.publication_type == "champion_result":
        return _render_champion_result(
            database_path=database_path,
            contest_id=publication.contest_id,
            max_message_length=max_message_length,
            max_table_rows=max_table_rows,
            now_utc=now_utc,
        )
    if publication.publication_type == "champion_predictions":
        return _render_champion_predictions(
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
            max_table_rows=max_table_rows,
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
        return "<p>Эта публикация больше не актуальна: матч удалён из конкурса.</p>"
    return "<p>Эта публикация больше не актуальна.</p>"


def retired_part_fallback_text() -> str:
    return "<p>Продолжение этой публикации больше не актуально.</p>"


def _render_match_result(
    *,
    database_path: Path,
    match_id: int,
    max_message_length: int,
    max_table_rows: int,
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
                match_points + advancing_points DESC,
                users.first_name COLLATE NOCASE ASC,
                COALESCE(users.last_name, '') COLLATE NOCASE ASC,
                users.id ASC
            """,
            (match_id,),
        ).fetchall()

    home_score = int(match["home_score_final"])
    away_score = int(match["away_score_final"])
    title = (
        f"<p><b>🏁 {escape_rich_text(match['home_team_name'])} "
        f"{home_score}:{away_score} "
        f"{escape_rich_text(match['away_team_name'])}</b></p>"
    )
    advancement = ""
    if home_score == away_score and match["advancing_team_name"] is not None:
        advancement = (
            "<p>В следующий раунд проходит "
            f"{escape_rich_text(match['advancing_team_name'])}</p>"
        )
    contest_line = f"<p>Конкурс: «{escape_rich_text(match['contest_name'])}»</p>"
    first_header = f"{title}{advancement}{contest_line}"
    continuation_header = f"{title}{contest_line}"

    if not predictions:
        return _single_message(
            f"{first_header}<p>Прогнозов на этот матч не было.</p>",
            max_message_length=max_message_length,
        )

    rows = tuple(_format_match_prediction_row(row) for row in predictions)
    return split_rich_table_messages(
        first_header=first_header,
        continuation_header=continuation_header,
        first_caption="Очки за матч",
        continuation_caption="Очки за матч · продолжение",
        column_names=("Участник", "Прогноз", "Очки"),
        alignments=("left", "center", "right"),
        column_spans=MATCH_RESULT_COLUMN_SPANS,
        rows=rows,
        max_message_length=max_message_length,
        max_table_rows=max_table_rows,
    )


def _render_champion_result(
    *,
    database_path: Path,
    contest_id: int,
    max_message_length: int,
    max_table_rows: int,
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
            _validate_champion_publication_deadline(contest, now_utc=now_utc)

        predictions = connection.execute(
            """
            SELECT
                users.id AS user_id,
                users.first_name,
                users.last_name,
                predicted_team.name AS predicted_team_name,
                CASE
                    WHEN champion_predictions.predicted_team_id =
                        contests.champion_team_id
                    THEN contests.champion_prediction_points
                    ELSE 0
                END AS awarded_points
            FROM champion_predictions
            JOIN users ON users.id = champion_predictions.user_id
            JOIN teams AS predicted_team
                ON predicted_team.id = champion_predictions.predicted_team_id
            JOIN contests ON contests.id = champion_predictions.contest_id
            WHERE champion_predictions.contest_id = ?
            ORDER BY
                awarded_points DESC,
                users.first_name COLLATE NOCASE ASC,
                COALESCE(users.last_name, '') COLLATE NOCASE ASC,
                users.id ASC
            """,
            (contest_id,),
        ).fetchall()

    title = (
        "<p><b>🏆 Чемпион турнира — "
        f"{escape_rich_text(contest['champion_name'])}</b></p>"
    )
    contest_line = f"<p>Конкурс: «{escape_rich_text(contest['name'])}»</p>"
    header = f"{title}{contest_line}"
    if not predictions:
        return _single_message(
            f"{header}<p>Никто не сделал прогноз на чемпиона.</p>",
            max_message_length=max_message_length,
        )

    rows = tuple(_format_champion_prediction_row(row) for row in predictions)
    return split_rich_table_messages(
        first_header=header,
        continuation_header=header,
        first_caption="Очки за прогноз",
        continuation_caption="Очки за прогноз · продолжение",
        column_names=("Участник", "Прогноз", "Очки"),
        alignments=("left", "center", "right"),
        column_spans=CHAMPION_RESULT_COLUMN_SPANS,
        rows=rows,
        max_message_length=max_message_length,
        max_table_rows=max_table_rows,
    )


def _render_champion_predictions(
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
                name,
                is_active,
                champion_prediction_enabled,
                champion_prediction_deadline_at
            FROM contests
            WHERE id = ?
            """,
            (contest_id,),
        ).fetchone()
        if contest is None:
            raise RuntimeError(
                "Contest was not found for champion predictions publication."
            )
        _validate_champion_publication_deadline(contest, now_utc=now_utc)
        predictions = connection.execute(
            """
            SELECT
                users.id AS user_id,
                users.first_name,
                users.last_name,
                teams.name AS predicted_team_name
            FROM champion_predictions
            JOIN users ON users.id = champion_predictions.user_id
            JOIN teams ON teams.id = champion_predictions.predicted_team_id
            WHERE champion_predictions.contest_id = ?
            ORDER BY
                users.first_name ASC,
                COALESCE(users.last_name, '') ASC,
                users.id ASC
            """,
            (contest_id,),
        ).fetchall()

    header = (
        "<p>🏆 <b>Прогнозы на чемпиона</b></p>"
        f"<p>Конкурс: «{escape_rich_text(contest['name'])}»</p>"
    )
    lines = tuple(
        "• "
        f"{escape_rich_text(_participant_name(row))} — "
        f"{escape_rich_text(row['predicted_team_name'])}"
        for row in predictions
    )
    return _split_lines(
        header=header,
        lines=lines,
        empty_text="Никто не сделал прогноз на чемпиона.",
        max_message_length=max_message_length,
    )


def _validate_champion_publication_deadline(contest, *, now_utc) -> None:
    deadline_value = contest["champion_prediction_deadline_at"]
    if not bool(contest["champion_prediction_enabled"]):
        raise StalePublicationRevision(
            "Champion prediction was disabled before rendering."
        )
    if deadline_value is None:
        raise StalePublicationRevision(
            "Champion prediction deadline is not configured."
        )
    deadline = datetime.fromisoformat(str(deadline_value).replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise RuntimeError("Champion prediction deadline does not include a timezone.")
    if bool(contest["is_active"]) and deadline > resolve_service_time(now_utc):
        raise StalePublicationRevision(
            "Champion prediction is still open and cannot be rendered."
        )


def _render_contest_completed(
    *,
    database_path: Path,
    contest_id: int,
    max_message_length: int,
    max_table_rows: int,
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
    title = f"<p><b>🍀 Конкурс «{escape_rich_text(row['name'])}» завершён</b></p>"
    if not details.leaderboard:
        return _single_message(
            f"{title}<p>В рейтинге нет участников.</p>",
            max_message_length=max_message_length,
        )

    _validate_leaderboard_invariant(details.leaderboard)
    winner = details.leaderboard[0]
    winner_line = (
        f"<p><b>🏆 Победитель — {escape_rich_text(winner.participant_name)}</b></p>"
    )
    explanation = ""
    if len(details.leaderboard) > 1:
        reason = resolve_leaderboard_tiebreak_reason(
            winner,
            details.leaderboard[1],
        )
        if reason is not None:
            explanation = (
                f"<p>{_format_tiebreak_explanation(winner.participant_name, reason)}"
                "</p>"
            )

    rows = tuple(_format_leaderboard_row(entry) for entry in details.leaderboard)
    return split_rich_table_messages(
        first_header=f"{title}{winner_line}{explanation}",
        continuation_header=title,
        first_caption="Итоговый рейтинг",
        continuation_caption="Итоговый рейтинг · продолжение",
        column_names=("Место", "Участник", "Очки"),
        alignments=("center", "left", "right"),
        column_spans=LEADERBOARD_COLUMN_SPANS,
        rows=rows,
        max_message_length=max_message_length,
        max_table_rows=max_table_rows,
    )


def _format_match_prediction_row(row) -> str:
    home_score = int(row["predicted_home_score"])
    away_score = int(row["predicted_away_score"])
    prediction = f"{home_score}:{away_score}"
    if home_score == away_score and row["advancing_team_name"] is not None:
        prediction += f" → {escape_rich_text(row['advancing_team_name'])}"
    points = int(row["match_points"]) + int(row["advancing_points"])
    points_text = format_awarded_points(points)
    if points > 0:
        points_text = f"<b>{points_text}</b>"
    return table_row(
        (escape_rich_text(_participant_name(row)), prediction, points_text),
        alignments=("left", "center", "right"),
        column_spans=MATCH_RESULT_COLUMN_SPANS,
    )


def _format_champion_prediction_row(row) -> str:
    points = int(row["awarded_points"])
    points_text = format_awarded_points(points)
    if points > 0:
        points_text = f"<b>{points_text}</b>"
    return table_row(
        (
            escape_rich_text(_participant_name(row)),
            escape_rich_text(row["predicted_team_name"]),
            points_text,
        ),
        alignments=("left", "center", "right"),
        column_spans=CHAMPION_RESULT_COLUMN_SPANS,
    )


def _format_leaderboard_row(entry) -> str:
    place = {1: "🥇", 2: "🥈", 3: "🥉"}.get(entry.place, str(entry.place))
    participant = escape_rich_text(entry.participant_name)
    points = str(entry.total_points)
    if entry.place == 1:
        participant = f"<b>{participant}</b>"
        points = f"<b>{points}</b>"
    return table_row(
        (place, participant, points),
        alignments=("center", "left", "right"),
        column_spans=LEADERBOARD_COLUMN_SPANS,
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


def _validate_leaderboard_invariant(leaderboard) -> None:
    expected_places = tuple(range(1, len(leaderboard) + 1))
    actual_places = tuple(entry.place for entry in leaderboard)
    if actual_places != expected_places:
        raise RuntimeError(
            "Leaderboard must contain unique consecutive places starting at one."
        )


def _format_tiebreak_explanation(
    winner_name: str,
    reason: LeaderboardTiebreakReason,
) -> str:
    winner = escape_rich_text(winner_name)
    explanations = {
        "exact_score": (
            f"При равенстве очков {winner} занял первое место благодаря "
            "большему количеству точных счетов."
        ),
        "goal_difference": (
            f"При равенстве очков {winner} занял первое место благодаря "
            "большему количеству угаданных разниц мячей."
        ),
        "outcome": (
            f"При равенстве очков {winner} занял первое место благодаря "
            "большему количеству угаданных исходов."
        ),
        "drawn_advancing_team": (
            f"При равенстве очков {winner} занял первое место благодаря "
            "большему количеству правильных прогнозов прошедшей команды."
        ),
        "champion": (
            f"При равенстве очков {winner} занял первое место благодаря "
            "правильному прогнозу чемпиона."
        ),
        "draw": (
            "Все дополнительные показатели участников с первого и второго "
            "места совпали. Победитель определён жребием."
        ),
    }
    return explanations[reason]


def _single_message(
    rich_html: str,
    *,
    max_message_length: int,
) -> tuple[str, ...]:
    if len(rich_html) > max_message_length:
        raise ValueError("Rich Message content does not fit into one message.")
    return (rich_html,)


def _split_lines(
    *,
    header: str,
    lines: tuple[str, ...],
    empty_text: str,
    max_message_length: int,
) -> tuple[str, ...]:
    if not lines:
        return _single_message(
            f"{header}<p>{escape_rich_text(empty_text)}</p>",
            max_message_length=max_message_length,
        )

    messages: list[str] = []
    current_lines: list[str] = []
    for line in lines:
        candidate_lines = (*current_lines, line)
        candidate = f"{header}<p>{'<br>'.join(candidate_lines)}</p>"
        if len(candidate) <= max_message_length:
            current_lines.append(line)
            continue
        if not current_lines:
            raise ValueError("A champion prediction line does not fit in one message.")
        messages.append(f"{header}<p>{'<br>'.join(current_lines)}</p>")
        current_lines = [line]
        if len(f"{header}<p>{line}</p>") > max_message_length:
            raise ValueError("A champion prediction line does not fit in one message.")

    if current_lines:
        messages.append(f"{header}<p>{'<br>'.join(current_lines)}</p>")
    return tuple(messages)

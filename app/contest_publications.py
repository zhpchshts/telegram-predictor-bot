from __future__ import annotations

from datetime import datetime, timezone
import json
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
from app.publication_message_matrix import (
    champion_predictions_insights,
    champion_result_insight,
    contest_completion_insight,
    prediction_display_mode,
    series_result_insights,
    swiss_result_insights,
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
    if publication.publication_type == "swiss_predictions":
        return _render_swiss_predictions(
            database_path=database_path,
            contest_id=publication.contest_id,
            max_message_length=max_message_length,
            now_utc=now_utc,
        )
    if publication.publication_type == "swiss_result":
        return _render_swiss_result(
            database_path=database_path,
            contest_id=publication.contest_id,
            max_message_length=max_message_length,
        )
    if publication.publication_type == "leaderboard_snapshot":
        return _render_intermediate_leaderboard(
            database_path=database_path,
            snapshot_id=publication.entity_id,
            contest_id=publication.contest_id,
            max_message_length=max_message_length,
            max_table_rows=max_table_rows,
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

    if prediction_display_mode(len(predictions)) == "statistics":
        return _render_match_result_statistics(
            header=first_header,
            match=match,
            predictions=predictions,
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

    if prediction_display_mode(len(predictions)) == "statistics":
        vote_counts = _count_named_predictions(
            predictions,
            field_name="predicted_team_name",
        )
        champion_name = str(contest["champion_name"])
        champion_count = vote_counts.get(champion_name, 0)
        insight = champion_result_insight(
            champion_name=champion_name,
            champion_count=champion_count,
            votes=tuple(vote_counts.items()),
            total=len(predictions),
        )
        lines = (
            f"<b>Прогнозов: {len(predictions)}</b>",
            (
                "Чемпиона выбрали: "
                f"{champion_count} ({_format_percent(champion_count, len(predictions))})"
            ),
            escape_rich_text(insight),
        )
        return _split_lines(
            header=header,
            lines=lines,
            empty_text="Никто не сделал прогноз на чемпиона.",
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
    if prediction_display_mode(len(predictions)) == "statistics":
        vote_counts = _count_named_predictions(
            predictions,
            field_name="predicted_team_name",
        )
        sorted_votes = tuple(
            sorted(
                vote_counts.items(),
                key=lambda item: (-item[1], item[0].casefold()),
            )
        )
        favorite, diversity = champion_predictions_insights(
            sorted_votes,
            total=len(predictions),
        )
        lines = (
            f"<b>Прогнозов: {len(predictions)}</b>",
            *(
                f"{escape_rich_text(team_name)} — {count} "
                f"({_format_percent(count, len(predictions))})"
                for team_name, count in sorted_votes
            ),
            escape_rich_text(favorite),
            escape_rich_text(diversity),
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
    if deadline > resolve_service_time(now_utc):
        raise StalePublicationRevision(
            "Champion prediction is still open and cannot be rendered."
        )


def _render_swiss_predictions(
    *,
    database_path: Path,
    contest_id: int,
    max_message_length: int,
    now_utc: datetime | None,
) -> tuple[str, ...]:
    with database_connection(database_path) as connection:
        settings = connection.execute(
            """
            SELECT contests.name, settings.enabled, settings.deadline_at
            FROM swiss_stage_prediction_settings AS settings
            JOIN contests ON contests.id = settings.contest_id
            WHERE settings.contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
        if settings is None:
            raise StalePublicationRevision(
                "Swiss prediction settings were removed before rendering."
            )
        _validate_swiss_publication_deadline(settings, now_utc=now_utc)
        prediction_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM swiss_stage_predictions WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()[0]
        )
        teams = connection.execute(
            """
            SELECT
                teams.name,
                candidates.position,
                COUNT(DISTINCT selections.prediction_id) AS support_count
            FROM contest_teams AS candidates
            JOIN teams ON teams.id = candidates.team_id
            LEFT JOIN swiss_stage_prediction_selections AS selections
                ON selections.contest_id = candidates.contest_id
               AND selections.team_id = candidates.team_id
            WHERE candidates.contest_id = ?
            GROUP BY teams.id, teams.name, candidates.position
            ORDER BY support_count DESC, candidates.position ASC, teams.id ASC
            """,
            (contest_id,),
        ).fetchall()

    header = (
        "<p>🔮 <b>Прогнозы на швейцарский этап</b></p>"
        f"<p>Конкурс: «{escape_rich_text(settings['name'])}»</p>"
    )
    if prediction_count == 0:
        return _single_message(
            f"{header}<p>Прогнозов на швейцарский этап нет.</p>",
            max_message_length=max_message_length,
        )

    lines: list[str] = [f"<b>Прогнозов: {prediction_count}</b>"]
    previous_support: int | None = None
    current_rank = 0
    for index, team in enumerate(teams, start=1):
        support_count = int(team["support_count"])
        if previous_support != support_count:
            current_rank = index
            previous_support = support_count
        lines.append(
            f"{current_rank}. {escape_rich_text(team['name'])} — "
            f"{_format_percent(support_count, prediction_count)}"
        )
    return _split_lines(
        header=header,
        lines=tuple(lines),
        empty_text="Прогнозов на швейцарский этап нет.",
        max_message_length=max_message_length,
    )


def _render_swiss_result(
    *,
    database_path: Path,
    contest_id: int,
    max_message_length: int,
) -> tuple[str, ...]:
    with database_connection(database_path) as connection:
        settings = connection.execute(
            """
            SELECT
                contests.name,
                settings.direct_qualifier_count,
                settings.elimination_qualifier_count
            FROM swiss_stage_prediction_settings AS settings
            JOIN contests ON contests.id = settings.contest_id
            JOIN swiss_stage_results AS result
                ON result.contest_id = settings.contest_id
            WHERE settings.contest_id = ?
            """,
            (contest_id,),
        ).fetchone()
        if settings is None:
            raise RuntimeError("Swiss result data was not found for publication.")
        actual_rows = connection.execute(
            """
            SELECT result.team_id, result.category, teams.name
            FROM swiss_stage_result_selections AS result
            JOIN teams ON teams.id = result.team_id
            JOIN contest_teams AS candidates
                ON candidates.contest_id = result.contest_id
               AND candidates.team_id = result.team_id
            WHERE result.contest_id = ?
            ORDER BY candidates.position ASC, result.team_id ASC
            """,
            (contest_id,),
        ).fetchall()
        prediction_rows = connection.execute(
            """
            SELECT
                predictions.id AS prediction_id,
                selections.team_id,
                selections.category
            FROM swiss_stage_predictions AS predictions
            JOIN swiss_stage_prediction_selections AS selections
                ON selections.prediction_id = predictions.id
            WHERE predictions.contest_id = ?
            ORDER BY predictions.id, selections.team_id
            """,
            (contest_id,),
        ).fetchall()
        prediction_ids = tuple(
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM swiss_stage_predictions WHERE contest_id = ?",
                (contest_id,),
            ).fetchall()
        )

    actual_categories = {
        int(row["team_id"]): str(row["category"]) for row in actual_rows
    }
    direct_names = tuple(
        str(row["name"]) for row in actual_rows if row["category"] == "direct"
    )
    elimination_names = tuple(
        str(row["name"]) for row in actual_rows if row["category"] == "elimination"
    )
    header = (
        "<p>🎯 <b>Итоги швейцарского этапа</b></p>"
        f"<p>Конкурс: «{escape_rich_text(settings['name'])}»</p>"
    )
    result_lines = (
        "Прямой проход: " + ", ".join(map(escape_rich_text, direct_names)),
        "Через стыки: " + ", ".join(map(escape_rich_text, elimination_names)),
    )
    prediction_count = len(prediction_ids)
    if prediction_count == 0:
        return _split_lines(
            header=header,
            lines=(*result_lines, "Прогнозов на швейцарский этап не было."),
            empty_text="Прогнозов на швейцарский этап не было.",
            max_message_length=max_message_length,
        )

    selections_by_prediction: dict[int, dict[int, str]] = {
        prediction_id: {} for prediction_id in prediction_ids
    }
    support_counts: dict[int, int] = {}
    for row in prediction_rows:
        prediction_id = int(row["prediction_id"])
        team_id = int(row["team_id"])
        selections_by_prediction[prediction_id][team_id] = str(row["category"])
        support_counts[team_id] = support_counts.get(team_id, 0) + 1

    maximum_points = 2 * (
        int(settings["direct_qualifier_count"])
        + int(settings["elimination_qualifier_count"])
    )
    scores: list[int] = []
    for selections in selections_by_prediction.values():
        points = sum(
            2 if actual_categories.get(team_id) == category else 1
            for team_id, category in selections.items()
            if team_id in actual_categories
        )
        scores.append(points)
    total_points = sum(scores)
    perfect_count = sum(points == maximum_points for points in scores)
    least_supported_actual_count = min(
        support_counts.get(team_id, 0) for team_id in actual_categories
    )
    accuracy, perfect, surprise = swiss_result_insights(
        total_points=total_points,
        prediction_count=prediction_count,
        maximum_points=maximum_points,
        perfect_count=perfect_count,
        least_supported_actual_count=least_supported_actual_count,
    )
    average_percent = _round_percent(
        total_points,
        prediction_count * maximum_points,
    )
    lines = (
        *result_lines,
        f"<b>Прогнозов: {prediction_count}</b>",
        f"Средняя точность: {average_percent}%",
        escape_rich_text(accuracy),
        escape_rich_text(perfect),
        escape_rich_text(surprise),
    )
    return _split_lines(
        header=header,
        lines=lines,
        empty_text="Прогнозов на швейцарский этап не было.",
        max_message_length=max_message_length,
    )


def _validate_swiss_publication_deadline(settings, *, now_utc) -> None:
    if not bool(settings["enabled"]):
        raise StalePublicationRevision(
            "Swiss prediction was disabled before rendering."
        )
    deadline_value = settings["deadline_at"]
    if deadline_value is None:
        raise StalePublicationRevision("Swiss prediction deadline is not configured.")
    deadline = datetime.fromisoformat(str(deadline_value).replace("Z", "+00:00"))
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise RuntimeError("Swiss prediction deadline does not include a timezone.")
    if deadline > resolve_service_time(now_utc):
        raise StalePublicationRevision(
            "Swiss prediction is still open and cannot be rendered."
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
        explanation = (
            "<p>"
            + escape_rich_text(
                contest_completion_insight(
                    winner_points=winner.total_points,
                    runner_up_points=details.leaderboard[1].total_points,
                    tiebreak_reason=reason,
                )
            )
            + "</p>"
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


def _render_intermediate_leaderboard(
    *,
    database_path: Path,
    snapshot_id: int,
    contest_id: int,
    max_message_length: int,
    max_table_rows: int,
) -> tuple[str, ...]:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT snapshot_json
            FROM leaderboard_publication_snapshots
            WHERE id = ? AND contest_id = ?
            """,
            (snapshot_id, contest_id),
        ).fetchone()
    if row is None:
        raise RuntimeError("Leaderboard publication snapshot was not found.")

    try:
        snapshot = json.loads(str(row["snapshot_json"]))
    except (TypeError, ValueError) as error:
        raise RuntimeError("Leaderboard publication snapshot is invalid.") from error
    contest_name, captured_at, entries, tiebreak_reason = (
        _validate_intermediate_leaderboard_snapshot(snapshot)
    )

    title = "<p><b>🍀 Промежуточный рейтинг</b></p>"
    context = (
        f"<p>Конкурс «{escape_rich_text(contest_name)}»</p>"
        f"<p>По состоянию на {captured_at.strftime('%d.%m.%Y, %H:%M UTC')}</p>"
    )
    explanation = ""
    if tiebreak_reason is not None:
        explanation = (
            "<p>"
            + escape_rich_text(
                _format_intermediate_tiebreak_explanation(
                    str(entries[0]["participant_name"]),
                    tiebreak_reason,
                )
            )
            + "</p>"
        )
    table_rows = tuple(_format_intermediate_leaderboard_row(entry) for entry in entries)
    header = f"{title}{context}"
    return split_rich_table_messages(
        first_header=f"{header}{explanation}",
        continuation_header=header,
        first_caption="Промежуточный рейтинг",
        continuation_caption="Промежуточный рейтинг · продолжение",
        column_names=("Место", "Участник", "Очки"),
        alignments=("center", "left", "right"),
        column_spans=LEADERBOARD_COLUMN_SPANS,
        rows=table_rows,
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


def _render_match_result_statistics(
    *,
    header: str,
    match,
    predictions,
    max_message_length: int,
) -> tuple[str, ...]:
    total = len(predictions)
    home_score = int(match["home_score_final"])
    away_score = int(match["away_score_final"])
    actual_winner = match["advancing_team_name"]
    if actual_winner is None:
        if home_score > away_score:
            actual_winner = match["home_team_name"]
        elif away_score > home_score:
            actual_winner = match["away_team_name"]

    winner_count = 0
    exact_count = 0
    points_counts: dict[int, int] = {}
    for prediction in predictions:
        predicted_home = int(prediction["predicted_home_score"])
        predicted_away = int(prediction["predicted_away_score"])
        exact_count += int(
            predicted_home == home_score and predicted_away == away_score
        )
        predicted_winner = prediction["advancing_team_name"]
        if predicted_winner is None:
            if predicted_home > predicted_away:
                predicted_winner = match["home_team_name"]
            elif predicted_away > predicted_home:
                predicted_winner = match["away_team_name"]
        winner_count += int(
            actual_winner is not None and predicted_winner == actual_winner
        )
        points = int(prediction["match_points"]) + int(prediction["advancing_points"])
        points_counts[points] = points_counts.get(points, 0) + 1

    winner_insight, exact_insight = series_result_insights(
        winner_count=winner_count,
        exact_count=exact_count,
        total=total,
    )
    lines = (
        f"<b>Прогнозов: {total}</b>",
        f"Победителя угадали: {winner_count} ({_format_percent(winner_count, total)})",
        f"Точный счёт угадали: {exact_count} ({_format_percent(exact_count, total)})",
        escape_rich_text(winner_insight),
        escape_rich_text(exact_insight),
        "<b>Распределение очков</b>",
        *(
            f"{points} — {count} ({_format_percent(count, total)})"
            for points, count in sorted(points_counts.items(), reverse=True)
        ),
    )
    return _split_lines(
        header=header,
        lines=lines,
        empty_text="Прогнозов на этот матч не было.",
        max_message_length=max_message_length,
    )


def _count_named_predictions(predictions, *, field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for prediction in predictions:
        name = str(prediction[field_name])
        counts[name] = counts.get(name, 0) + 1
    return counts


def _format_percent(count: int, total: int) -> str:
    return f"{_round_percent(count, total)}%"


def _round_percent(count: int, total: int) -> int:
    return (count * 200 + total) // (2 * total)


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


def _format_intermediate_leaderboard_row(entry: dict[str, object]) -> str:
    place_value = int(entry["place"])
    place = {1: "🥇", 2: "🥈", 3: "🥉"}.get(place_value, str(place_value))
    participant = escape_rich_text(str(entry["participant_name"]))
    points = str(int(entry["total_points"]))
    if place_value == 1:
        participant = f"<b>{participant}</b>"
        points = f"<b>{points}</b>"
    return table_row(
        (place, participant, points),
        alignments=("center", "left", "right"),
        column_spans=LEADERBOARD_COLUMN_SPANS,
    )


def _validate_intermediate_leaderboard_snapshot(
    snapshot: object,
) -> tuple[
    str,
    datetime,
    tuple[dict[str, object], ...],
    LeaderboardTiebreakReason | None,
]:
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise RuntimeError("Leaderboard publication snapshot version is invalid.")
    contest_name = snapshot.get("contest_name")
    captured_at_value = snapshot.get("captured_at")
    entries_value = snapshot.get("entries")
    reason_value = snapshot.get("top_tiebreak_reason")
    if not isinstance(contest_name, str) or not contest_name.strip():
        raise RuntimeError("Leaderboard publication contest name is invalid.")
    if not isinstance(captured_at_value, str):
        raise RuntimeError("Leaderboard publication timestamp is invalid.")
    try:
        captured_at = datetime.fromisoformat(captured_at_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("Leaderboard publication timestamp is invalid.") from error
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise RuntimeError("Leaderboard publication timestamp has no timezone.")
    captured_at = captured_at.astimezone(timezone.utc)
    if not isinstance(entries_value, list) or not entries_value:
        raise RuntimeError("Leaderboard publication entries are invalid.")

    entries: list[dict[str, object]] = []
    for expected_place, entry in enumerate(entries_value, start=1):
        if not isinstance(entry, dict):
            raise RuntimeError("Leaderboard publication entry is invalid.")
        place = entry.get("place")
        participant_name = entry.get("participant_name")
        total_points = entry.get("total_points")
        if (
            isinstance(place, bool)
            or not isinstance(place, int)
            or place != expected_place
            or not isinstance(participant_name, str)
            or not participant_name.strip()
            or isinstance(total_points, bool)
            or not isinstance(total_points, int)
        ):
            raise RuntimeError("Leaderboard publication entry is invalid.")
        entries.append(
            {
                "place": place,
                "participant_name": participant_name,
                "total_points": total_points,
            }
        )

    valid_reasons = {
        "exact_score",
        "goal_difference",
        "outcome",
        "drawn_advancing_team",
        "champion",
        "draw",
    }
    if reason_value is not None and reason_value not in valid_reasons:
        raise RuntimeError("Leaderboard publication tiebreak reason is invalid.")
    if reason_value is not None and len(entries) < 2:
        raise RuntimeError("Leaderboard publication tiebreak has no runner-up.")
    return (
        contest_name,
        captured_at,
        tuple(entries),
        reason_value,  # type: ignore[return-value]
    )


def _format_intermediate_tiebreak_explanation(
    leader_name: str,
    reason: LeaderboardTiebreakReason,
) -> str:
    explanations = {
        "exact_score": (
            f"При равенстве очков {leader_name} занимает первое место благодаря "
            "большему количеству точных счетов."
        ),
        "goal_difference": (
            f"При равенстве очков {leader_name} занимает первое место благодаря "
            "большему количеству угаданных разниц мячей."
        ),
        "outcome": (
            f"При равенстве очков {leader_name} занимает первое место благодаря "
            "большему количеству угаданных исходов."
        ),
        "drawn_advancing_team": (
            f"При равенстве очков {leader_name} занимает первое место благодаря "
            "большему количеству правильных прогнозов прошедшей команды."
        ),
        "champion": (
            f"При равенстве очков {leader_name} занимает первое место благодаря "
            "правильному прогнозу чемпиона."
        ),
        "draw": (
            "Все дополнительные показатели первого и второго места совпали. "
            "Первое место определено жребием."
        ),
    }
    return explanations[reason]


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

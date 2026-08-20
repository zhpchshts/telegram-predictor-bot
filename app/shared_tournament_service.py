from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.database import database_connection
from app.publication_outbox import (
    create_or_revise_champion_predictions_publication,
    create_or_revise_champion_publication,
    create_or_revise_match_result_publication,
    create_or_revise_swiss_predictions_publication,
    create_or_revise_swiss_result_publication,
    handle_match_publication_deletion,
    revise_champion_publication_for_related_change,
)
from app.scoring_service import (
    recalculate_match_prediction_scores,
    recalculate_tie_prediction_scores,
)


SUPPORTED_TEMPLATE_KEYS = frozenset({"world_cup_2026", "the_international_2026"})


class SharedTournamentNotFoundError(ValueError):
    pass


class SharedTournamentConflictError(ValueError):
    pass


class SharedTournamentLockedError(ValueError):
    pass


class SharedMatchNotFoundError(ValueError):
    pass


class SharedMatchConflictError(ValueError):
    pass


class SharedMatchUpdateUnavailableError(ValueError):
    pass


class SharedMatchResultUnavailableError(ValueError):
    pass


class SharedTournamentSettingsLockedError(ValueError):
    pass


class SharedTournamentResultUnavailableError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SharedTeam:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class SharedMatch:
    id: int
    home_team: SharedTeam
    away_team: SharedTeam
    starts_at_utc: str
    best_of: int | None
    status: str
    home_score: int | None
    away_score: int | None
    advancing_team_id: int | None
    version: int
    linked_contest_count: int
    prediction_count: int


@dataclass(frozen=True, slots=True)
class SharedTournamentSummary:
    id: int
    name: str
    template_key: str
    is_archived: bool
    version: int
    linked_contest_count: int
    match_count: int


@dataclass(frozen=True, slots=True)
class SharedChampionSettings:
    is_enabled: bool
    deadline_at: str | None
    points: int
    actual_champion: SharedTeam | None


@dataclass(frozen=True, slots=True)
class SharedSwissStageSettings:
    is_enabled: bool
    deadline_at: str | None
    direct_qualifier_count: int
    elimination_qualifier_count: int
    direct_qualifier_team_ids: tuple[int, ...]
    elimination_qualifier_team_ids: tuple[int, ...]
    settings_locked: bool


@dataclass(frozen=True, slots=True)
class SharedTournamentDetails:
    tournament: SharedTournamentSummary
    teams: tuple[SharedTeam, ...]
    matches: tuple[SharedMatch, ...]
    champion_prediction: SharedChampionSettings
    swiss_stage_prediction: SharedSwissStageSettings


@dataclass(frozen=True, slots=True)
class SharedMatchDeletionResult:
    linked_contest_count: int
    deleted_prediction_count: int


def list_shared_tournaments(
    *, database_path: Path
) -> tuple[SharedTournamentSummary, ...]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                tournament.id,
                tournament.name,
                tournament.template_key,
                tournament.is_archived,
                tournament.version,
                COUNT(DISTINCT link.contest_id) AS linked_contest_count,
                COUNT(DISTINCT shared_match.id) AS match_count
            FROM shared_tournaments AS tournament
            LEFT JOIN contest_shared_tournaments AS link
                ON link.shared_tournament_id = tournament.id
            LEFT JOIN shared_matches AS shared_match
                ON shared_match.shared_tournament_id = tournament.id
            GROUP BY tournament.id
            ORDER BY tournament.is_archived, tournament.created_at DESC, tournament.id DESC
            """
        ).fetchall()
    return tuple(_shared_tournament_summary_from_row(row) for row in rows)


def get_shared_tournament_details(
    *, database_path: Path, shared_tournament_id: int
) -> SharedTournamentDetails:
    with database_connection(database_path) as connection:
        return _get_shared_tournament_details(
            connection, shared_tournament_id=shared_tournament_id
        )


def create_shared_tournament(
    *,
    database_path: Path,
    name: str,
    template_key: str,
    actor_telegram_user_id: int,
) -> SharedTournamentDetails:
    normalized_name = _normalize_name(name)
    normalized_template_key = _normalize_template_key(template_key)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT 1
            FROM shared_tournaments
            WHERE is_archived = 0 AND lower(name) = lower(?)
            """,
            (normalized_name,),
        ).fetchone()
        if existing is not None:
            raise SharedTournamentConflictError(
                "Активный общий турнир с таким названием уже существует."
            )
        tournament_id = int(
            connection.execute(
                """
                INSERT INTO shared_tournaments (
                    name, template_key, created_by_telegram_user_id
                )
                VALUES (?, ?, ?)
                """,
                (
                    normalized_name,
                    normalized_template_key,
                    actor_telegram_user_id,
                ),
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO shared_tournament_settings (
                shared_tournament_id, champion_prediction_points
            )
            VALUES (?, ?)
            """,
            (
                tournament_id,
                4 if normalized_template_key == "the_international_2026" else 5,
            ),
        )
        _record_event(
            connection,
            shared_tournament_id=tournament_id,
            shared_match_id=None,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type="shared_tournament.created",
            before_state=None,
            after_state={
                "id": tournament_id,
                "name": normalized_name,
                "template_key": normalized_template_key,
            },
        )
        return _get_shared_tournament_details(
            connection, shared_tournament_id=tournament_id
        )


def save_shared_tournament_teams(
    *,
    database_path: Path,
    shared_tournament_id: int,
    team_names: list[str],
    expected_version: int,
    actor_telegram_user_id: int,
) -> SharedTournamentDetails:
    normalized_names = _normalize_team_names(team_names)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tournament_row = _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        _require_expected_tournament_version(
            tournament_row, expected_version=expected_version
        )
        dependency = connection.execute(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM shared_matches
                    WHERE shared_tournament_id = ?
                ) AS match_exists,
                EXISTS (
                    SELECT 1
                    FROM contest_shared_tournaments AS link
                    JOIN champion_predictions AS prediction
                      ON prediction.contest_id = link.contest_id
                    WHERE link.shared_tournament_id = ?
                ) OR EXISTS (
                    SELECT 1
                    FROM contest_shared_tournaments AS link
                    JOIN swiss_stage_predictions AS prediction
                      ON prediction.contest_id = link.contest_id
                    WHERE link.shared_tournament_id = ?
                ) OR EXISTS (
                    SELECT 1
                    FROM shared_tournament_settings
                    WHERE shared_tournament_id = ?
                      AND (
                          champion_team_id IS NOT NULL
                          OR EXISTS (
                              SELECT 1
                              FROM contest_shared_tournaments AS link
                              JOIN swiss_stage_results AS result
                                ON result.contest_id = link.contest_id
                              WHERE link.shared_tournament_id = ?
                          )
                      )
                ) AS long_prediction_exists
            """,
            (
                shared_tournament_id,
                shared_tournament_id,
                shared_tournament_id,
                shared_tournament_id,
                shared_tournament_id,
            ),
        ).fetchone()
        if dependency is not None and (
            bool(dependency["match_exists"])
            or bool(dependency["long_prediction_exists"])
        ):
            raise SharedTournamentLockedError(
                "Список команд общего турнира заблокирован после добавления "
                "матчей, сохранения прогнозов или внесения результатов."
            )
        settings_row = _get_shared_settings_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        if bool(settings_row["swiss_stage_prediction_enabled"]) and (
            int(settings_row["swiss_direct_qualifier_count"])
            + int(settings_row["swiss_elimination_qualifier_count"])
            > len(normalized_names)
        ):
            raise ValueError(
                "Сумма лимитов швейцарского этапа не может превышать "
                "количество команд турнира."
            )
        before_names = [
            str(row["name"])
            for row in connection.execute(
                """
                SELECT teams.name
                FROM shared_tournament_teams AS selection
                JOIN teams ON teams.id = selection.team_id
                WHERE selection.shared_tournament_id = ?
                ORDER BY selection.position
                """,
                (shared_tournament_id,),
            ).fetchall()
        ]
        team_ids = [
            _find_or_create_team(connection, team_name=name)
            for name in normalized_names
        ]
        connection.execute(
            "DELETE FROM shared_tournament_teams WHERE shared_tournament_id = ?",
            (shared_tournament_id,),
        )
        connection.executemany(
            """
            INSERT INTO shared_tournament_teams (
                shared_tournament_id, team_id, position
            )
            VALUES (?, ?, ?)
            """,
            [
                (shared_tournament_id, team_id, position)
                for position, team_id in enumerate(team_ids)
            ],
        )
        linked_contest_rows = connection.execute(
            """
            SELECT contest_id
            FROM contest_shared_tournaments
            WHERE shared_tournament_id = ?
            ORDER BY contest_id
            """,
            (shared_tournament_id,),
        ).fetchall()
        for contest_row in linked_contest_rows:
            contest_id = int(contest_row["contest_id"])
            connection.execute(
                "DELETE FROM contest_teams WHERE contest_id = ?", (contest_id,)
            )
            connection.executemany(
                """
                INSERT INTO contest_teams (contest_id, team_id, position)
                VALUES (?, ?, ?)
                """,
                [
                    (contest_id, team_id, position)
                    for position, team_id in enumerate(team_ids)
                ],
            )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=None,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type="shared_tournament.teams_updated",
            before_state={"team_names": before_names},
            after_state={"team_names": list(normalized_names)},
        )
        _ = tournament_row
        return _get_shared_tournament_details(
            connection, shared_tournament_id=shared_tournament_id
        )


def create_shared_match(
    *,
    database_path: Path,
    shared_tournament_id: int,
    home_team_id: int,
    away_team_id: int,
    starts_at_utc: str,
    best_of: int | None,
    actor_telegram_user_id: int,
    now_utc: datetime | None = None,
) -> SharedMatch:
    normalized_start = _normalize_datetime(starts_at_utc)
    resolved_now = _resolve_now(now_utc)
    if _parse_datetime(normalized_start) <= resolved_now:
        raise ValueError("Время начала нового матча должно быть в будущем.")
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tournament_row = _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        normalized_best_of = _normalize_best_of(
            best_of, template_key=str(tournament_row["template_key"])
        )
        home_team = _get_shared_team_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            team_id=home_team_id,
        )
        away_team = _get_shared_team_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            team_id=away_team_id,
        )
        if home_team is None or away_team is None:
            raise ValueError("Обе команды должны входить в общий турнир.")
        if home_team_id == away_team_id:
            raise ValueError("В матче должны участвовать разные команды.")
        duplicate = connection.execute(
            """
            SELECT 1
            FROM shared_matches
            WHERE shared_tournament_id = ?
              AND (
                    (home_team_id = ? AND away_team_id = ?)
                 OR (home_team_id = ? AND away_team_id = ?)
              )
            """,
            (
                shared_tournament_id,
                home_team_id,
                away_team_id,
                away_team_id,
                home_team_id,
            ),
        ).fetchone()
        if duplicate is not None:
            raise SharedMatchConflictError(
                "Матч между этими командами уже существует в общем турнире."
            )
        shared_match_id = int(
            connection.execute(
                """
                INSERT INTO shared_matches (
                    shared_tournament_id,
                    home_team_id,
                    away_team_id,
                    starts_at_utc,
                    best_of
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    shared_tournament_id,
                    home_team_id,
                    away_team_id,
                    normalized_start,
                    normalized_best_of,
                ),
            ).lastrowid
        )
        contest_rows = connection.execute(
            """
            SELECT contests.id
            FROM contest_shared_tournaments AS link
            JOIN contests ON contests.id = link.contest_id
            WHERE link.shared_tournament_id = ? AND contests.is_active = 1
            ORDER BY contests.id
            """,
            (shared_tournament_id,),
        ).fetchall()
        shared_match_row = _get_shared_match_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
        )
        for contest_row in contest_rows:
            _create_local_match(
                connection,
                contest_id=int(contest_row["id"]),
                shared_match_row=shared_match_row,
            )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type="shared_match.created",
            before_state=None,
            after_state=_shared_match_snapshot(shared_match_row),
            metadata={"linked_contest_count": len(contest_rows)},
        )
        return _shared_match_from_row(
            _get_shared_match_details_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                shared_match_id=shared_match_id,
            )
        )


def update_shared_match_start(
    *,
    database_path: Path,
    shared_tournament_id: int,
    shared_match_id: int,
    starts_at_utc: str,
    expected_version: int,
    actor_telegram_user_id: int,
    now_utc: datetime | None = None,
) -> SharedMatch:
    normalized_start = _normalize_datetime(starts_at_utc)
    resolved_now = _resolve_now(now_utc)
    if _parse_datetime(normalized_start) <= resolved_now:
        raise SharedMatchUpdateUnavailableError(
            "Новое время начала матча должно быть в будущем."
        )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        match_row = _get_shared_match_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
        )
        _require_expected_version(match_row, expected_version=expected_version)
        if (
            str(match_row["status"]) != "scheduled"
            or _parse_datetime(str(match_row["starts_at_utc"])) <= resolved_now
        ):
            raise SharedMatchUpdateUnavailableError(
                "Дедлайн матча уже наступил и больше не может быть изменён."
            )
        local_rows = connection.execute(
            """
            SELECT matches.status, matches.starts_at_utc
            FROM shared_match_links AS link
            JOIN matches ON matches.id = link.match_id
            WHERE link.shared_match_id = ?
            """,
            (shared_match_id,),
        ).fetchall()
        if any(
            str(local_row["status"]) != "scheduled"
            or _parse_datetime(str(local_row["starts_at_utc"])) <= resolved_now
            for local_row in local_rows
        ):
            raise SharedMatchUpdateUnavailableError(
                "Дедлайн хотя бы в одном связанном конкурсе уже наступил."
            )
        if str(match_row["starts_at_utc"]) == normalized_start:
            return _shared_match_from_row(
                _get_shared_match_details_row(
                    connection,
                    shared_tournament_id=shared_tournament_id,
                    shared_match_id=shared_match_id,
                )
            )
        before_state = _shared_match_snapshot(match_row)
        updated = connection.execute(
            """
            UPDATE shared_matches
            SET starts_at_utc = ?, version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND version = ? AND status = 'scheduled'
            """,
            (normalized_start, shared_match_id, expected_version),
        )
        if updated.rowcount != 1:
            raise SharedMatchConflictError(
                "Матч уже был изменён. Обновите данные и повторите действие."
            )
        connection.execute(
            """
            UPDATE matches
            SET starts_at_utc = ?
            WHERE id IN (
                SELECT match_id FROM shared_match_links
                WHERE shared_match_id = ?
            )
              AND status = 'scheduled'
            """,
            (normalized_start, shared_match_id),
        )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        updated_row = _get_shared_match_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
        )
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type="shared_match.start_updated",
            before_state=before_state,
            after_state=_shared_match_snapshot(updated_row),
        )
        return _shared_match_from_row(
            _get_shared_match_details_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                shared_match_id=shared_match_id,
            )
        )


def save_shared_match_result(
    *,
    database_path: Path,
    shared_tournament_id: int,
    shared_match_id: int,
    home_score: int,
    away_score: int,
    advancing_team_id: int | None,
    expected_version: int,
    actor_telegram_user_id: int,
    actor_first_name: str,
    actor_last_name: str | None,
    actor_username: str | None,
    now_utc: datetime | None = None,
) -> SharedMatch:
    normalized_home_score = _normalize_score(home_score)
    normalized_away_score = _normalize_score(away_score)
    resolved_now = _resolve_now(now_utc)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tournament_row = _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        match_row = _get_shared_match_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
        )
        _require_expected_version(match_row, expected_version=expected_version)
        if str(match_row["status"]) == "cancelled":
            raise SharedMatchResultUnavailableError(
                "Для отменённого матча нельзя сохранить результат."
            )
        if (
            str(match_row["status"]) != "finished"
            and _parse_datetime(str(match_row["starts_at_utc"])) > resolved_now
        ):
            raise SharedMatchResultUnavailableError(
                "Результат можно внести только после начала матча."
            )
        normalized_advancing_team_id = _resolve_advancing_team(
            match_row,
            template_key=str(tournament_row["template_key"]),
            home_score=normalized_home_score,
            away_score=normalized_away_score,
            advancing_team_id=advancing_team_id,
        )
        previous_result_exists = match_row["home_score_final"] is not None
        before_state = _shared_match_snapshot(match_row)
        actor_user_id = _upsert_actor_user(
            connection,
            telegram_user_id=actor_telegram_user_id,
            first_name=actor_first_name,
            last_name=actor_last_name,
            username=actor_username,
        )
        updated = connection.execute(
            """
            UPDATE shared_matches
            SET
                status = 'finished',
                home_score_final = ?,
                away_score_final = ?,
                advancing_team_id = ?,
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND version = ?
            """,
            (
                normalized_home_score,
                normalized_away_score,
                normalized_advancing_team_id,
                shared_match_id,
                expected_version,
            ),
        )
        if updated.rowcount != 1:
            raise SharedMatchConflictError(
                "Матч уже был изменён. Обновите данные и повторите действие."
            )
        link_rows = connection.execute(
            """
            SELECT link.contest_id, link.match_id, matches.tie_id,
                   matches.home_score_final, matches.away_score_final,
                   ties.advancing_team_id
            FROM shared_match_links AS link
            JOIN matches ON matches.id = link.match_id
            JOIN ties ON ties.id = matches.tie_id
            WHERE link.shared_match_id = ?
            ORDER BY link.contest_id
            """,
            (shared_match_id,),
        ).fetchall()
        for link_row in link_rows:
            local_was_created = link_row["home_score_final"] is None
            local_match_id = int(link_row["match_id"])
            tie_id = int(link_row["tie_id"])
            connection.execute(
                """
                UPDATE matches
                SET status = 'finished', home_score_final = ?, away_score_final = ?
                WHERE id = ?
                """,
                (normalized_home_score, normalized_away_score, local_match_id),
            )
            connection.execute(
                "UPDATE ties SET advancing_team_id = ? WHERE id = ?",
                (normalized_advancing_team_id, tie_id),
            )
            recalculate_match_prediction_scores(connection, match_id=local_match_id)
            recalculate_tie_prediction_scores(connection, tie_id=tie_id)
            event = connection.execute(
                """
                INSERT INTO event_log (
                    contest_id, actor_user_id, event_type, entity_type,
                    entity_id, payload_json
                )
                VALUES (?, ?, ?, 'match', ?, ?)
                """,
                (
                    int(link_row["contest_id"]),
                    actor_user_id,
                    (
                        "shared_match.result_recorded"
                        if local_was_created
                        else "shared_match.result_corrected"
                    ),
                    local_match_id,
                    json.dumps(
                        {
                            "shared_match_id": shared_match_id,
                            "result": {
                                "home_score": normalized_home_score,
                                "away_score": normalized_away_score,
                                "advancing_team_id": normalized_advancing_team_id,
                            },
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            if event.lastrowid is None:
                raise RuntimeError("Не удалось записать синхронизацию результата.")
            create_or_revise_match_result_publication(
                connection,
                contest_id=int(link_row["contest_id"]),
                match_id=local_match_id,
                event_id=int(event.lastrowid),
                was_created=local_was_created,
                now_utc=resolved_now,
            )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        updated_row = _get_shared_match_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
        )
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type=(
                "shared_match.result_corrected"
                if previous_result_exists
                else "shared_match.result_recorded"
            ),
            before_state=before_state,
            after_state=_shared_match_snapshot(updated_row),
            metadata={"linked_contest_count": len(link_rows)},
        )
        return _shared_match_from_row(
            _get_shared_match_details_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                shared_match_id=shared_match_id,
            )
        )


def save_shared_champion_settings(
    *,
    database_path: Path,
    shared_tournament_id: int,
    enabled: bool,
    deadline_at: str | None,
    points: int,
    expected_version: int,
    actor_telegram_user_id: int,
    actor_first_name: str,
    actor_last_name: str | None,
    actor_username: str | None,
    now_utc: datetime | None = None,
) -> SharedTournamentDetails:
    resolved_now = _resolve_now(now_utc)
    normalized_points = _normalize_non_negative_integer(points, field_name="Баллы")
    normalized_deadline = _normalize_optional_deadline(
        deadline_at, enabled=enabled, field_name="прогноза на чемпиона"
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tournament_row = _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        _require_expected_tournament_version(
            tournament_row, expected_version=expected_version
        )
        settings = _get_shared_settings_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        if settings["champion_team_id"] is not None:
            raise SharedTournamentSettingsLockedError(
                "Настройки прогноза на чемпиона нельзя изменить после указания чемпиона."
            )
        if enabled and not _shared_team_count(
            connection, shared_tournament_id=shared_tournament_id
        ):
            raise ValueError("Сначала добавьте команды турнира.")
        _validate_deadline_change(
            previous_deadline=settings["champion_prediction_deadline_at"],
            new_deadline=normalized_deadline,
            now_utc=resolved_now,
            field_name="прогноза на чемпиона",
        )
        before = _shared_settings_snapshot(settings)
        connection.execute(
            """
            UPDATE shared_tournament_settings
            SET champion_prediction_enabled = ?,
                champion_prediction_deadline_at = ?,
                champion_prediction_points = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE shared_tournament_id = ?
            """,
            (
                int(enabled),
                normalized_deadline,
                normalized_points,
                shared_tournament_id,
            ),
        )
        actor_user_id = _upsert_actor_user(
            connection,
            telegram_user_id=actor_telegram_user_id,
            first_name=actor_first_name,
            last_name=actor_last_name,
            username=actor_username,
        )
        for contest_row in _get_linked_contest_rows(
            connection, shared_tournament_id=shared_tournament_id
        ):
            contest_id = int(contest_row["id"])
            connection.execute(
                """
                UPDATE contests
                SET champion_prediction_enabled = ?,
                    champion_prediction_deadline_at = ?,
                    champion_prediction_points = ?
                WHERE id = ?
                """,
                (int(enabled), normalized_deadline, normalized_points, contest_id),
            )
            event_id = _write_contest_sync_event(
                connection,
                contest_id=contest_id,
                actor_user_id=actor_user_id,
                event_type="shared_tournament.champion_prediction_settings_updated",
                payload={
                    "shared_tournament_id": shared_tournament_id,
                    "enabled": enabled,
                    "deadline_at": normalized_deadline,
                    "points": normalized_points,
                },
            )
            revise_champion_publication_for_related_change(
                connection,
                contest_id=contest_id,
                event_id=event_id,
                now_utc=resolved_now,
            )
            if bool(contest_row["match_prediction_publication_enabled"]):
                create_or_revise_champion_predictions_publication(
                    connection,
                    contest_id=contest_id,
                    event_id=event_id,
                    now_utc=resolved_now,
                )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        after = _get_shared_settings_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=None,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type="shared_tournament.champion_prediction_settings_updated",
            before_state=before,
            after_state=_shared_settings_snapshot(after),
        )
        return _get_shared_tournament_details(
            connection, shared_tournament_id=shared_tournament_id
        )


def save_shared_champion_result(
    *,
    database_path: Path,
    shared_tournament_id: int,
    champion_team_id: int,
    expected_version: int,
    actor_telegram_user_id: int,
    actor_first_name: str,
    actor_last_name: str | None,
    actor_username: str | None,
    now_utc: datetime | None = None,
) -> SharedTournamentDetails:
    resolved_now = _resolve_now(now_utc)
    normalized_team_id = _normalize_positive_integer(
        champion_team_id, field_name="Фактический чемпион"
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tournament_row = _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        _require_expected_tournament_version(
            tournament_row, expected_version=expected_version
        )
        settings = _get_shared_settings_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        if not bool(settings["champion_prediction_enabled"]):
            raise SharedTournamentResultUnavailableError(
                "Сначала включите прогноз на чемпиона."
            )
        _require_deadline_passed(
            settings["champion_prediction_deadline_at"],
            now_utc=resolved_now,
            field_name="прогноза на чемпиона",
        )
        unfinished = connection.execute(
            """
            SELECT 1 FROM shared_matches
            WHERE shared_tournament_id = ?
              AND status NOT IN ('finished', 'cancelled')
            LIMIT 1
            """,
            (shared_tournament_id,),
        ).fetchone()
        if unfinished is not None:
            raise SharedTournamentResultUnavailableError(
                "Чемпиона можно указать после завершения всех матчей турнира."
            )
        _require_shared_team(
            connection,
            shared_tournament_id=shared_tournament_id,
            team_id=normalized_team_id,
        )
        previous_team_id = settings["champion_team_id"]
        if previous_team_id == normalized_team_id:
            return _get_shared_tournament_details(
                connection, shared_tournament_id=shared_tournament_id
            )
        actor_user_id = _upsert_actor_user(
            connection,
            telegram_user_id=actor_telegram_user_id,
            first_name=actor_first_name,
            last_name=actor_last_name,
            username=actor_username,
        )
        connection.execute(
            """
            UPDATE shared_tournament_settings
            SET champion_team_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE shared_tournament_id = ?
            """,
            (normalized_team_id, shared_tournament_id),
        )
        for contest_row in _get_linked_contest_rows(
            connection, shared_tournament_id=shared_tournament_id
        ):
            contest_id = int(contest_row["id"])
            local_previous = contest_row["champion_team_id"]
            connection.execute(
                "UPDATE contests SET champion_team_id = ? WHERE id = ?",
                (normalized_team_id, contest_id),
            )
            event_id = _write_contest_sync_event(
                connection,
                contest_id=contest_id,
                actor_user_id=actor_user_id,
                event_type=(
                    "shared_tournament.champion_recorded"
                    if local_previous is None
                    else "shared_tournament.champion_corrected"
                ),
                payload={
                    "shared_tournament_id": shared_tournament_id,
                    "champion_team_id": normalized_team_id,
                    "previous_champion_team_id": local_previous,
                },
            )
            create_or_revise_champion_publication(
                connection,
                contest_id=contest_id,
                event_id=event_id,
                was_created=local_previous is None,
                now_utc=resolved_now,
            )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=None,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type=(
                "shared_tournament.champion_recorded"
                if previous_team_id is None
                else "shared_tournament.champion_corrected"
            ),
            before_state={"champion_team_id": previous_team_id},
            after_state={"champion_team_id": normalized_team_id},
        )
        return _get_shared_tournament_details(
            connection, shared_tournament_id=shared_tournament_id
        )


def save_shared_swiss_settings(
    *,
    database_path: Path,
    shared_tournament_id: int,
    enabled: bool,
    deadline_at: str | None,
    direct_qualifier_count: int,
    elimination_qualifier_count: int,
    expected_version: int,
    actor_telegram_user_id: int,
    actor_first_name: str,
    actor_last_name: str | None,
    actor_username: str | None,
    now_utc: datetime | None = None,
) -> SharedTournamentDetails:
    resolved_now = _resolve_now(now_utc)
    direct_count = _normalize_positive_integer(
        direct_qualifier_count, field_name="Количество прямых проходов"
    )
    elimination_count = _normalize_positive_integer(
        elimination_qualifier_count,
        field_name="Количество проходов через элиминейшн-раунд",
    )
    normalized_deadline = _normalize_optional_deadline(
        deadline_at, enabled=enabled, field_name="прогноза на швейцарский этап"
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tournament_row = _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        _require_expected_tournament_version(
            tournament_row, expected_version=expected_version
        )
        settings = _get_shared_settings_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        team_count = _shared_team_count(
            connection, shared_tournament_id=shared_tournament_id
        )
        if enabled and team_count == 0:
            raise ValueError("Сначала добавьте команды турнира.")
        if enabled and direct_count + elimination_count > team_count:
            raise ValueError(
                "Сумма лимитов швейцарского этапа не может превышать "
                "количество команд турнира."
            )
        _validate_deadline_change(
            previous_deadline=settings["swiss_stage_prediction_deadline_at"],
            new_deadline=normalized_deadline,
            now_utc=resolved_now,
            field_name="прогноза на швейцарский этап",
        )
        non_deadline_changed = any(
            (
                enabled != bool(settings["swiss_stage_prediction_enabled"]),
                direct_count != int(settings["swiss_direct_qualifier_count"]),
                elimination_count != int(settings["swiss_elimination_qualifier_count"]),
            )
        )
        if non_deadline_changed and _shared_swiss_settings_locked(
            connection, shared_tournament_id=shared_tournament_id
        ):
            raise SharedTournamentSettingsLockedError(
                "Настройки швейцарского этапа нельзя изменить после первого "
                "прогноза или результата; до дедлайна можно менять только дедлайн."
            )
        before = _shared_settings_snapshot(settings)
        connection.execute(
            """
            UPDATE shared_tournament_settings
            SET swiss_stage_prediction_enabled = ?,
                swiss_stage_prediction_deadline_at = ?,
                swiss_direct_qualifier_count = ?,
                swiss_elimination_qualifier_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE shared_tournament_id = ?
            """,
            (
                int(enabled),
                normalized_deadline,
                direct_count,
                elimination_count,
                shared_tournament_id,
            ),
        )
        actor_user_id = _upsert_actor_user(
            connection,
            telegram_user_id=actor_telegram_user_id,
            first_name=actor_first_name,
            last_name=actor_last_name,
            username=actor_username,
        )
        for contest_row in _get_linked_contest_rows(
            connection, shared_tournament_id=shared_tournament_id
        ):
            contest_id = int(contest_row["id"])
            connection.execute(
                """
                INSERT INTO swiss_stage_prediction_settings (
                    contest_id, enabled, deadline_at,
                    direct_qualifier_count, elimination_qualifier_count
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(contest_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    deadline_at = excluded.deadline_at,
                    direct_qualifier_count = excluded.direct_qualifier_count,
                    elimination_qualifier_count = excluded.elimination_qualifier_count,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    contest_id,
                    int(enabled),
                    normalized_deadline,
                    direct_count,
                    elimination_count,
                ),
            )
            event_id = _write_contest_sync_event(
                connection,
                contest_id=contest_id,
                actor_user_id=actor_user_id,
                event_type="shared_tournament.swiss_settings_updated",
                payload={
                    "shared_tournament_id": shared_tournament_id,
                    "enabled": enabled,
                    "deadline_at": normalized_deadline,
                    "direct_qualifier_count": direct_count,
                    "elimination_qualifier_count": elimination_count,
                },
            )
            if bool(contest_row["match_prediction_publication_enabled"]):
                create_or_revise_swiss_predictions_publication(
                    connection,
                    contest_id=contest_id,
                    event_id=event_id,
                    now_utc=resolved_now,
                )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        after = _get_shared_settings_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=None,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type="shared_tournament.swiss_settings_updated",
            before_state=before,
            after_state=_shared_settings_snapshot(after),
        )
        return _get_shared_tournament_details(
            connection, shared_tournament_id=shared_tournament_id
        )


def save_shared_swiss_result(
    *,
    database_path: Path,
    shared_tournament_id: int,
    direct_team_ids: list[int],
    elimination_team_ids: list[int],
    expected_version: int,
    actor_telegram_user_id: int,
    actor_first_name: str,
    actor_last_name: str | None,
    actor_username: str | None,
    now_utc: datetime | None = None,
) -> SharedTournamentDetails:
    resolved_now = _resolve_now(now_utc)
    direct_ids, elimination_ids = _normalize_team_id_sets(
        direct_team_ids, elimination_team_ids
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tournament_row = _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        _require_expected_tournament_version(
            tournament_row, expected_version=expected_version
        )
        settings = _get_shared_settings_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        if not bool(settings["swiss_stage_prediction_enabled"]):
            raise SharedTournamentResultUnavailableError(
                "Сначала включите прогноз на швейцарский этап."
            )
        _require_deadline_passed(
            settings["swiss_stage_prediction_deadline_at"],
            now_utc=resolved_now,
            field_name="прогноза на швейцарский этап",
        )
        _validate_shared_selection(
            connection,
            shared_tournament_id=shared_tournament_id,
            direct_ids=direct_ids,
            elimination_ids=elimination_ids,
            direct_count=int(settings["swiss_direct_qualifier_count"]),
            elimination_count=int(settings["swiss_elimination_qualifier_count"]),
        )
        previous_direct, previous_elimination = _get_shared_swiss_result_ids(
            connection, shared_tournament_id=shared_tournament_id
        )
        if set(previous_direct) == set(direct_ids) and set(previous_elimination) == set(
            elimination_ids
        ):
            return _get_shared_tournament_details(
                connection, shared_tournament_id=shared_tournament_id
            )
        was_created = not previous_direct and not previous_elimination
        actor_user_id = _upsert_actor_user(
            connection,
            telegram_user_id=actor_telegram_user_id,
            first_name=actor_first_name,
            last_name=actor_last_name,
            username=actor_username,
        )
        connection.execute(
            "DELETE FROM shared_swiss_stage_result_selections WHERE shared_tournament_id = ?",
            (shared_tournament_id,),
        )
        connection.executemany(
            """
            INSERT INTO shared_swiss_stage_result_selections (
                shared_tournament_id, team_id, category
            ) VALUES (?, ?, ?)
            """,
            [(shared_tournament_id, team_id, "direct") for team_id in direct_ids]
            + [
                (shared_tournament_id, team_id, "elimination")
                for team_id in elimination_ids
            ],
        )
        for contest_row in _get_linked_contest_rows(
            connection, shared_tournament_id=shared_tournament_id
        ):
            contest_id = int(contest_row["id"])
            local_exists = connection.execute(
                "SELECT 1 FROM swiss_stage_results WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO swiss_stage_results (contest_id) VALUES (?)
                ON CONFLICT(contest_id) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                """,
                (contest_id,),
            )
            connection.execute(
                "DELETE FROM swiss_stage_result_selections WHERE contest_id = ?",
                (contest_id,),
            )
            connection.executemany(
                """
                INSERT INTO swiss_stage_result_selections (contest_id, team_id, category)
                VALUES (?, ?, ?)
                """,
                [(contest_id, team_id, "direct") for team_id in direct_ids]
                + [(contest_id, team_id, "elimination") for team_id in elimination_ids],
            )
            event_id = _write_contest_sync_event(
                connection,
                contest_id=contest_id,
                actor_user_id=actor_user_id,
                event_type=(
                    "shared_tournament.swiss_result_recorded"
                    if local_exists is None
                    else "shared_tournament.swiss_result_corrected"
                ),
                payload={
                    "shared_tournament_id": shared_tournament_id,
                    "direct_team_ids": list(direct_ids),
                    "elimination_team_ids": list(elimination_ids),
                },
            )
            create_or_revise_swiss_result_publication(
                connection,
                contest_id=contest_id,
                event_id=event_id,
                was_created=local_exists is None,
                now_utc=resolved_now,
            )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=None,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type=(
                "shared_tournament.swiss_result_recorded"
                if was_created
                else "shared_tournament.swiss_result_corrected"
            ),
            before_state={
                "direct_team_ids": list(previous_direct),
                "elimination_team_ids": list(previous_elimination),
            },
            after_state={
                "direct_team_ids": list(direct_ids),
                "elimination_team_ids": list(elimination_ids),
            },
        )
        return _get_shared_tournament_details(
            connection, shared_tournament_id=shared_tournament_id
        )


def delete_shared_match(
    *,
    database_path: Path,
    shared_tournament_id: int,
    shared_match_id: int,
    expected_version: int,
    actor_telegram_user_id: int,
    actor_first_name: str,
    actor_last_name: str | None,
    actor_username: str | None,
    now_utc: datetime | None = None,
) -> SharedMatchDeletionResult:
    resolved_now = _resolve_now(now_utc)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _get_active_tournament_row(
            connection, shared_tournament_id=shared_tournament_id
        )
        match_row = _get_shared_match_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
        )
        _require_expected_version(match_row, expected_version=expected_version)
        actor_user_id = _upsert_actor_user(
            connection,
            telegram_user_id=actor_telegram_user_id,
            first_name=actor_first_name,
            last_name=actor_last_name,
            username=actor_username,
        )
        link_rows = connection.execute(
            """
            SELECT link.contest_id, link.match_id, matches.tie_id,
                   COUNT(match_predictions.id) AS prediction_count
            FROM shared_match_links AS link
            JOIN matches ON matches.id = link.match_id
            LEFT JOIN match_predictions ON match_predictions.match_id = matches.id
            WHERE link.shared_match_id = ?
            GROUP BY link.contest_id, link.match_id, matches.tie_id
            ORDER BY link.contest_id
            """,
            (shared_match_id,),
        ).fetchall()
        deleted_prediction_count = sum(
            int(row["prediction_count"]) for row in link_rows
        )
        _record_event(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
            actor_telegram_user_id=actor_telegram_user_id,
            event_type="shared_match.deleted",
            before_state=_shared_match_snapshot(match_row),
            after_state=None,
            metadata={
                "linked_contest_count": len(link_rows),
                "deleted_prediction_count": deleted_prediction_count,
            },
        )
        for link_row in link_rows:
            contest_id = int(link_row["contest_id"])
            local_match_id = int(link_row["match_id"])
            event = connection.execute(
                """
                INSERT INTO event_log (
                    contest_id, actor_user_id, event_type, entity_type,
                    entity_id, payload_json
                )
                VALUES (?, ?, 'shared_match.deleted', 'match', ?, ?)
                """,
                (
                    contest_id,
                    actor_user_id,
                    local_match_id,
                    json.dumps(
                        {"shared_match_id": shared_match_id},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            if event.lastrowid is None:
                raise RuntimeError("Не удалось записать удаление общего матча.")
            handle_match_publication_deletion(
                connection,
                contest_id=contest_id,
                match_id=local_match_id,
                event_id=int(event.lastrowid),
                now_utc=resolved_now,
            )
            connection.execute("DELETE FROM matches WHERE id = ?", (local_match_id,))
            tie_id = int(link_row["tie_id"])
            if (
                connection.execute(
                    "SELECT 1 FROM matches WHERE tie_id = ? LIMIT 1", (tie_id,)
                ).fetchone()
                is None
            ):
                connection.execute("DELETE FROM ties WHERE id = ?", (tie_id,))
        connection.execute(
            "DELETE FROM shared_matches WHERE id = ?", (shared_match_id,)
        )
        _touch_tournament(connection, shared_tournament_id=shared_tournament_id)
        return SharedMatchDeletionResult(
            linked_contest_count=len(link_rows),
            deleted_prediction_count=deleted_prediction_count,
        )


def attach_shared_tournament(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    shared_tournament_id: int,
) -> None:
    tournament_row = _get_active_tournament_row(
        connection, shared_tournament_id=shared_tournament_id
    )
    contest_row = connection.execute(
        "SELECT id, template_key FROM contests WHERE id = ?",
        (contest_id,),
    ).fetchone()
    if contest_row is None:
        raise RuntimeError("Конкурс для подключения общего турнира не найден.")
    if str(contest_row["template_key"]) != str(tournament_row["template_key"]):
        raise SharedTournamentConflictError(
            "Шаблон конкурса не совпадает с шаблоном общего турнира."
        )
    if (
        connection.execute(
            "SELECT 1 FROM contest_shared_tournaments WHERE contest_id = ?",
            (contest_id,),
        ).fetchone()
        is not None
    ):
        raise SharedTournamentConflictError("К конкурсу уже подключён общий турнир.")
    connection.execute(
        """
        INSERT INTO contest_shared_tournaments (contest_id, shared_tournament_id)
        VALUES (?, ?)
        """,
        (contest_id, shared_tournament_id),
    )
    team_rows = connection.execute(
        """
        SELECT team_id, position
        FROM shared_tournament_teams
        WHERE shared_tournament_id = ?
        ORDER BY position
        """,
        (shared_tournament_id,),
    ).fetchall()
    connection.executemany(
        """
        INSERT INTO contest_teams (contest_id, team_id, position)
        VALUES (?, ?, ?)
        """,
        [(contest_id, int(row["team_id"]), int(row["position"])) for row in team_rows],
    )
    settings = _get_shared_settings_row(
        connection, shared_tournament_id=shared_tournament_id
    )
    connection.execute(
        """
        UPDATE contests
        SET champion_prediction_enabled = ?,
            champion_prediction_deadline_at = ?,
            champion_prediction_points = ?,
            champion_team_id = ?
        WHERE id = ?
        """,
        (
            int(settings["champion_prediction_enabled"]),
            settings["champion_prediction_deadline_at"],
            int(settings["champion_prediction_points"]),
            settings["champion_team_id"],
            contest_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO swiss_stage_prediction_settings (
            contest_id, enabled, deadline_at,
            direct_qualifier_count, elimination_qualifier_count
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            contest_id,
            int(settings["swiss_stage_prediction_enabled"]),
            settings["swiss_stage_prediction_deadline_at"],
            int(settings["swiss_direct_qualifier_count"]),
            int(settings["swiss_elimination_qualifier_count"]),
        ),
    )
    direct_ids, elimination_ids = _get_shared_swiss_result_ids(
        connection, shared_tournament_id=shared_tournament_id
    )
    if direct_ids or elimination_ids:
        connection.execute(
            "INSERT INTO swiss_stage_results (contest_id) VALUES (?)", (contest_id,)
        )
        connection.executemany(
            """
            INSERT INTO swiss_stage_result_selections (contest_id, team_id, category)
            VALUES (?, ?, ?)
            """,
            [(contest_id, team_id, "direct") for team_id in direct_ids]
            + [(contest_id, team_id, "elimination") for team_id in elimination_ids],
        )
    match_rows = connection.execute(
        """
        SELECT * FROM shared_matches
        WHERE shared_tournament_id = ?
        ORDER BY starts_at_utc, id
        """,
        (shared_tournament_id,),
    ).fetchall()
    for match_row in match_rows:
        _create_local_match(
            connection,
            contest_id=contest_id,
            shared_match_row=match_row,
        )


def contest_uses_shared_tournament(
    connection: sqlite3.Connection, *, contest_id: int
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM contest_shared_tournaments WHERE contest_id = ?",
            (contest_id,),
        ).fetchone()
        is not None
    )


def _create_local_match(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    shared_match_row: sqlite3.Row,
) -> int:
    competition_row = connection.execute(
        """
        SELECT competitions.id AS competition_id,
               scoring_rule_sets.id AS scoring_rule_set_id
        FROM competitions
        JOIN scoring_rule_sets
          ON scoring_rule_sets.competition_id = competitions.id
        WHERE competitions.contest_id = ?
          AND competitions.is_active = 1
          AND scoring_rule_sets.is_active = 1
        ORDER BY competitions.id, scoring_rule_sets.version DESC
        LIMIT 1
        """,
        (contest_id,),
    ).fetchone()
    if competition_row is None:
        raise RuntimeError("Не найдены правила связанного конкурса.")
    competition_id = int(competition_row["competition_id"])
    stage_row = connection.execute(
        """
        SELECT id FROM stages
        WHERE competition_id = ?
        ORDER BY position, id
        LIMIT 1
        """,
        (competition_id,),
    ).fetchone()
    if stage_row is None:
        stage_id = int(
            connection.execute(
                """
                INSERT INTO stages (competition_id, name, position, stage_type)
                VALUES (?, 'Плей-офф', 1, 'knockout')
                """,
                (competition_id,),
            ).lastrowid
        )
    else:
        stage_id = int(stage_row["id"])
    position_row = connection.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 AS value FROM ties WHERE stage_id = ?",
        (stage_id,),
    ).fetchone()
    position = int(position_row["value"])
    team_names = connection.execute(
        """
        SELECT home.name AS home_name, away.name AS away_name
        FROM teams AS home, teams AS away
        WHERE home.id = ? AND away.id = ?
        """,
        (
            int(shared_match_row["home_team_id"]),
            int(shared_match_row["away_team_id"]),
        ),
    ).fetchone()
    if team_names is None:
        raise RuntimeError("Не найдены команды общего матча.")
    scoring_rule_set_id = int(competition_row["scoring_rule_set_id"])
    tie_id = int(
        connection.execute(
            """
            INSERT INTO ties (
                stage_id, scoring_rule_set_id, name, position,
                is_two_legged, advancing_team_id
            )
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (
                stage_id,
                scoring_rule_set_id,
                f"{team_names['home_name']} — {team_names['away_name']}",
                position,
                shared_match_row["advancing_team_id"],
            ),
        ).lastrowid
    )
    next_match_id_row = connection.execute(
        """
        SELECT MAX(value) + 1 AS next_id
        FROM (
            SELECT COALESCE(MAX(id), 0) AS value FROM matches
            UNION ALL
            SELECT COALESCE(MAX(entity_id), 0) AS value
            FROM event_log
            WHERE entity_type = 'match'
        )
        """
    ).fetchone()
    if next_match_id_row is None or next_match_id_row["next_id"] is None:
        raise RuntimeError("Не удалось определить идентификатор связанного матча.")
    local_match_id = int(next_match_id_row["next_id"])
    connection.execute(
        """
            INSERT INTO matches (
                id, stage_id, tie_id, scoring_rule_set_id, home_team_id,
                away_team_id, starts_at_utc, best_of, status,
                home_score_final, away_score_final
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            local_match_id,
            stage_id,
            tie_id,
            scoring_rule_set_id,
            int(shared_match_row["home_team_id"]),
            int(shared_match_row["away_team_id"]),
            str(shared_match_row["starts_at_utc"]),
            shared_match_row["best_of"],
            str(shared_match_row["status"]),
            shared_match_row["home_score_final"],
            shared_match_row["away_score_final"],
        ),
    )
    connection.execute(
        """
        INSERT INTO shared_match_links (shared_match_id, match_id, contest_id)
        VALUES (?, ?, ?)
        """,
        (int(shared_match_row["id"]), local_match_id, contest_id),
    )
    return local_match_id


def _get_shared_tournament_details(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> SharedTournamentDetails:
    row = connection.execute(
        """
        SELECT
            tournament.id,
            tournament.name,
            tournament.template_key,
            tournament.is_archived,
            tournament.version,
            COUNT(DISTINCT contest_link.contest_id) AS linked_contest_count,
            COUNT(DISTINCT shared_match.id) AS match_count
        FROM shared_tournaments AS tournament
        LEFT JOIN contest_shared_tournaments AS contest_link
          ON contest_link.shared_tournament_id = tournament.id
        LEFT JOIN shared_matches AS shared_match
          ON shared_match.shared_tournament_id = tournament.id
        WHERE tournament.id = ?
        GROUP BY tournament.id
        """,
        (shared_tournament_id,),
    ).fetchone()
    if row is None:
        raise SharedTournamentNotFoundError("Общий турнир не найден.")
    team_rows = connection.execute(
        """
        SELECT teams.id, teams.name
        FROM shared_tournament_teams AS selection
        JOIN teams ON teams.id = selection.team_id
        WHERE selection.shared_tournament_id = ?
        ORDER BY selection.position, teams.id
        """,
        (shared_tournament_id,),
    ).fetchall()
    match_rows = connection.execute(
        _SHARED_MATCH_DETAILS_QUERY
        + " WHERE shared_match.shared_tournament_id = ?"
        + " GROUP BY shared_match.id"
        + " ORDER BY shared_match.starts_at_utc, shared_match.id",
        (shared_tournament_id,),
    ).fetchall()
    settings = _get_shared_settings_row(
        connection, shared_tournament_id=shared_tournament_id
    )
    champion_team = None
    if settings["champion_team_id"] is not None:
        champion_row = connection.execute(
            "SELECT id, name FROM teams WHERE id = ?",
            (int(settings["champion_team_id"]),),
        ).fetchone()
        if champion_row is not None:
            champion_team = SharedTeam(
                id=int(champion_row["id"]), name=str(champion_row["name"])
            )
    direct_ids, elimination_ids = _get_shared_swiss_result_ids(
        connection, shared_tournament_id=shared_tournament_id
    )
    return SharedTournamentDetails(
        tournament=_shared_tournament_summary_from_row(row),
        teams=tuple(
            SharedTeam(id=int(item["id"]), name=str(item["name"])) for item in team_rows
        ),
        matches=tuple(_shared_match_from_row(item) for item in match_rows),
        champion_prediction=SharedChampionSettings(
            is_enabled=bool(settings["champion_prediction_enabled"]),
            deadline_at=(
                str(settings["champion_prediction_deadline_at"])
                if settings["champion_prediction_deadline_at"] is not None
                else None
            ),
            points=int(settings["champion_prediction_points"]),
            actual_champion=champion_team,
        ),
        swiss_stage_prediction=SharedSwissStageSettings(
            is_enabled=bool(settings["swiss_stage_prediction_enabled"]),
            deadline_at=(
                str(settings["swiss_stage_prediction_deadline_at"])
                if settings["swiss_stage_prediction_deadline_at"] is not None
                else None
            ),
            direct_qualifier_count=int(settings["swiss_direct_qualifier_count"]),
            elimination_qualifier_count=int(
                settings["swiss_elimination_qualifier_count"]
            ),
            direct_qualifier_team_ids=direct_ids,
            elimination_qualifier_team_ids=elimination_ids,
            settings_locked=_shared_swiss_settings_locked(
                connection, shared_tournament_id=shared_tournament_id
            ),
        ),
    )


_SHARED_MATCH_DETAILS_QUERY = """
    SELECT
        shared_match.*,
        home.id AS home_team_detail_id,
        home.name AS home_team_name,
        away.id AS away_team_detail_id,
        away.name AS away_team_name,
        COUNT(DISTINCT link.contest_id) AS linked_contest_count,
        COUNT(DISTINCT prediction.id) AS prediction_count
    FROM shared_matches AS shared_match
    JOIN teams AS home ON home.id = shared_match.home_team_id
    JOIN teams AS away ON away.id = shared_match.away_team_id
    LEFT JOIN shared_match_links AS link ON link.shared_match_id = shared_match.id
    LEFT JOIN match_predictions AS prediction ON prediction.match_id = link.match_id
"""


def _get_shared_match_details_row(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    shared_match_id: int,
) -> sqlite3.Row:
    row = connection.execute(
        _SHARED_MATCH_DETAILS_QUERY
        + " WHERE shared_match.shared_tournament_id = ? AND shared_match.id = ?"
        + " GROUP BY shared_match.id",
        (shared_tournament_id, shared_match_id),
    ).fetchone()
    if row is None:
        raise SharedMatchNotFoundError("Общий матч не найден.")
    return row


def _get_shared_match_row(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    shared_match_id: int,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM shared_matches
        WHERE shared_tournament_id = ? AND id = ?
        """,
        (shared_tournament_id, shared_match_id),
    ).fetchone()
    if row is None:
        raise SharedMatchNotFoundError("Общий матч не найден.")
    return row


def _get_active_tournament_row(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM shared_tournaments WHERE id = ?",
        (shared_tournament_id,),
    ).fetchone()
    if row is None:
        raise SharedTournamentNotFoundError("Общий турнир не найден.")
    if bool(row["is_archived"]):
        raise SharedTournamentLockedError("Общий турнир находится в архиве.")
    return row


def _get_shared_team_row(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    team_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT teams.id, teams.name
        FROM shared_tournament_teams AS selection
        JOIN teams ON teams.id = selection.team_id
        WHERE selection.shared_tournament_id = ? AND selection.team_id = ?
        """,
        (shared_tournament_id, team_id),
    ).fetchone()


def _shared_tournament_summary_from_row(row: sqlite3.Row) -> SharedTournamentSummary:
    return SharedTournamentSummary(
        id=int(row["id"]),
        name=str(row["name"]),
        template_key=str(row["template_key"]),
        is_archived=bool(row["is_archived"]),
        version=int(row["version"]),
        linked_contest_count=int(row["linked_contest_count"]),
        match_count=int(row["match_count"]),
    )


def _shared_match_from_row(row: sqlite3.Row) -> SharedMatch:
    return SharedMatch(
        id=int(row["id"]),
        home_team=SharedTeam(
            id=int(row["home_team_detail_id"]), name=str(row["home_team_name"])
        ),
        away_team=SharedTeam(
            id=int(row["away_team_detail_id"]), name=str(row["away_team_name"])
        ),
        starts_at_utc=str(row["starts_at_utc"]),
        best_of=int(row["best_of"]) if row["best_of"] is not None else None,
        status=str(row["status"]),
        home_score=(
            int(row["home_score_final"])
            if row["home_score_final"] is not None
            else None
        ),
        away_score=(
            int(row["away_score_final"])
            if row["away_score_final"] is not None
            else None
        ),
        advancing_team_id=(
            int(row["advancing_team_id"])
            if row["advancing_team_id"] is not None
            else None
        ),
        version=int(row["version"]),
        linked_contest_count=int(row["linked_contest_count"]),
        prediction_count=int(row["prediction_count"]),
    )


def _shared_match_snapshot(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "home_team_id": int(row["home_team_id"]),
        "away_team_id": int(row["away_team_id"]),
        "starts_at_utc": str(row["starts_at_utc"]),
        "best_of": int(row["best_of"]) if row["best_of"] is not None else None,
        "status": str(row["status"]),
        "home_score": (
            int(row["home_score_final"])
            if row["home_score_final"] is not None
            else None
        ),
        "away_score": (
            int(row["away_score_final"])
            if row["away_score_final"] is not None
            else None
        ),
        "advancing_team_id": (
            int(row["advancing_team_id"])
            if row["advancing_team_id"] is not None
            else None
        ),
        "version": int(row["version"]),
    }


def _get_shared_settings_row(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM shared_tournament_settings
        WHERE shared_tournament_id = ?
        """,
        (shared_tournament_id,),
    ).fetchone()
    if row is not None:
        return row
    tournament = connection.execute(
        "SELECT template_key FROM shared_tournaments WHERE id = ?",
        (shared_tournament_id,),
    ).fetchone()
    if tournament is None:
        raise SharedTournamentNotFoundError("Общий турнир не найден.")
    connection.execute(
        """
        INSERT INTO shared_tournament_settings (
            shared_tournament_id, champion_prediction_points
        ) VALUES (?, ?)
        """,
        (
            shared_tournament_id,
            4 if str(tournament["template_key"]) == "the_international_2026" else 5,
        ),
    )
    row = connection.execute(
        "SELECT * FROM shared_tournament_settings WHERE shared_tournament_id = ?",
        (shared_tournament_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Не удалось создать настройки общего турнира.")
    return row


def _shared_settings_snapshot(row: sqlite3.Row) -> dict[str, object]:
    return {
        "champion_prediction_enabled": bool(row["champion_prediction_enabled"]),
        "champion_prediction_deadline_at": row["champion_prediction_deadline_at"],
        "champion_prediction_points": int(row["champion_prediction_points"]),
        "champion_team_id": row["champion_team_id"],
        "swiss_stage_prediction_enabled": bool(row["swiss_stage_prediction_enabled"]),
        "swiss_stage_prediction_deadline_at": row["swiss_stage_prediction_deadline_at"],
        "swiss_direct_qualifier_count": int(row["swiss_direct_qualifier_count"]),
        "swiss_elimination_qualifier_count": int(
            row["swiss_elimination_qualifier_count"]
        ),
    }


def _get_linked_contest_rows(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT contests.id, contests.champion_team_id,
               contests.match_prediction_publication_enabled
        FROM contest_shared_tournaments AS link
        JOIN contests ON contests.id = link.contest_id
        WHERE link.shared_tournament_id = ?
        ORDER BY contests.id
        """,
        (shared_tournament_id,),
    ).fetchall()


def _write_contest_sync_event(
    connection: sqlite3.Connection,
    *,
    contest_id: int,
    actor_user_id: int,
    event_type: str,
    payload: dict[str, object],
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO event_log (
            contest_id, actor_user_id, event_type, entity_type,
            entity_id, payload_json
        ) VALUES (?, ?, ?, 'contest', ?, ?)
        """,
        (
            contest_id,
            actor_user_id,
            event_type,
            contest_id,
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("Не удалось записать синхронизацию общего турнира.")
    return int(cursor.lastrowid)


def _shared_team_count(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS value FROM shared_tournament_teams
        WHERE shared_tournament_id = ?
        """,
        (shared_tournament_id,),
    ).fetchone()
    return int(row["value"]) if row is not None else 0


def _require_shared_team(
    connection: sqlite3.Connection, *, shared_tournament_id: int, team_id: int
) -> None:
    if (
        _get_shared_team_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            team_id=team_id,
        )
        is None
    ):
        raise ValueError("Выбранная команда не входит в общий турнир.")


def _shared_swiss_settings_locked(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> bool:
    row = connection.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM contest_shared_tournaments AS link
                JOIN swiss_stage_predictions AS prediction
                  ON prediction.contest_id = link.contest_id
                WHERE link.shared_tournament_id = ?
            ) OR EXISTS (
                SELECT 1 FROM shared_swiss_stage_result_selections
                WHERE shared_tournament_id = ?
            ) AS value
        """,
        (shared_tournament_id, shared_tournament_id),
    ).fetchone()
    return bool(row["value"]) if row is not None else False


def _get_shared_swiss_result_ids(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    rows = connection.execute(
        """
        SELECT team_id, category
        FROM shared_swiss_stage_result_selections
        WHERE shared_tournament_id = ?
        ORDER BY category, team_id
        """,
        (shared_tournament_id,),
    ).fetchall()
    return (
        tuple(int(row["team_id"]) for row in rows if row["category"] == "direct"),
        tuple(int(row["team_id"]) for row in rows if row["category"] == "elimination"),
    )


def _validate_deadline_change(
    *,
    previous_deadline: object,
    new_deadline: str | None,
    now_utc: datetime,
    field_name: str,
) -> None:
    previous = str(previous_deadline) if previous_deadline is not None else None
    if previous == new_deadline:
        return
    if previous is not None and _parse_datetime(previous) <= now_utc:
        raise SharedTournamentSettingsLockedError(
            f"Дедлайн {field_name} уже наступил и больше не может быть изменён."
        )
    if new_deadline is not None and _parse_datetime(new_deadline) <= now_utc:
        raise ValueError(f"Новый дедлайн {field_name} должен быть в будущем.")


def _require_deadline_passed(
    deadline: object, *, now_utc: datetime, field_name: str
) -> None:
    if deadline is None:
        raise SharedTournamentResultUnavailableError(
            f"Для {field_name} не задан дедлайн."
        )
    if _parse_datetime(str(deadline)) > now_utc:
        raise SharedTournamentResultUnavailableError(
            f"Результат можно указать после дедлайна {field_name}."
        )


def _normalize_optional_deadline(
    value: str | None, *, enabled: bool, field_name: str
) -> str | None:
    if enabled and value is None:
        raise ValueError(f"Укажите дедлайн {field_name}.")
    return _normalize_datetime(value) if value is not None else None


def _normalize_positive_integer(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} должно быть положительным целым числом.")
    return value


def _normalize_non_negative_integer(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} должны быть целым неотрицательным числом.")
    return value


def _normalize_team_id_sets(
    direct_team_ids: list[int], elimination_team_ids: list[int]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not isinstance(direct_team_ids, list) or not isinstance(
        elimination_team_ids, list
    ):
        raise ValueError("Списки команд должны быть массивами.")
    direct = tuple(
        _normalize_positive_integer(team_id, field_name="Идентификатор команды")
        for team_id in direct_team_ids
    )
    elimination = tuple(
        _normalize_positive_integer(team_id, field_name="Идентификатор команды")
        for team_id in elimination_team_ids
    )
    if len(set(direct)) != len(direct) or len(set(elimination)) != len(elimination):
        raise ValueError("Команды в каждой категории не должны повторяться.")
    if set(direct) & set(elimination):
        raise ValueError("Команда не может одновременно находиться в двух категориях.")
    return direct, elimination


def _validate_shared_selection(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    direct_ids: tuple[int, ...],
    elimination_ids: tuple[int, ...],
    direct_count: int,
    elimination_count: int,
) -> None:
    if len(direct_ids) != direct_count or len(elimination_ids) != elimination_count:
        raise ValueError("Количество выбранных команд не соответствует настройкам.")
    for team_id in direct_ids + elimination_ids:
        _require_shared_team(
            connection,
            shared_tournament_id=shared_tournament_id,
            team_id=team_id,
        )


def _record_event(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    shared_match_id: int | None,
    actor_telegram_user_id: int,
    event_type: str,
    before_state: dict[str, object] | None,
    after_state: dict[str, object] | None,
    metadata: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO shared_tournament_events (
            shared_tournament_id, shared_match_id, actor_telegram_user_id,
            event_type, before_state, after_state, metadata
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            shared_tournament_id,
            shared_match_id,
            actor_telegram_user_id,
            _normalize_event_type(event_type),
            _json(before_state),
            _json(after_state),
            _json(metadata),
        ),
    )


def _touch_tournament(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> None:
    connection.execute(
        """
        UPDATE shared_tournaments
        SET version = version + 1, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (shared_tournament_id,),
    )


def _require_expected_version(row: sqlite3.Row, *, expected_version: int) -> None:
    if isinstance(expected_version, bool) or expected_version <= 0:
        raise ValueError("Версия матча должна быть положительным целым числом.")
    if int(row["version"]) != expected_version:
        raise SharedMatchConflictError(
            "Матч уже был изменён. Обновите данные и повторите действие."
        )


def _require_expected_tournament_version(
    row: sqlite3.Row, *, expected_version: int
) -> None:
    if isinstance(expected_version, bool) or expected_version <= 0:
        raise ValueError("Версия турнира должна быть положительным целым числом.")
    if int(row["version"]) != expected_version:
        raise SharedTournamentConflictError(
            "Общий турнир уже был изменён. Обновите данные и повторите действие."
        )


def _normalize_name(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("Введите название общего турнира.")
    if len(normalized) > 80:
        raise ValueError("Название общего турнира не должно быть длиннее 80 символов.")
    return normalized


def _normalize_template_key(value: str) -> str:
    if value not in SUPPORTED_TEMPLATE_KEYS:
        raise ValueError("Неизвестный шаблон общего турнира.")
    return value


def _normalize_team_names(values: list[str]) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("Список команд должен быть массивом.")
    names: list[str] = []
    keys: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Название команды должно быть строкой.")
        name = " ".join(value.split())
        if not name:
            raise ValueError("Название команды не может быть пустым.")
        if len(name) > 80:
            raise ValueError("Название команды не должно быть длиннее 80 символов.")
        key = name.casefold()
        if key in keys:
            raise ValueError("Названия команд общего турнира не должны повторяться.")
        keys.add(key)
        names.append(name)
    return tuple(names)


def _find_or_create_team(connection: sqlite3.Connection, *, team_name: str) -> int:
    rows = connection.execute("SELECT id, name FROM teams ORDER BY id").fetchall()
    for row in rows:
        if str(row["name"]).casefold() == team_name.casefold():
            return int(row["id"])
    return int(
        connection.execute(
            "INSERT INTO teams (name) VALUES (?)", (team_name,)
        ).lastrowid
    )


def _normalize_best_of(value: int | None, *, template_key: str) -> int | None:
    if template_key == "the_international_2026":
        if isinstance(value, bool) or value not in (3, 5):
            raise ValueError("Для серии The International выберите Bo3 или Bo5.")
        return value
    if value is not None:
        raise ValueError("Формат Bo3/Bo5 доступен только для The International.")
    return None


def _normalize_datetime(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Дата и время начала матча должны быть строкой.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Некорректная дата и время начала матча.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Дата и время начала матча должны содержать часовой пояс.")
    return _serialize_datetime(parsed.astimezone(timezone.utc))


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("У общего матча сохранена некорректная дата.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("У общего матча сохранена дата без часового пояса.")
    return parsed.astimezone(timezone.utc)


def _resolve_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Текущее время должно содержать часовой пояс.")
    return value.astimezone(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _normalize_score(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Счёт должен быть целым числом.")
    if value < 0:
        raise ValueError("Счёт не может быть отрицательным.")
    return value


def _resolve_advancing_team(
    match_row: sqlite3.Row,
    *,
    template_key: str,
    home_score: int,
    away_score: int,
    advancing_team_id: int | None,
) -> int:
    home_team_id = int(match_row["home_team_id"])
    away_team_id = int(match_row["away_team_id"])
    if template_key == "the_international_2026":
        best_of = int(match_row["best_of"])
        wins_required = best_of // 2 + 1
        if home_score == wins_required and 0 <= away_score < wins_required:
            expected = home_team_id
        elif away_score == wins_required and 0 <= home_score < wins_required:
            expected = away_team_id
        else:
            raise ValueError(
                f"Для Bo{best_of} победитель должен выиграть {wins_required} карты."
            )
        if advancing_team_id not in (None, expected):
            raise ValueError("Прошедшая команда должна совпадать с победителем серии.")
        return expected
    if advancing_team_id not in {home_team_id, away_team_id}:
        raise ValueError("Укажите команду, прошедшую дальше.")
    expected = home_team_id if home_score > away_score else away_team_id
    if home_score != away_score and advancing_team_id != expected:
        raise ValueError("Прошедшая команда должна совпадать с победителем по счёту.")
    return advancing_team_id


def _upsert_actor_user(
    connection: sqlite3.Connection,
    *,
    telegram_user_id: int,
    first_name: str,
    last_name: str | None,
    username: str | None,
) -> int:
    connection.execute(
        """
        INSERT INTO users (telegram_user_id, username, first_name, last_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_name = excluded.last_name
        """,
        (telegram_user_id, username, first_name, last_name),
    )
    row = connection.execute(
        "SELECT id FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("Не удалось определить редактора общего турнира.")
    return int(row["id"])


def _normalize_event_type(value: str) -> str:
    if not value or len(value) > 80:
        raise ValueError("Некорректный тип события общего турнира.")
    return value


def _json(value: dict[str, object] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )

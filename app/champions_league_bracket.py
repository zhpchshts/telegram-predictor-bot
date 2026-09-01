from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from app.database import database_connection


CHAMPIONS_LEAGUE_TEMPLATE_KEY = "champions_league_2026_27"

RoundKey = Literal[
    "playoff",
    "round_of_16",
    "quarterfinal",
    "semifinal",
    "final",
]
NodeFormat = Literal["two_legged", "single"]
NodeSyncStatus = Literal["pending", "materialized", "conflict"]
FixtureImportStatus = Literal["pending", "imported", "conflict", "tombstoned"]


@dataclass(frozen=True, slots=True)
class ChampionsLeagueRound:
    key: RoundKey
    name: str
    stage_position: int
    node_format: NodeFormat
    node_count: int


CHAMPIONS_LEAGUE_ROUNDS: tuple[ChampionsLeagueRound, ...] = (
    ChampionsLeagueRound("playoff", "Стыковые матчи", 10, "two_legged", 8),
    ChampionsLeagueRound("round_of_16", "1/8 финала", 20, "two_legged", 8),
    ChampionsLeagueRound("quarterfinal", "1/4 финала", 30, "two_legged", 4),
    ChampionsLeagueRound("semifinal", "1/2 финала", 40, "two_legged", 2),
    ChampionsLeagueRound("final", "Финал", 50, "single", 1),
)
CHAMPIONS_LEAGUE_BRACKET_NODE_COUNT = sum(
    round_definition.node_count for round_definition in CHAMPIONS_LEAGUE_ROUNDS
)
EXTERNAL_TIE_CLAIM_TIMEOUT = timedelta(minutes=15)
_ROUND_BY_KEY = {
    round_definition.key: round_definition
    for round_definition in CHAMPIONS_LEAGUE_ROUNDS
}


class ChampionsLeagueBracketError(ValueError):
    pass


class ChampionsLeagueBracketConflictError(ChampionsLeagueBracketError):
    pass


@dataclass(frozen=True, slots=True)
class BracketNode:
    id: int
    shared_tournament_id: int
    round_key: RoundKey
    round_name: str
    round_position: int
    bracket_position: int
    node_format: NodeFormat
    first_source_node_id: int | None
    second_source_node_id: int | None
    resolved_first_team_id: int | None
    resolved_second_team_id: int | None
    first_leg_starts_at_utc: str | None
    second_leg_starts_at_utc: str | None
    materialized_shared_tie_id: int | None
    materialized_shared_match_id: int | None
    sync_status: NodeSyncStatus
    sync_error: str | None
    version: int

    @property
    def is_ready_for_materialization(self) -> bool:
        dates_ready = self.first_leg_starts_at_utc is not None and (
            self.node_format == "single" or self.second_leg_starts_at_utc is not None
        )
        return (
            self.sync_status == "pending"
            and self.resolved_first_team_id is not None
            and self.resolved_second_team_id is not None
            and dates_ready
        )


@dataclass(frozen=True, slots=True)
class ChampionsLeagueBracket:
    shared_tournament_id: int
    nodes: tuple[BracketNode, ...]

    @property
    def ready_nodes(self) -> tuple[BracketNode, ...]:
        return tuple(node for node in self.nodes if node.is_ready_for_materialization)


@dataclass(frozen=True, slots=True)
class NodeReconciliation:
    node: BracketNode
    action: Literal["noop", "updated", "conflict"]


@dataclass(frozen=True, slots=True)
class ExternalSourceConfig:
    shared_tournament_id: int
    source: str
    external_event_id: str
    sync_enabled: bool
    enabled_at: str | None
    sync_generation: int
    last_attempt_at: str | None
    last_success_at: str | None
    last_error: str | None
    version: int


@dataclass(frozen=True, slots=True)
class ExternalTeamLink:
    shared_tournament_id: int
    team_id: int
    source: str
    external_team_id: str


@dataclass(frozen=True, slots=True)
class ExternalTieLink:
    id: int
    shared_tournament_id: int
    source: str
    external_event_id: str
    external_tie_id: str
    shared_tie_id: int | None
    round_key: RoundKey | None
    bracket_position: int | None
    materialization_claim: str | None
    claim_started_at: str | None
    tombstoned_at: str | None


@dataclass(frozen=True, slots=True)
class FixtureImport:
    id: int
    shared_tournament_id: int
    source: str
    external_event_id: str
    external_fixture_id: str
    external_tie_id: str | None
    round_key: RoundKey
    bracket_position: int
    leg_number: int | None
    shared_bracket_node_id: int | None
    shared_tie_id: int | None
    shared_match_id: int | None
    payload_hash: str
    provider_updated_at: str | None
    import_status: FixtureImportStatus
    last_error: str | None
    imported_at: str | None
    tombstoned_at: str | None
    version: int


def ensure_champions_league_bracket(
    *, database_path: Path, shared_tournament_id: int
) -> ChampionsLeagueBracket:
    """Create the 23 empty canonical nodes without touching historical fixtures."""

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_champions_league_tournament(
            connection,
            shared_tournament_id=shared_tournament_id,
            require_active=True,
        )
        _ensure_nodes(connection, shared_tournament_id=shared_tournament_id)
        return _get_bracket(connection, shared_tournament_id=shared_tournament_id)


def get_champions_league_bracket(
    *, database_path: Path, shared_tournament_id: int
) -> ChampionsLeagueBracket:
    with database_connection(database_path) as connection:
        _require_champions_league_tournament(
            connection,
            shared_tournament_id=shared_tournament_id,
            require_active=False,
        )
        return _get_bracket(connection, shared_tournament_id=shared_tournament_id)


def configure_bracket_node(
    *,
    database_path: Path,
    shared_tournament_id: int,
    round_key: RoundKey,
    bracket_position: int,
    first_source_node_id: int | None,
    second_source_node_id: int | None,
    resolved_first_team_id: int | None,
    resolved_second_team_id: int | None,
    first_leg_starts_at_utc: str | None,
    second_leg_starts_at_utc: str | None,
    expected_version: int,
) -> NodeReconciliation:
    """Apply provider/draw metadata without rewriting a materialized fixture."""

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_champions_league_tournament(
            connection,
            shared_tournament_id=shared_tournament_id,
            require_active=True,
        )
        row = _get_node_row_by_position(
            connection,
            shared_tournament_id=shared_tournament_id,
            round_key=round_key,
            bracket_position=bracket_position,
        )
        return _configure_node_in_connection(
            connection,
            row=row,
            first_source_node_id=first_source_node_id,
            second_source_node_id=second_source_node_id,
            resolved_first_team_id=resolved_first_team_id,
            resolved_second_team_id=resolved_second_team_id,
            first_leg_starts_at_utc=first_leg_starts_at_utc,
            second_leg_starts_at_utc=second_leg_starts_at_utc,
            expected_version=expected_version,
        )


def reconcile_bracket_node_from_sources(
    *, database_path: Path, shared_tournament_id: int, node_id: int
) -> NodeReconciliation:
    """Resolve upstream winners, failing closed after downstream materialization."""

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_champions_league_tournament(
            connection,
            shared_tournament_id=shared_tournament_id,
            require_active=True,
        )
        row = _get_node_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            node_id=node_id,
        )
        first_team_id = _resolved_source_winner(
            connection, source_node_id=row["first_source_node_id"]
        )
        second_team_id = _resolved_source_winner(
            connection, source_node_id=row["second_source_node_id"]
        )
        requested_first = (
            first_team_id
            if row["first_source_node_id"] is not None
            else row["resolved_first_team_id"]
        )
        requested_second = (
            second_team_id
            if row["second_source_node_id"] is not None
            else row["resolved_second_team_id"]
        )
        return _configure_node_in_connection(
            connection,
            row=row,
            first_source_node_id=_optional_int(row["first_source_node_id"]),
            second_source_node_id=_optional_int(row["second_source_node_id"]),
            resolved_first_team_id=_optional_int(requested_first),
            resolved_second_team_id=_optional_int(requested_second),
            first_leg_starts_at_utc=_optional_str(row["first_leg_starts_at_utc"]),
            second_leg_starts_at_utc=_optional_str(row["second_leg_starts_at_utc"]),
            expected_version=int(row["version"]),
        )


def reconcile_downstream_bracket_nodes(
    *, database_path: Path, shared_tournament_id: int, source_node_id: int
) -> tuple[NodeReconciliation, ...]:
    """Reconcile every immediate consumer of a corrected/completed node."""

    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id
            FROM shared_bracket_nodes
            WHERE shared_tournament_id = ?
              AND (first_source_node_id = ? OR second_source_node_id = ?)
            ORDER BY id
            """,
            (shared_tournament_id, source_node_id, source_node_id),
        ).fetchall()
    return tuple(
        reconcile_bracket_node_from_sources(
            database_path=database_path,
            shared_tournament_id=shared_tournament_id,
            node_id=int(row["id"]),
        )
        for row in rows
    )


def mark_bracket_node_materialized(
    *,
    database_path: Path,
    shared_tournament_id: int,
    node_id: int,
    expected_version: int,
    shared_tie_id: int | None = None,
    shared_match_id: int | None = None,
) -> BracketNode:
    """Bind an existing shared tie/final to a ready node atomically."""

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_champions_league_tournament(
            connection,
            shared_tournament_id=shared_tournament_id,
            require_active=True,
        )
        row = _get_node_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            node_id=node_id,
        )
        _require_version(row, expected_version=expected_version)
        node_format = str(row["node_format"])
        current_tie_id = _optional_int(row["materialized_shared_tie_id"])
        current_match_id = _optional_int(row["materialized_shared_match_id"])
        if current_tie_id is not None or current_match_id is not None:
            if current_tie_id == shared_tie_id and current_match_id == shared_match_id:
                return _node_from_row(row)
            return _set_node_conflict(
                connection,
                row=row,
                message="Узел уже связан с другой материализованной фикстурой.",
            )
        _require_node_ready_for_materialization(row)
        if node_format == "two_legged":
            if shared_tie_id is None or shared_match_id is not None:
                raise ChampionsLeagueBracketError(
                    "Для двухматчевого узла укажите только shared_tie_id."
                )
            _validate_and_label_materialized_tie(
                connection,
                row=row,
                shared_tournament_id=shared_tournament_id,
                shared_tie_id=shared_tie_id,
            )
        else:
            if shared_match_id is None or shared_tie_id is not None:
                raise ChampionsLeagueBracketError(
                    "Для финального узла укажите только shared_match_id."
                )
            _validate_and_label_materialized_match(
                connection,
                row=row,
                shared_tournament_id=shared_tournament_id,
                shared_match_id=shared_match_id,
            )

        updated = connection.execute(
            """
            UPDATE shared_bracket_nodes
            SET materialized_shared_tie_id = ?,
                materialized_shared_match_id = ?,
                sync_status = 'materialized',
                sync_error = NULL,
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND version = ?
            """,
            (shared_tie_id, shared_match_id, node_id, expected_version),
        )
        if updated.rowcount != 1:
            raise ChampionsLeagueBracketConflictError("Узел сетки уже изменился.")
        return _node_from_row(
            _get_node_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                node_id=node_id,
            )
        )


def sync_materialized_node_dates(
    *,
    database_path: Path,
    shared_tournament_id: int,
    node_id: int,
    expected_version: int,
    first_leg_starts_at_utc: str,
    second_leg_starts_at_utc: str | None,
) -> BracketNode:
    """Mirror dates only after the underlying shared match update has succeeded."""

    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_champions_league_tournament(
            connection,
            shared_tournament_id=shared_tournament_id,
            require_active=True,
        )
        row = _get_node_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            node_id=node_id,
        )
        _require_version(row, expected_version=expected_version)
        first_start = _normalize_optional_datetime(first_leg_starts_at_utc)
        second_start = _normalize_optional_datetime(second_leg_starts_at_utc)
        if first_start is None:
            raise ChampionsLeagueBracketError("Дата первого матча обязательна.")
        _require_materialized_dates_match(
            connection,
            row=row,
            first_leg_starts_at_utc=first_start,
            second_leg_starts_at_utc=second_start,
        )
        connection.execute(
            """
            UPDATE shared_bracket_nodes
            SET first_leg_starts_at_utc = ?, second_leg_starts_at_utc = ?,
                version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND version = ?
            """,
            (first_start, second_start, node_id, expected_version),
        )
        return _node_from_row(
            _get_node_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                node_id=node_id,
            )
        )


def configure_external_source(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    external_event_id: str,
    sync_enabled: bool,
    expected_version: int | None = None,
    expected_tournament_version: int | None = None,
    now_utc: datetime | None = None,
) -> ExternalSourceConfig:
    normalized_source = _normalize_identity(source, field_name="Источник")
    normalized_event = _normalize_identity(
        external_event_id, field_name="Внешний турнир"
    )
    now = _format_datetime(now_utc or datetime.now(timezone.utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tournament_row = _require_champions_league_tournament(
            connection,
            shared_tournament_id=shared_tournament_id,
            require_active=True,
        )
        if expected_tournament_version is not None:
            _require_version(
                tournament_row, expected_version=expected_tournament_version
            )
        existing = connection.execute(
            """
            SELECT * FROM shared_tournament_external_sources
            WHERE shared_tournament_id = ? AND source = ?
            """,
            (shared_tournament_id, normalized_source),
        ).fetchone()
        if existing is None:
            if expected_version is not None:
                raise ChampionsLeagueBracketConflictError(
                    "Настройка источника ещё не существует."
                )
            try:
                connection.execute(
                    """
                    INSERT INTO shared_tournament_external_sources (
                        shared_tournament_id, source, external_event_id,
                        sync_enabled, enabled_at, sync_generation
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (
                        shared_tournament_id,
                        normalized_source,
                        normalized_event,
                        int(sync_enabled),
                        now if sync_enabled else None,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ChampionsLeagueBracketConflictError(
                    "Этот внешний турнир уже подключён к другому общему турниру."
                ) from error
        else:
            if (
                str(existing["external_event_id"]) == normalized_event
                and bool(existing["sync_enabled"]) == sync_enabled
            ):
                return _external_source_from_row(existing)
            if expected_version is None:
                expected_version = int(existing["version"])
            _require_version(existing, expected_version=expected_version)
            enabled_at = existing["enabled_at"]
            event_changed = str(existing["external_event_id"]) != normalized_event
            if sync_enabled and (not bool(existing["sync_enabled"]) or event_changed):
                enabled_at = now
            elif not sync_enabled:
                enabled_at = None
            last_attempt_at = None if event_changed else existing["last_attempt_at"]
            last_success_at = None if event_changed else existing["last_success_at"]
            last_error = None if event_changed else existing["last_error"]
            updated = connection.execute(
                """
                UPDATE shared_tournament_external_sources
                SET external_event_id = ?, sync_enabled = ?, enabled_at = ?,
                    last_attempt_at = ?, last_success_at = ?, last_error = ?,
                    sync_generation = sync_generation + 1,
                    version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE shared_tournament_id = ? AND source = ? AND version = ?
                """,
                (
                    normalized_event,
                    int(sync_enabled),
                    enabled_at,
                    last_attempt_at,
                    last_success_at,
                    last_error,
                    shared_tournament_id,
                    normalized_source,
                    expected_version,
                ),
            )
            if updated.rowcount != 1:
                raise ChampionsLeagueBracketConflictError(
                    "Настройки источника уже изменились."
                )
        current_tournament_version = int(tournament_row["version"])
        touched = connection.execute(
            """
            UPDATE shared_tournaments
            SET version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND version = ?
            """,
            (shared_tournament_id, current_tournament_version),
        )
        if touched.rowcount != 1:
            raise ChampionsLeagueBracketConflictError(
                "Общий турнир уже изменился; перечитайте данные и повторите действие."
            )
        return _external_source_from_row(
            _get_external_source_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                source=normalized_source,
            )
        )


def get_external_source(
    *, database_path: Path, shared_tournament_id: int, source: str
) -> ExternalSourceConfig | None:
    normalized_source = _normalize_identity(source, field_name="Источник")
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM shared_tournament_external_sources
            WHERE shared_tournament_id = ? AND source = ?
            """,
            (shared_tournament_id, normalized_source),
        ).fetchone()
        return _external_source_from_row(row) if row is not None else None


def list_enabled_external_sources(
    *, database_path: Path
) -> tuple[ExternalSourceConfig, ...]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT source_config.*
            FROM shared_tournament_external_sources AS source_config
            JOIN shared_tournaments AS tournament
              ON tournament.id = source_config.shared_tournament_id
            WHERE source_config.sync_enabled = 1
              AND tournament.is_archived = 0
              AND tournament.template_key = ?
            ORDER BY source_config.shared_tournament_id, source_config.source
            """,
            (CHAMPIONS_LEAGUE_TEMPLATE_KEY,),
        ).fetchall()
    return tuple(_external_source_from_row(row) for row in rows)


def list_enabled_sync_targets(
    *, database_path: Path
) -> tuple[ExternalSourceConfig, ...]:
    """Return active Champions League targets, grouped by source/event by callers."""

    return list_enabled_external_sources(database_path=database_path)


def list_enabled_sync_tournament_ids(
    *, database_path: Path, source: str, external_event_id: str
) -> tuple[int, ...]:
    """Compatibility-focused worker query for one provider season."""

    normalized_source = _normalize_identity(source, field_name="Источник")
    normalized_event = _normalize_identity(
        external_event_id, field_name="Внешний турнир"
    )
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT source_config.shared_tournament_id
            FROM shared_tournament_external_sources AS source_config
            JOIN shared_tournaments AS tournament
              ON tournament.id = source_config.shared_tournament_id
            WHERE source_config.source = ?
              AND source_config.external_event_id = ?
              AND source_config.sync_enabled = 1
              AND tournament.is_archived = 0
              AND tournament.template_key = ?
            ORDER BY source_config.shared_tournament_id
            """,
            (normalized_source, normalized_event, CHAMPIONS_LEAGUE_TEMPLATE_KEY),
        ).fetchall()
    return tuple(int(row["shared_tournament_id"]) for row in rows)


def record_sync_attempt(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    attempted_at: datetime,
) -> ExternalSourceConfig:
    normalized_source = _normalize_identity(source, field_name="Источник")
    attempted = _format_datetime(attempted_at)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        source_row = _get_external_source_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            source=normalized_source,
        )
        if source_row["last_attempt_at"] is not None and _parse_datetime(
            attempted
        ) < _parse_datetime(str(source_row["last_attempt_at"])):
            return _external_source_from_row(source_row)
        connection.execute(
            """
            UPDATE shared_tournament_external_sources
            SET last_attempt_at = ?, version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE shared_tournament_id = ? AND source = ?
            """,
            (attempted, shared_tournament_id, normalized_source),
        )
        return _external_source_from_row(
            _get_external_source_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                source=normalized_source,
            )
        )


def record_sync_success(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    completed_at: datetime,
) -> ExternalSourceConfig:
    return update_external_source_sync_status(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        source=source,
        attempted_at=completed_at,
        success=True,
    )


def record_sync_failure(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    attempted_at: datetime,
    error: str,
) -> ExternalSourceConfig:
    return update_external_source_sync_status(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        source=source,
        attempted_at=attempted_at,
        success=False,
        error=error,
    )


def record_external_sync_failure(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    external_event_id: str,
    error_message: str,
    now_utc: datetime,
) -> ExternalSourceConfig:
    config = get_external_source(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        source=source,
    )
    if config is None or config.external_event_id != external_event_id:
        raise ChampionsLeagueBracketConflictError(
            "Источник синхронизации не совпадает с настройками турнира."
        )
    return record_sync_failure(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        source=source,
        attempted_at=now_utc,
        error=error_message,
    )


def update_external_source_sync_status(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    attempted_at: datetime,
    success: bool,
    error: str | None = None,
) -> ExternalSourceConfig:
    normalized_source = _normalize_identity(source, field_name="Источник")
    attempted = _format_datetime(attempted_at)
    normalized_error = _normalize_optional_error(error)
    if success and normalized_error is not None:
        raise ChampionsLeagueBracketError(
            "Успешная синхронизация не может содержать ошибку."
        )
    if not success and normalized_error is None:
        raise ChampionsLeagueBracketError(
            "Для неуспешной синхронизации требуется описание ошибки."
        )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        source_row = _get_external_source_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            source=normalized_source,
        )
        if source_row["last_attempt_at"] is not None and _parse_datetime(
            attempted
        ) < _parse_datetime(str(source_row["last_attempt_at"])):
            return _external_source_from_row(source_row)
        connection.execute(
            """
            UPDATE shared_tournament_external_sources
            SET last_attempt_at = ?,
                last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                last_error = ?,
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE shared_tournament_id = ? AND source = ?
            """,
            (
                attempted,
                int(success),
                attempted,
                normalized_error,
                shared_tournament_id,
                normalized_source,
            ),
        )
        return _external_source_from_row(
            _get_external_source_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                source=normalized_source,
            )
        )


def set_external_team_link(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    team_id: int,
    external_team_id: str,
) -> ExternalTeamLink:
    normalized_source = _normalize_identity(source, field_name="Источник")
    normalized_external_id = _normalize_identity(
        external_team_id, field_name="Внешняя команда"
    )
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _get_external_source_row(
            connection,
            shared_tournament_id=shared_tournament_id,
            source=normalized_source,
        )
        if (
            connection.execute(
                """
            SELECT 1 FROM shared_tournament_teams
            WHERE shared_tournament_id = ? AND team_id = ?
            """,
                (shared_tournament_id, team_id),
            ).fetchone()
            is None
        ):
            raise ChampionsLeagueBracketError(
                "Команда не входит в состав общего турнира."
            )
        existing = connection.execute(
            """
            SELECT team_id, external_team_id
            FROM shared_team_external_links
            WHERE shared_tournament_id = ? AND source = ?
              AND (team_id = ? OR external_team_id = ?)
            """,
            (
                shared_tournament_id,
                normalized_source,
                team_id,
                normalized_external_id,
            ),
        ).fetchall()
        if existing:
            if len(existing) == 1 and (
                int(existing[0]["team_id"]) == team_id
                and str(existing[0]["external_team_id"]) == normalized_external_id
            ):
                return ExternalTeamLink(
                    shared_tournament_id=shared_tournament_id,
                    team_id=team_id,
                    source=normalized_source,
                    external_team_id=normalized_external_id,
                )
            raise ChampionsLeagueBracketConflictError(
                "Внешняя команда или команда турнира уже имеет другую привязку."
            )
        connection.execute(
            """
            INSERT INTO shared_team_external_links (
                shared_tournament_id, team_id, source, external_team_id
            ) VALUES (?, ?, ?, ?)
            """,
            (
                shared_tournament_id,
                team_id,
                normalized_source,
                normalized_external_id,
            ),
        )
        return ExternalTeamLink(
            shared_tournament_id=shared_tournament_id,
            team_id=team_id,
            source=normalized_source,
            external_team_id=normalized_external_id,
        )


def list_external_team_links(
    *, database_path: Path, shared_tournament_id: int, source: str
) -> tuple[ExternalTeamLink, ...]:
    normalized_source = _normalize_identity(source, field_name="Источник")
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM shared_team_external_links
            WHERE shared_tournament_id = ? AND source = ?
            ORDER BY team_id
            """,
            (shared_tournament_id, normalized_source),
        ).fetchall()
    return tuple(
        ExternalTeamLink(
            shared_tournament_id=int(row["shared_tournament_id"]),
            team_id=int(row["team_id"]),
            source=str(row["source"]),
            external_team_id=str(row["external_team_id"]),
        )
        for row in rows
    )


def claim_external_tie_for_materialization(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    external_event_id: str,
    external_tie_id: str,
    round_key: RoundKey,
    bracket_position: int,
    claim_token: str,
    now_utc: datetime | None = None,
) -> ExternalTieLink:
    """Claim one external tie before creating a shared fixture.

    A live competing claim fails closed. A claim abandoned for at least fifteen
    minutes may be taken over; tombstones and already-bound links are returned
    without being changed.
    """

    normalized_source = _normalize_identity(source, field_name="Источник")
    normalized_event = _normalize_identity(
        external_event_id, field_name="Внешний турнир"
    )
    normalized_external_tie = _normalize_identity(
        external_tie_id, field_name="Внешнее противостояние"
    )
    round_definition = _require_round(round_key)
    if round_definition.node_format != "two_legged":
        raise ChampionsLeagueBracketError(
            "Claim внешнего противостояния доступен только для двухматчевого раунда."
        )
    _validate_bracket_position(round_definition, bracket_position)
    normalized_claim = _normalize_identity(claim_token, field_name="Токен claim")
    claimed_at = _format_datetime(now_utc or datetime.now(timezone.utc))
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_external_source_identity(
            connection,
            shared_tournament_id=shared_tournament_id,
            source=normalized_source,
            external_event_id=normalized_event,
        )
        existing = connection.execute(
            """
            SELECT * FROM shared_tie_external_links
            WHERE shared_tournament_id = ? AND source = ?
              AND external_event_id = ? AND external_tie_id = ?
            """,
            (
                shared_tournament_id,
                normalized_source,
                normalized_event,
                normalized_external_tie,
            ),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO shared_tie_external_links (
                    shared_tournament_id, source, external_event_id,
                    external_tie_id, round_key, bracket_position,
                    materialization_claim, claim_started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shared_tournament_id,
                    normalized_source,
                    normalized_event,
                    normalized_external_tie,
                    round_key,
                    bracket_position,
                    normalized_claim,
                    claimed_at,
                ),
            )
        elif existing["tombstoned_at"] is not None:
            connection.execute(
                "UPDATE shared_tie_external_links "
                "SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(existing["id"]),),
            )
        else:
            existing_round = _optional_str(existing["round_key"])
            existing_position = _optional_int(existing["bracket_position"])
            if (existing_round is None) != (existing_position is None):
                raise ChampionsLeagueBracketConflictError(
                    "У внешнего противостояния повреждена позиция сетки."
                )
            if existing_round is not None and (
                existing_round != round_key or existing_position != bracket_position
            ):
                raise ChampionsLeagueBracketConflictError(
                    "Внешнее противостояние уже относится к другой позиции сетки."
                )
            if existing_round is None:
                connection.execute(
                    """
                    UPDATE shared_tie_external_links
                    SET round_key = ?, bracket_position = ?
                    WHERE id = ? AND round_key IS NULL AND bracket_position IS NULL
                    """,
                    (round_key, bracket_position, int(existing["id"])),
                )
            if existing["shared_tie_id"] is not None:
                connection.execute(
                    "UPDATE shared_tie_external_links "
                    "SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (int(existing["id"]),),
                )
            else:
                current_claim = _optional_str(existing["materialization_claim"])
                claim_started_at = _optional_str(existing["claim_started_at"])
                claim_expired = claim_started_at is None or (
                    _parse_datetime(claim_started_at) + EXTERNAL_TIE_CLAIM_TIMEOUT
                    <= _parse_datetime(claimed_at)
                )
                if current_claim not in (None, normalized_claim) and not claim_expired:
                    raise ChampionsLeagueBracketConflictError(
                        "Внешнее противостояние уже материализуется другим процессом."
                    )
                connection.execute(
                    """
                    UPDATE shared_tie_external_links
                    SET materialization_claim = ?, claim_started_at = ?,
                        last_seen_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND shared_tie_id IS NULL AND tombstoned_at IS NULL
                    """,
                    (normalized_claim, claimed_at, int(existing["id"])),
                )
        row = connection.execute(
            """
            SELECT * FROM shared_tie_external_links
            WHERE shared_tournament_id = ? AND source = ?
              AND external_event_id = ? AND external_tie_id = ?
            """,
            (
                shared_tournament_id,
                normalized_source,
                normalized_event,
                normalized_external_tie,
            ),
        ).fetchone()
        return _external_tie_link_from_row(row)


def record_external_tie_link(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    external_event_id: str,
    external_tie_id: str,
    shared_tie_id: int,
    claim_token: str,
) -> ExternalTieLink:
    source = _normalize_identity(source, field_name="Источник")
    external_event_id = _normalize_identity(
        external_event_id, field_name="Внешний турнир"
    )
    external_tie_id = _normalize_identity(
        external_tie_id, field_name="Внешнее противостояние"
    )
    claim_token = _normalize_identity(claim_token, field_name="Токен claim")
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_external_source_identity(
            connection,
            shared_tournament_id=shared_tournament_id,
            source=source,
            external_event_id=external_event_id,
        )
        _require_shared_tie(
            connection,
            shared_tournament_id=shared_tournament_id,
            shared_tie_id=shared_tie_id,
        )
        existing = connection.execute(
            """
            SELECT * FROM shared_tie_external_links
            WHERE shared_tournament_id = ? AND source = ?
              AND external_event_id = ? AND external_tie_id = ?
            """,
            (shared_tournament_id, source, external_event_id, external_tie_id),
        ).fetchone()
        if existing is not None:
            if existing["tombstoned_at"] is not None:
                raise ChampionsLeagueBracketConflictError(
                    "Внешнее противостояние является tombstone и не будет создано повторно."
                )
            current_tie_id = _optional_int(existing["shared_tie_id"])
            if current_tie_id is not None and current_tie_id != shared_tie_id:
                raise ChampionsLeagueBracketConflictError(
                    "Внешнее противостояние уже связано с другой парой."
                )
            if current_tie_id is None:
                if _optional_str(existing["materialization_claim"]) != claim_token:
                    raise ChampionsLeagueBracketConflictError(
                        "Claim внешнего противостояния принадлежит другому процессу."
                    )
                bound = connection.execute(
                    """
                    UPDATE shared_tie_external_links
                    SET shared_tie_id = ?, materialization_claim = NULL,
                        claim_started_at = NULL, last_seen_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND shared_tie_id IS NULL
                      AND materialization_claim = ? AND tombstoned_at IS NULL
                    """,
                    (shared_tie_id, int(existing["id"]), claim_token),
                )
                if bound.rowcount != 1:
                    raise ChampionsLeagueBracketConflictError(
                        "Claim внешнего противостояния уже изменился."
                    )
            else:
                connection.execute(
                    "UPDATE shared_tie_external_links "
                    "SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (int(existing["id"]),),
                )
        else:
            raise ChampionsLeagueBracketConflictError(
                "Сначала зарезервируйте внешнее противостояние для материализации."
            )
        row = connection.execute(
            """
            SELECT * FROM shared_tie_external_links
            WHERE shared_tournament_id = ? AND source = ?
              AND external_event_id = ? AND external_tie_id = ?
            """,
            (shared_tournament_id, source, external_event_id, external_tie_id),
        ).fetchone()
        return _external_tie_link_from_row(row)


def get_external_tie_link(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    external_event_id: str,
    external_tie_id: str,
) -> ExternalTieLink | None:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM shared_tie_external_links
            WHERE shared_tournament_id = ? AND source = ?
              AND external_event_id = ? AND external_tie_id = ?
            """,
            (
                shared_tournament_id,
                _normalize_identity(source, field_name="Источник"),
                _normalize_identity(external_event_id, field_name="Внешний турнир"),
                _normalize_identity(
                    external_tie_id, field_name="Внешнее противостояние"
                ),
            ),
        ).fetchone()
        return _external_tie_link_from_row(row) if row is not None else None


def record_fixture_seen(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str,
    external_event_id: str,
    external_fixture_id: str,
    round_key: RoundKey,
    bracket_position: int,
    leg_number: int | None,
    payload: bytes | str,
    external_tie_id: str | None = None,
    provider_updated_at: str | None = None,
) -> FixtureImport:
    round_definition = _require_round(round_key)
    _validate_bracket_position(round_definition, bracket_position)
    _validate_leg_number(round_definition, leg_number)
    payload_hash = hashlib.sha256(
        payload if isinstance(payload, bytes) else payload.encode("utf-8")
    ).hexdigest()
    source = _normalize_identity(source, field_name="Источник")
    external_event_id = _normalize_identity(
        external_event_id, field_name="Внешний турнир"
    )
    external_fixture_id = _normalize_identity(
        external_fixture_id, field_name="Внешний матч"
    )
    external_tie_id = (
        _normalize_identity(external_tie_id, field_name="Внешнее противостояние")
        if external_tie_id is not None
        else None
    )
    provider_updated_at = _normalize_optional_datetime(provider_updated_at)
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_external_source_identity(
            connection,
            shared_tournament_id=shared_tournament_id,
            source=source,
            external_event_id=external_event_id,
        )
        node = _get_node_row_by_position(
            connection,
            shared_tournament_id=shared_tournament_id,
            round_key=round_key,
            bracket_position=bracket_position,
        )
        existing = connection.execute(
            """
            SELECT * FROM shared_fixture_imports
            WHERE shared_tournament_id = ? AND source = ?
              AND external_event_id = ? AND external_fixture_id = ?
            """,
            (shared_tournament_id, source, external_event_id, external_fixture_id),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO shared_fixture_imports (
                    shared_tournament_id, source, external_event_id,
                    external_fixture_id, external_tie_id, round_key,
                    bracket_position, leg_number, shared_bracket_node_id,
                    payload_hash, provider_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shared_tournament_id,
                    source,
                    external_event_id,
                    external_fixture_id,
                    external_tie_id,
                    round_key,
                    bracket_position,
                    leg_number,
                    int(node["id"]),
                    payload_hash,
                    provider_updated_at,
                ),
            )
        else:
            existing_provider_updated_at = _optional_str(
                existing["provider_updated_at"]
            )
            if str(existing["import_status"]) == "tombstoned":
                connection.execute(
                    """
                    UPDATE shared_fixture_imports
                    SET last_seen_at = CURRENT_TIMESTAMP, version = version + 1
                    WHERE id = ?
                    """,
                    (int(existing["id"]),),
                )
                return _fixture_import_from_row(
                    _get_fixture_import_row_by_id(connection, int(existing["id"]))
                )
            if (
                existing_provider_updated_at is not None
                and provider_updated_at is not None
                and _parse_datetime(provider_updated_at)
                < _parse_datetime(existing_provider_updated_at)
            ):
                raise ChampionsLeagueBracketConflictError(
                    "Получен устаревший snapshot внешней фикстуры."
                )
            metadata_changed = (
                _optional_str(existing["external_tie_id"]) != external_tie_id
                or str(existing["round_key"]) != round_key
                or int(existing["bracket_position"]) != bracket_position
                or _optional_int(existing["leg_number"]) != leg_number
            )
            changed = str(existing["payload_hash"]) != payload_hash
            next_status = str(existing["import_status"])
            next_error = existing["last_error"]
            if metadata_changed and next_status != "tombstoned":
                next_status = "conflict"
                next_error = (
                    "Источник изменил раунд, позицию или принадлежность "
                    "ранее увиденной фикстуры."
                )
            elif changed and next_status != "tombstoned":
                next_status = "pending"
                next_error = None
            connection.execute(
                """
                UPDATE shared_fixture_imports
                SET shared_bracket_node_id = CASE WHEN ? THEN shared_bracket_node_id ELSE ? END,
                    payload_hash = ?,
                    provider_updated_at = ?, import_status = ?, last_error = ?,
                    last_seen_at = CURRENT_TIMESTAMP, version = version + 1
                WHERE id = ?
                """,
                (
                    int(metadata_changed),
                    int(node["id"]),
                    payload_hash,
                    provider_updated_at or existing_provider_updated_at,
                    next_status,
                    next_error,
                    int(existing["id"]),
                ),
            )
        return _fixture_import_from_row(
            _get_fixture_import_row(
                connection,
                shared_tournament_id=shared_tournament_id,
                source=source,
                external_event_id=external_event_id,
                external_fixture_id=external_fixture_id,
            )
        )


def mark_fixture_imported(
    *,
    database_path: Path,
    fixture_import_id: int,
    shared_match_id: int,
    shared_tie_id: int | None = None,
    expected_version: int,
) -> FixtureImport:
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _get_fixture_import_row_by_id(connection, fixture_import_id)
        if str(row["import_status"]) == "tombstoned":
            raise ChampionsLeagueBracketConflictError(
                "Удалённая внешняя фикстура не будет импортирована повторно."
            )
        _require_version(row, expected_version=expected_version)
        match = connection.execute(
            """
            SELECT shared_tournament_id, shared_tie_id, leg_number,
                   round_key, bracket_position
            FROM shared_matches WHERE id = ?
            """,
            (shared_match_id,),
        ).fetchone()
        if match is None or int(match["shared_tournament_id"]) != int(
            row["shared_tournament_id"]
        ):
            raise ChampionsLeagueBracketError("Общий матч не найден в этом турнире.")
        actual_tie_id = _optional_int(match["shared_tie_id"])
        if actual_tie_id != shared_tie_id:
            raise ChampionsLeagueBracketError(
                "Противостояние внешней фикстуры не совпадает с общим матчем."
            )
        if (
            _optional_str(match["round_key"]) != str(row["round_key"])
            or _optional_int(match["bracket_position"]) != int(row["bracket_position"])
            or _optional_int(match["leg_number"]) != _optional_int(row["leg_number"])
        ):
            raise ChampionsLeagueBracketConflictError(
                "Позиция общего матча не совпадает с внешней фикстурой."
            )
        node = (
            connection.execute(
                "SELECT * FROM shared_bracket_nodes WHERE id = ?",
                (int(row["shared_bracket_node_id"]),),
            ).fetchone()
            if row["shared_bracket_node_id"] is not None
            else None
        )
        if node is None:
            raise ChampionsLeagueBracketConflictError(
                "Узел внешней фикстуры больше не существует."
            )
        if shared_tie_id is None:
            materialized_matches = _optional_int(node["materialized_shared_match_id"])
            materialized_ties = _optional_int(node["materialized_shared_tie_id"])
            materialization_matches = materialized_matches == shared_match_id
        else:
            materialized_ties = _optional_int(node["materialized_shared_tie_id"])
            materialized_matches = _optional_int(node["materialized_shared_match_id"])
            materialization_matches = materialized_ties == shared_tie_id
        if (
            not materialization_matches
            or (shared_tie_id is None and materialized_ties is not None)
            or (shared_tie_id is not None and materialized_matches is not None)
        ):
            raise ChampionsLeagueBracketConflictError(
                "Материализованный узел не совпадает с внешней фикстурой."
            )
        connection.execute(
            """
            UPDATE shared_fixture_imports
            SET shared_match_id = ?, shared_tie_id = ?,
                import_status = 'imported', last_error = NULL,
                imported_at = CURRENT_TIMESTAMP, tombstoned_at = NULL,
                last_seen_at = CURRENT_TIMESTAMP, version = version + 1
            WHERE id = ? AND version = ?
            """,
            (
                shared_match_id,
                shared_tie_id,
                fixture_import_id,
                expected_version,
            ),
        )
        return _fixture_import_from_row(
            _get_fixture_import_row_by_id(connection, fixture_import_id)
        )


def mark_fixture_conflict(
    *,
    database_path: Path,
    fixture_import_id: int,
    error: str,
    expected_version: int,
) -> FixtureImport:
    normalized_error = _normalize_optional_error(error)
    if normalized_error is None:
        raise ChampionsLeagueBracketError("Описание конфликта не может быть пустым.")
    with database_connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _get_fixture_import_row_by_id(connection, fixture_import_id)
        if str(row["import_status"]) == "tombstoned":
            return _fixture_import_from_row(row)
        _require_version(row, expected_version=expected_version)
        connection.execute(
            """
            UPDATE shared_fixture_imports
            SET import_status = 'conflict', last_error = ?,
                last_seen_at = CURRENT_TIMESTAMP, version = version + 1
            WHERE id = ? AND version = ?
            """,
            (normalized_error, fixture_import_id, expected_version),
        )
        return _fixture_import_from_row(
            _get_fixture_import_row_by_id(connection, fixture_import_id)
        )


def list_fixture_imports(
    *,
    database_path: Path,
    shared_tournament_id: int,
    source: str | None = None,
) -> tuple[FixtureImport, ...]:
    with database_connection(database_path) as connection:
        if source is None:
            rows = connection.execute(
                """
                SELECT * FROM shared_fixture_imports
                WHERE shared_tournament_id = ?
                ORDER BY id
                """,
                (shared_tournament_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM shared_fixture_imports
                WHERE shared_tournament_id = ? AND source = ?
                ORDER BY id
                """,
                (
                    shared_tournament_id,
                    _normalize_identity(source, field_name="Источник"),
                ),
            ).fetchall()
    return tuple(_fixture_import_from_row(row) for row in rows)


def _ensure_nodes(connection: sqlite3.Connection, *, shared_tournament_id: int) -> None:
    for round_definition in CHAMPIONS_LEAGUE_ROUNDS:
        connection.executemany(
            """
            INSERT INTO shared_bracket_nodes (
                shared_tournament_id, round_key, bracket_position, node_format
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (shared_tournament_id, round_key, bracket_position)
            DO NOTHING
            """,
            [
                (
                    shared_tournament_id,
                    round_definition.key,
                    position,
                    round_definition.node_format,
                )
                for position in range(1, round_definition.node_count + 1)
            ],
        )


def _get_bracket(
    connection: sqlite3.Connection, *, shared_tournament_id: int
) -> ChampionsLeagueBracket:
    rows = connection.execute(
        """
        SELECT * FROM shared_bracket_nodes
        WHERE shared_tournament_id = ?
        ORDER BY CASE round_key
            WHEN 'playoff' THEN 10
            WHEN 'round_of_16' THEN 20
            WHEN 'quarterfinal' THEN 30
            WHEN 'semifinal' THEN 40
            WHEN 'final' THEN 50
            END,
            bracket_position
        """,
        (shared_tournament_id,),
    ).fetchall()
    return ChampionsLeagueBracket(
        shared_tournament_id=shared_tournament_id,
        nodes=tuple(_node_from_row(row) for row in rows),
    )


def _configure_node_in_connection(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    first_source_node_id: int | None,
    second_source_node_id: int | None,
    resolved_first_team_id: int | None,
    resolved_second_team_id: int | None,
    first_leg_starts_at_utc: str | None,
    second_leg_starts_at_utc: str | None,
    expected_version: int,
) -> NodeReconciliation:
    _require_version(row, expected_version=expected_version)
    tournament_id = int(row["shared_tournament_id"])
    round_definition = _require_round(str(row["round_key"]))
    _validate_sources(
        connection,
        row=row,
        first_source_node_id=first_source_node_id,
        second_source_node_id=second_source_node_id,
        resolved_first_team_id=resolved_first_team_id,
        resolved_second_team_id=resolved_second_team_id,
    )
    _validate_resolved_team(
        connection,
        shared_tournament_id=tournament_id,
        team_id=resolved_first_team_id,
    )
    _validate_resolved_team(
        connection,
        shared_tournament_id=tournament_id,
        team_id=resolved_second_team_id,
    )
    if (
        resolved_first_team_id is not None
        and resolved_first_team_id == resolved_second_team_id
    ):
        raise ChampionsLeagueBracketError("В узле должны быть разные команды.")
    first_start = _normalize_optional_datetime(first_leg_starts_at_utc)
    second_start = _normalize_optional_datetime(second_leg_starts_at_utc)
    if round_definition.node_format == "single" and second_start is not None:
        raise ChampionsLeagueBracketError("У финала не может быть ответного матча.")
    if first_start is not None and second_start is not None:
        if _parse_datetime(first_start) >= _parse_datetime(second_start):
            raise ChampionsLeagueBracketError(
                "Ответный матч должен начинаться позже первого."
            )

    requested = (
        first_source_node_id,
        second_source_node_id,
        resolved_first_team_id,
        resolved_second_team_id,
        first_start,
        second_start,
    )
    current = (
        _optional_int(row["first_source_node_id"]),
        _optional_int(row["second_source_node_id"]),
        _optional_int(row["resolved_first_team_id"]),
        _optional_int(row["resolved_second_team_id"]),
        _optional_str(row["first_leg_starts_at_utc"]),
        _optional_str(row["second_leg_starts_at_utc"]),
    )
    is_materialized = (
        row["materialized_shared_tie_id"] is not None
        or row["materialized_shared_match_id"] is not None
    )
    if requested == current:
        return NodeReconciliation(node=_node_from_row(row), action="noop")
    if is_materialized:
        if requested[:4] == current[:4]:
            try:
                _require_materialized_dates_match(
                    connection,
                    row=row,
                    first_leg_starts_at_utc=first_start,
                    second_leg_starts_at_utc=second_start,
                )
            except ChampionsLeagueBracketError:
                pass
            else:
                connection.execute(
                    """
                    UPDATE shared_bracket_nodes
                    SET first_leg_starts_at_utc = ?, second_leg_starts_at_utc = ?,
                        version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND version = ?
                    """,
                    (first_start, second_start, int(row["id"]), expected_version),
                )
                return NodeReconciliation(
                    node=_node_from_row(
                        _get_node_row(
                            connection,
                            shared_tournament_id=tournament_id,
                            node_id=int(row["id"]),
                        )
                    ),
                    action="updated",
                )
        node = _set_node_conflict(
            connection,
            row=row,
            message=(
                "Источник изменил команды, связи или даты уже "
                "материализованного узла; прогнозы не изменены."
            ),
        )
        return NodeReconciliation(node=node, action="conflict")

    updated = connection.execute(
        """
        UPDATE shared_bracket_nodes
        SET first_source_node_id = ?, second_source_node_id = ?,
            resolved_first_team_id = ?, resolved_second_team_id = ?,
            first_leg_starts_at_utc = ?, second_leg_starts_at_utc = ?,
            sync_status = 'pending', sync_error = NULL,
            version = version + 1, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND version = ?
        """,
        (*requested, int(row["id"]), expected_version),
    )
    if updated.rowcount != 1:
        raise ChampionsLeagueBracketConflictError("Узел сетки уже изменился.")
    return NodeReconciliation(
        node=_node_from_row(
            _get_node_row(
                connection,
                shared_tournament_id=tournament_id,
                node_id=int(row["id"]),
            )
        ),
        action="updated",
    )


def _set_node_conflict(
    connection: sqlite3.Connection, *, row: sqlite3.Row, message: str
) -> BracketNode:
    if (
        str(row["sync_status"]) == "conflict"
        and _optional_str(row["sync_error"]) == message
    ):
        return _node_from_row(row)
    connection.execute(
        """
        UPDATE shared_bracket_nodes
        SET sync_status = 'conflict', sync_error = ?,
            version = version + 1, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (message, int(row["id"])),
    )
    return _node_from_row(
        _get_node_row(
            connection,
            shared_tournament_id=int(row["shared_tournament_id"]),
            node_id=int(row["id"]),
        )
    )


def _validate_and_label_materialized_tie(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    shared_tournament_id: int,
    shared_tie_id: int,
) -> None:
    tie = _require_shared_tie(
        connection,
        shared_tournament_id=shared_tournament_id,
        shared_tie_id=shared_tie_id,
    )
    _require_materialized_teams_match(
        row,
        first_team_id=int(tie["first_team_id"]),
        second_team_id=int(tie["second_team_id"]),
    )
    legs = connection.execute(
        """
        SELECT leg_number, starts_at_utc, round_key, bracket_position
        FROM shared_matches
        WHERE shared_tie_id = ?
        ORDER BY leg_number
        """,
        (shared_tie_id,),
    ).fetchall()
    if len(legs) != 2 or [int(leg["leg_number"]) for leg in legs] != [1, 2]:
        raise ChampionsLeagueBracketConflictError(
            "Материализованное противостояние должно содержать ровно два матча."
        )
    _require_requested_dates_match(
        row,
        first_leg_starts_at_utc=str(legs[0]["starts_at_utc"]),
        second_leg_starts_at_utc=str(legs[1]["starts_at_utc"]),
    )
    expected_round = str(row["round_key"])
    expected_position = int(row["bracket_position"])
    if (tie["round_key"] is not None and str(tie["round_key"]) != expected_round) or (
        tie["bracket_position"] is not None
        and int(tie["bracket_position"]) != expected_position
    ):
        raise ChampionsLeagueBracketConflictError(
            "Противостояние уже относится к другой позиции сетки."
        )
    if any(
        (leg["round_key"] is not None and str(leg["round_key"]) != expected_round)
        or (
            leg["bracket_position"] is not None
            and int(leg["bracket_position"]) != expected_position
        )
        for leg in legs
    ):
        raise ChampionsLeagueBracketConflictError(
            "Матчи противостояния уже относятся к другой позиции сетки."
        )
    try:
        if tie["round_key"] is None or tie["bracket_position"] is None:
            connection.execute(
                """
                UPDATE shared_two_legged_ties
                SET round_key = ?, bracket_position = ?,
                    version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (expected_round, expected_position, shared_tie_id),
            )
        if any(
            leg["round_key"] is None or leg["bracket_position"] is None for leg in legs
        ):
            connection.execute(
                """
                UPDATE shared_matches
                SET round_key = ?, bracket_position = ?,
                    version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE shared_tie_id = ?
                  AND (round_key IS NULL OR bracket_position IS NULL)
                """,
                (expected_round, expected_position, shared_tie_id),
            )
    except sqlite3.IntegrityError as error:
        raise ChampionsLeagueBracketConflictError(
            "Эта позиция сетки уже занята другим противостоянием."
        ) from error


def _validate_and_label_materialized_match(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    shared_tournament_id: int,
    shared_match_id: int,
) -> None:
    match = connection.execute(
        """
        SELECT * FROM shared_matches
        WHERE id = ? AND shared_tournament_id = ?
        """,
        (shared_match_id, shared_tournament_id),
    ).fetchone()
    if match is None or match["shared_tie_id"] is not None:
        raise ChampionsLeagueBracketError("Финальный общий матч не найден.")
    _require_materialized_teams_match(
        row,
        first_team_id=int(match["home_team_id"]),
        second_team_id=int(match["away_team_id"]),
    )
    _require_requested_dates_match(
        row,
        first_leg_starts_at_utc=str(match["starts_at_utc"]),
        second_leg_starts_at_utc=None,
    )
    if match["round_key"] is not None and str(match["round_key"]) != "final":
        raise ChampionsLeagueBracketConflictError(
            "Финальный матч уже относится к другому раунду."
        )
    expected_position = int(row["bracket_position"])
    if (
        match["bracket_position"] is not None
        and int(match["bracket_position"]) != expected_position
    ):
        raise ChampionsLeagueBracketConflictError(
            "Финальный матч уже относится к другой позиции сетки."
        )
    try:
        if match["round_key"] is None or match["bracket_position"] is None:
            connection.execute(
                """
                UPDATE shared_matches
                SET round_key = 'final', bracket_position = ?,
                    version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (expected_position, shared_match_id),
            )
    except sqlite3.IntegrityError as error:
        raise ChampionsLeagueBracketConflictError(
            "Финальная позиция сетки уже занята другим матчем."
        ) from error


def _require_materialized_teams_match(
    row: sqlite3.Row, *, first_team_id: int, second_team_id: int
) -> None:
    expected = {
        _optional_int(row["resolved_first_team_id"]),
        _optional_int(row["resolved_second_team_id"]),
    }
    if None in expected or expected != {first_team_id, second_team_id}:
        raise ChampionsLeagueBracketConflictError(
            "Команды материализованной фикстуры не совпадают с узлом сетки."
        )


def _require_node_ready_for_materialization(row: sqlite3.Row) -> None:
    if str(row["sync_status"]) != "pending":
        raise ChampionsLeagueBracketConflictError(
            "Узел с конфликтом нельзя материализовать до согласования данных."
        )
    if (
        row["resolved_first_team_id"] is None
        or row["resolved_second_team_id"] is None
        or row["first_leg_starts_at_utc"] is None
        or (
            str(row["node_format"]) == "two_legged"
            and row["second_leg_starts_at_utc"] is None
        )
    ):
        raise ChampionsLeagueBracketError(
            "Для материализации узла нужны обе команды и даты всех матчей."
        )


def _require_requested_dates_match(
    row: sqlite3.Row,
    *,
    first_leg_starts_at_utc: str,
    second_leg_starts_at_utc: str | None,
) -> None:
    if (
        _optional_str(row["first_leg_starts_at_utc"]) != first_leg_starts_at_utc
        or _optional_str(row["second_leg_starts_at_utc"]) != second_leg_starts_at_utc
    ):
        raise ChampionsLeagueBracketConflictError(
            "Даты материализованной фикстуры не совпадают с узлом сетки."
        )


def _require_materialized_dates_match(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    first_leg_starts_at_utc: str,
    second_leg_starts_at_utc: str | None,
) -> None:
    if row["materialized_shared_tie_id"] is not None:
        legs = connection.execute(
            """
            SELECT starts_at_utc
            FROM shared_matches
            WHERE shared_tie_id = ?
            ORDER BY leg_number
            """,
            (int(row["materialized_shared_tie_id"]),),
        ).fetchall()
        if (
            len(legs) != 2
            or str(legs[0]["starts_at_utc"]) != first_leg_starts_at_utc
            or second_leg_starts_at_utc is None
            or str(legs[1]["starts_at_utc"]) != second_leg_starts_at_utc
        ):
            raise ChampionsLeagueBracketConflictError(
                "Даты узла не совпадают с материализованным противостоянием."
            )
        return
    if row["materialized_shared_match_id"] is not None:
        match = connection.execute(
            "SELECT starts_at_utc FROM shared_matches WHERE id = ?",
            (int(row["materialized_shared_match_id"]),),
        ).fetchone()
        if (
            match is None
            or str(match["starts_at_utc"]) != first_leg_starts_at_utc
            or second_leg_starts_at_utc is not None
        ):
            raise ChampionsLeagueBracketConflictError(
                "Дата узла не совпадает с материализованным финалом."
            )
        return
    raise ChampionsLeagueBracketConflictError("Узел ещё не материализован.")


def _resolved_source_winner(
    connection: sqlite3.Connection, *, source_node_id: object
) -> int | None:
    if source_node_id is None:
        return None
    source = connection.execute(
        "SELECT * FROM shared_bracket_nodes WHERE id = ?", (int(source_node_id),)
    ).fetchone()
    if source is None:
        return None
    if source["materialized_shared_tie_id"] is not None:
        result = connection.execute(
            "SELECT advancing_team_id FROM shared_two_legged_ties WHERE id = ?",
            (int(source["materialized_shared_tie_id"]),),
        ).fetchone()
    elif source["materialized_shared_match_id"] is not None:
        result = connection.execute(
            "SELECT advancing_team_id FROM shared_matches WHERE id = ?",
            (int(source["materialized_shared_match_id"]),),
        ).fetchone()
    else:
        return None
    return _optional_int(result["advancing_team_id"]) if result is not None else None


def _validate_sources(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    first_source_node_id: int | None,
    second_source_node_id: int | None,
    resolved_first_team_id: int | None,
    resolved_second_team_id: int | None,
) -> None:
    if (
        first_source_node_id is not None
        and first_source_node_id == second_source_node_id
    ):
        raise ChampionsLeagueBracketError("Источники сторон должны различаться.")
    previous_round_by_round: dict[RoundKey, RoundKey | None] = {
        "playoff": None,
        "round_of_16": "playoff",
        "quarterfinal": "round_of_16",
        "semifinal": "quarterfinal",
        "final": "semifinal",
    }
    current_round = _require_round(str(row["round_key"]))
    expected_source_round = previous_round_by_round[current_round.key]
    for source_node_id, resolved_team_id in (
        (first_source_node_id, resolved_first_team_id),
        (second_source_node_id, resolved_second_team_id),
    ):
        if source_node_id is None:
            continue
        source = connection.execute(
            """
            SELECT * FROM shared_bracket_nodes
            WHERE id = ? AND shared_tournament_id = ?
            """,
            (source_node_id, int(row["shared_tournament_id"])),
        ).fetchone()
        if source is None:
            raise ChampionsLeagueBracketError("Исходный узел сетки не найден.")
        source_round = _require_round(str(source["round_key"]))
        if expected_source_round is None or source_round.key != expected_source_round:
            raise ChampionsLeagueBracketConflictError(
                "Исходный узел должен относиться к непосредственно предыдущему раунду."
            )
        duplicate_consumer = connection.execute(
            """
            SELECT id FROM shared_bracket_nodes
            WHERE shared_tournament_id = ? AND id != ?
              AND (first_source_node_id = ? OR second_source_node_id = ?)
            LIMIT 1
            """,
            (
                int(row["shared_tournament_id"]),
                int(row["id"]),
                source_node_id,
                source_node_id,
            ),
        ).fetchone()
        if duplicate_consumer is not None:
            raise ChampionsLeagueBracketConflictError(
                "Исходный узел уже используется в другом узле следующего раунда."
            )
        known_winner = _resolved_source_winner(
            connection, source_node_id=source_node_id
        )
        if (
            known_winner is not None
            and resolved_team_id is not None
            and known_winner != resolved_team_id
        ):
            raise ChampionsLeagueBracketConflictError(
                "Команда узла не совпадает с победителем исходного противостояния."
            )


def _validate_resolved_team(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    team_id: int | None,
) -> None:
    if team_id is None:
        return
    if (
        connection.execute(
            """
        SELECT 1 FROM shared_tournament_teams
        WHERE shared_tournament_id = ? AND team_id = ?
        """,
            (shared_tournament_id, team_id),
        ).fetchone()
        is None
    ):
        raise ChampionsLeagueBracketError("Команда не входит в общий турнир.")


def _require_champions_league_tournament(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    require_active: bool,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM shared_tournaments WHERE id = ?", (shared_tournament_id,)
    ).fetchone()
    if row is None:
        raise ChampionsLeagueBracketError("Общий турнир не найден.")
    if str(row["template_key"]) != CHAMPIONS_LEAGUE_TEMPLATE_KEY:
        raise ChampionsLeagueBracketError(
            "Автоматическая футбольная сетка доступна только для Лиги чемпионов."
        )
    if require_active and bool(row["is_archived"]):
        raise ChampionsLeagueBracketConflictError("Общий турнир находится в архиве.")
    return row


def _require_shared_tie(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    shared_tie_id: int,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM shared_two_legged_ties
        WHERE id = ? AND shared_tournament_id = ?
        """,
        (shared_tie_id, shared_tournament_id),
    ).fetchone()
    if row is None:
        raise ChampionsLeagueBracketError("Общее противостояние не найдено.")
    return row


def _get_node_row_by_position(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    round_key: RoundKey,
    bracket_position: int,
) -> sqlite3.Row:
    round_definition = _require_round(round_key)
    _validate_bracket_position(round_definition, bracket_position)
    row = connection.execute(
        """
        SELECT * FROM shared_bracket_nodes
        WHERE shared_tournament_id = ? AND round_key = ? AND bracket_position = ?
        """,
        (shared_tournament_id, round_key, bracket_position),
    ).fetchone()
    if row is None:
        raise ChampionsLeagueBracketError(
            "Узлы сетки ещё не созданы; сначала вызовите ensure bracket."
        )
    return row


def _get_node_row(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    node_id: int,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM shared_bracket_nodes
        WHERE shared_tournament_id = ? AND id = ?
        """,
        (shared_tournament_id, node_id),
    ).fetchone()
    if row is None:
        raise ChampionsLeagueBracketError("Узел сетки не найден.")
    return row


def _get_external_source_row(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    source: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM shared_tournament_external_sources
        WHERE shared_tournament_id = ? AND source = ?
        """,
        (shared_tournament_id, source),
    ).fetchone()
    if row is None:
        raise ChampionsLeagueBracketError("Источник общего турнира не настроен.")
    return row


def _require_external_source_identity(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    source: str,
    external_event_id: str,
) -> sqlite3.Row:
    row = _get_external_source_row(
        connection,
        shared_tournament_id=shared_tournament_id,
        source=source,
    )
    if str(row["external_event_id"]) != external_event_id:
        raise ChampionsLeagueBracketConflictError(
            "Внешний турнир не совпадает с настройками источника."
        )
    return row


def _get_fixture_import_row(
    connection: sqlite3.Connection,
    *,
    shared_tournament_id: int,
    source: str,
    external_event_id: str,
    external_fixture_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM shared_fixture_imports
        WHERE shared_tournament_id = ? AND source = ?
          AND external_event_id = ? AND external_fixture_id = ?
        """,
        (shared_tournament_id, source, external_event_id, external_fixture_id),
    ).fetchone()
    if row is None:
        raise ChampionsLeagueBracketError("Импорт внешней фикстуры не найден.")
    return row


def _get_fixture_import_row_by_id(
    connection: sqlite3.Connection, fixture_import_id: int
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM shared_fixture_imports WHERE id = ?", (fixture_import_id,)
    ).fetchone()
    if row is None:
        raise ChampionsLeagueBracketError("Импорт внешней фикстуры не найден.")
    return row


def _node_from_row(row: sqlite3.Row) -> BracketNode:
    round_definition = _require_round(str(row["round_key"]))
    return BracketNode(
        id=int(row["id"]),
        shared_tournament_id=int(row["shared_tournament_id"]),
        round_key=round_definition.key,
        round_name=round_definition.name,
        round_position=round_definition.stage_position,
        bracket_position=int(row["bracket_position"]),
        node_format=str(row["node_format"]),  # type: ignore[arg-type]
        first_source_node_id=_optional_int(row["first_source_node_id"]),
        second_source_node_id=_optional_int(row["second_source_node_id"]),
        resolved_first_team_id=_optional_int(row["resolved_first_team_id"]),
        resolved_second_team_id=_optional_int(row["resolved_second_team_id"]),
        first_leg_starts_at_utc=_optional_str(row["first_leg_starts_at_utc"]),
        second_leg_starts_at_utc=_optional_str(row["second_leg_starts_at_utc"]),
        materialized_shared_tie_id=_optional_int(row["materialized_shared_tie_id"]),
        materialized_shared_match_id=_optional_int(row["materialized_shared_match_id"]),
        sync_status=str(row["sync_status"]),  # type: ignore[arg-type]
        sync_error=_optional_str(row["sync_error"]),
        version=int(row["version"]),
    )


def _external_source_from_row(row: sqlite3.Row) -> ExternalSourceConfig:
    return ExternalSourceConfig(
        shared_tournament_id=int(row["shared_tournament_id"]),
        source=str(row["source"]),
        external_event_id=str(row["external_event_id"]),
        sync_enabled=bool(row["sync_enabled"]),
        enabled_at=_optional_str(row["enabled_at"]),
        sync_generation=int(row["sync_generation"]),
        last_attempt_at=_optional_str(row["last_attempt_at"]),
        last_success_at=_optional_str(row["last_success_at"]),
        last_error=_optional_str(row["last_error"]),
        version=int(row["version"]),
    )


def _external_tie_link_from_row(row: sqlite3.Row) -> ExternalTieLink:
    return ExternalTieLink(
        id=int(row["id"]),
        shared_tournament_id=int(row["shared_tournament_id"]),
        source=str(row["source"]),
        external_event_id=str(row["external_event_id"]),
        external_tie_id=str(row["external_tie_id"]),
        shared_tie_id=_optional_int(row["shared_tie_id"]),
        round_key=(
            _require_round(str(row["round_key"])).key
            if row["round_key"] is not None
            else None
        ),
        bracket_position=_optional_int(row["bracket_position"]),
        materialization_claim=_optional_str(row["materialization_claim"]),
        claim_started_at=_optional_str(row["claim_started_at"]),
        tombstoned_at=_optional_str(row["tombstoned_at"]),
    )


def _fixture_import_from_row(row: sqlite3.Row) -> FixtureImport:
    return FixtureImport(
        id=int(row["id"]),
        shared_tournament_id=int(row["shared_tournament_id"]),
        source=str(row["source"]),
        external_event_id=str(row["external_event_id"]),
        external_fixture_id=str(row["external_fixture_id"]),
        external_tie_id=_optional_str(row["external_tie_id"]),
        round_key=_require_round(str(row["round_key"])).key,
        bracket_position=int(row["bracket_position"]),
        leg_number=_optional_int(row["leg_number"]),
        shared_bracket_node_id=_optional_int(row["shared_bracket_node_id"]),
        shared_tie_id=_optional_int(row["shared_tie_id"]),
        shared_match_id=_optional_int(row["shared_match_id"]),
        payload_hash=str(row["payload_hash"]),
        provider_updated_at=_optional_str(row["provider_updated_at"]),
        import_status=str(row["import_status"]),  # type: ignore[arg-type]
        last_error=_optional_str(row["last_error"]),
        imported_at=_optional_str(row["imported_at"]),
        tombstoned_at=_optional_str(row["tombstoned_at"]),
        version=int(row["version"]),
    )


def _require_round(value: str) -> ChampionsLeagueRound:
    try:
        return _ROUND_BY_KEY[value]  # type: ignore[index]
    except KeyError as error:
        raise ChampionsLeagueBracketError(f"Неизвестный раунд: {value}.") from error


def _validate_bracket_position(
    round_definition: ChampionsLeagueRound, bracket_position: int
) -> None:
    if isinstance(bracket_position, bool) or not (
        1 <= bracket_position <= round_definition.node_count
    ):
        raise ChampionsLeagueBracketError(
            f"Для раунда {round_definition.name} позиция должна быть от 1 "
            f"до {round_definition.node_count}."
        )


def _validate_leg_number(
    round_definition: ChampionsLeagueRound, leg_number: int | None
) -> None:
    if round_definition.node_format == "single":
        if leg_number is not None:
            raise ChampionsLeagueBracketError("У финала нет номера матча пары.")
    elif isinstance(leg_number, bool) or leg_number not in (1, 2):
        raise ChampionsLeagueBracketError(
            "Для двухматчевого раунда укажите leg_number 1 или 2."
        )


def _require_version(row: sqlite3.Row, *, expected_version: int) -> None:
    if isinstance(expected_version, bool) or expected_version <= 0:
        raise ChampionsLeagueBracketError("Версия должна быть положительной.")
    if int(row["version"]) != expected_version:
        raise ChampionsLeagueBracketConflictError(
            "Данные уже изменились; перечитайте их и повторите действие."
        )


def _normalize_identity(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise ChampionsLeagueBracketError(
            f"{field_name} должен содержать от 1 до 255 символов."
        )
    return normalized


def _normalize_optional_error(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:2000]


def _normalize_optional_datetime(value: str | None) -> str | None:
    if value is None:
        return None
    return _format_datetime(_parse_datetime(value))


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ChampionsLeagueBracketError("Некорректная дата и время.") from error
    if parsed.tzinfo is None:
        raise ChampionsLeagueBracketError("Дата и время должны содержать часовой пояс.")
    return parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ChampionsLeagueBracketError("Дата и время должны содержать часовой пояс.")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None

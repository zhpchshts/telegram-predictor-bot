from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import escape
import json
import logging
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from app.football_data_provider import (
    CHAMPIONS_LEAGUE_TEMPLATE_SEASON_START_YEAR,
    FOOTBALL_DATA_SOURCE,
    ExternalKnockoutMatch,
    FootballDataClient,
    FootballDataKnockoutSnapshot,
    ProviderConflict,
)


LOGGER = logging.getLogger(__name__)
MAX_BACKOFF_MINUTES = 6 * 60


class ChampionsLeagueSyncUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChampionsLeagueSyncTarget:
    shared_tournament_id: int
    enabled_at: str | None = None
    sync_generation: int = 1
    actor_telegram_user_id: int = 1
    actor_first_name: str = "Champions League sync"
    actor_last_name: str | None = None
    actor_username: str | None = None


@dataclass(frozen=True, slots=True)
class TournamentSyncResult:
    created_tie_count: int = 0
    created_match_count: int = 0
    updated_start_count: int = 0
    saved_result_count: int = 0
    conflict_count: int = 0


@dataclass(frozen=True, slots=True)
class ChampionsLeagueSyncCycleResult:
    target_count: int
    fetched: bool
    successful_target_count: int
    failed_target_count: int
    provider_conflict_count: int
    created_tie_count: int
    created_match_count: int
    updated_start_count: int
    saved_result_count: int
    conflict_count: int
    provider_error: str | None = None

    @property
    def failed(self) -> bool:
        return (
            self.provider_error is not None
            or self.failed_target_count > 0
            or self.provider_conflict_count > 0
            or self.conflict_count > 0
        )

    @property
    def retryable_failure(self) -> bool:
        """Only transport/apply failures should slow the global poller."""

        return self.provider_error is not None or self.failed_target_count > 0


class ChampionsLeagueSyncBackend(Protocol):
    def list_enabled_targets(
        self,
        *,
        database_path: Path,
        source: str,
        external_event_id: str,
    ) -> tuple[ChampionsLeagueSyncTarget, ...]: ...

    def apply_snapshot(
        self,
        *,
        database_path: Path,
        target: ChampionsLeagueSyncTarget,
        snapshot: FootballDataKnockoutSnapshot,
        now_utc: datetime,
    ) -> TournamentSyncResult: ...

    def record_failure(
        self,
        *,
        database_path: Path,
        target: ChampionsLeagueSyncTarget,
        source: str,
        external_event_id: str,
        error_message: str,
        now_utc: datetime,
    ) -> None: ...


class DatabaseChampionsLeagueSyncBackend:
    """Bridge to the bracket domain without coupling the worker to its schema."""

    def list_enabled_targets(
        self,
        *,
        database_path: Path,
        source: str,
        external_event_id: str,
    ) -> tuple[ChampionsLeagueSyncTarget, ...]:
        from app.champions_league_bracket import list_enabled_sync_targets
        from app.database import database_connection

        source_configs = tuple(
            item
            for item in list_enabled_sync_targets(database_path=database_path)
            if item.source == source and item.external_event_id == external_event_id
        )
        if not source_configs:
            return ()
        tournament_ids = tuple(item.shared_tournament_id for item in source_configs)
        placeholders = ",".join("?" for _item in tournament_ids)
        with database_connection(database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT tournament.id, tournament.created_by_telegram_user_id,
                       users.first_name, users.last_name, users.username
                FROM shared_tournaments AS tournament
                LEFT JOIN users
                  ON users.telegram_user_id = tournament.created_by_telegram_user_id
                WHERE tournament.id IN ({placeholders})
                ORDER BY tournament.id
                """,
                tournament_ids,
            ).fetchall()
        actors = {int(row["id"]): row for row in rows}
        return tuple(
            ChampionsLeagueSyncTarget(
                shared_tournament_id=config.shared_tournament_id,
                enabled_at=config.enabled_at,
                sync_generation=config.sync_generation,
                actor_telegram_user_id=int(
                    actors[config.shared_tournament_id]["created_by_telegram_user_id"]
                ),
                actor_first_name=(
                    str(actors[config.shared_tournament_id]["first_name"])
                    if actors[config.shared_tournament_id]["first_name"] is not None
                    else "Champions League sync"
                ),
                actor_last_name=(
                    str(actors[config.shared_tournament_id]["last_name"])
                    if actors[config.shared_tournament_id]["last_name"] is not None
                    else None
                ),
                actor_username=(
                    str(actors[config.shared_tournament_id]["username"])
                    if actors[config.shared_tournament_id]["username"] is not None
                    else None
                ),
            )
            for config in source_configs
            if config.shared_tournament_id in actors
        )

    def apply_snapshot(
        self,
        *,
        database_path: Path,
        target: ChampionsLeagueSyncTarget,
        snapshot: FootballDataKnockoutSnapshot,
        now_utc: datetime,
    ) -> TournamentSyncResult:
        from app.champions_league_bracket import (
            record_sync_attempt,
            record_sync_failure,
            record_sync_success,
        )

        record_sync_attempt(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            source=snapshot.source,
            attempted_at=now_utc,
        )
        result = apply_football_data_snapshot(
            database_path=database_path,
            target=target,
            snapshot=snapshot,
            now_utc=now_utc,
        )
        total_conflicts = result.conflict_count + len(snapshot.conflicts)
        if total_conflicts:
            provider_diagnostics = _format_provider_conflicts(snapshot.conflicts)
            error_message = f"Синхронизация завершена с конфликтами: {total_conflicts}."
            if provider_diagnostics is not None:
                error_message = f"{error_message} {provider_diagnostics}"
            record_sync_failure(
                database_path=database_path,
                shared_tournament_id=target.shared_tournament_id,
                source=snapshot.source,
                attempted_at=now_utc,
                error=error_message,
            )
        else:
            record_sync_success(
                database_path=database_path,
                shared_tournament_id=target.shared_tournament_id,
                source=snapshot.source,
                completed_at=now_utc,
            )
        return result

    def record_failure(
        self,
        *,
        database_path: Path,
        target: ChampionsLeagueSyncTarget,
        source: str,
        external_event_id: str,
        error_message: str,
        now_utc: datetime,
    ) -> None:
        from app.champions_league_bracket import record_sync_failure

        _ = external_event_id
        record_sync_failure(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            source=source,
            attempted_at=now_utc,
            error=error_message,
        )


def _require_target_source_enabled(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    source: str,
    external_event_id: str,
) -> object:
    from app.champions_league_bracket import get_external_source

    source_config = get_external_source(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
        source=source,
    )
    if (
        source_config is None
        or not source_config.sync_enabled
        or source_config.external_event_id != external_event_id
        or source_config.enabled_at != target.enabled_at
        or source_config.sync_generation != target.sync_generation
    ):
        raise ChampionsLeagueSyncUnavailableError(
            "Настройка синхронизации изменилась; snapshot не применён."
        )
    return source_config


@dataclass(frozen=True, slots=True)
class _ExternalEntity:
    round_key: str
    matches: tuple[ExternalKnockoutMatch, ...]
    external_tie_id: str | None

    @property
    def is_complete(self) -> bool:
        return self.round_key == "final" or len(self.matches) == 2


def apply_football_data_snapshot(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    snapshot: FootballDataKnockoutSnapshot,
    now_utc: datetime,
) -> TournamentSyncResult:
    """Safely apply one parsed provider snapshot to one explicitly enabled target."""
    from app.champions_league_bracket import (
        ensure_champions_league_bracket,
        get_champions_league_bracket,
        list_fixture_imports,
    )

    _require_supported_season(snapshot.season_start_year)
    if snapshot.external_event_id != (
        f"CL:{CHAMPIONS_LEAGUE_TEMPLATE_SEASON_START_YEAR}"
    ):
        raise ChampionsLeagueSyncUnavailableError(
            "Snapshot относится не к сезону шаблона Лиги чемпионов 2026/27."
        )
    now_utc = _resolve_now(now_utc)
    _require_target_source_enabled(
        database_path=database_path,
        target=target,
        source=snapshot.source,
        external_event_id=snapshot.external_event_id,
    )

    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    team_ids, team_conflicts = _resolve_external_team_mappings(
        database_path=database_path,
        target=target,
        snapshot=snapshot,
    )
    bracket = get_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    fixture_imports = tuple(
        item
        for item in list_fixture_imports(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            source=snapshot.source,
        )
        if item.external_event_id == snapshot.external_event_id
    )
    imports_by_fixture = {item.external_fixture_id: item for item in fixture_imports}
    snapshot_fixture_ids = {
        match.external_match_id for match in _snapshot_matches(snapshot)
    }
    missing_imports = [
        item
        for item in fixture_imports
        if item.import_status != "tombstoned"
        and item.external_fixture_id not in snapshot_fixture_ids
    ]
    for missing_import in missing_imports:
        updated = _mark_imports_conflict(
            database_path=database_path,
            fixture_imports=[missing_import],
            message=(
                "Ранее импортированная фикстура отсутствует в полном snapshot "
                "источника; автоматическое удаление запрещено."
            ),
        )[0]
        imports_by_fixture[missing_import.external_fixture_id] = updated
    used_positions = {
        (item.round_key, item.bracket_position) for item in fixture_imports
    }
    used_positions.update(
        (node.round_key, node.bracket_position)
        for node in bracket.nodes
        if (
            node.resolved_first_team_id is not None
            or node.resolved_second_team_id is not None
            or node.first_leg_starts_at_utc is not None
            or node.materialized_shared_tie_id is not None
            or node.materialized_shared_match_id is not None
        )
    )
    entities = _snapshot_entities(snapshot)
    totals = TournamentSyncResult(
        conflict_count=(team_conflicts + len(missing_imports))
    )
    for entity in entities:
        position = _resolve_entity_position(
            entity=entity,
            imports_by_fixture=imports_by_fixture,
            bracket=bracket,
            team_ids=team_ids,
            used_positions=used_positions,
        )
        if position is None:
            totals = _add_result(totals, conflict_count=1)
            continue
        used_positions.add((entity.round_key, position))
        entity_result = _apply_external_entity(
            database_path=database_path,
            target=target,
            snapshot=snapshot,
            entity=entity,
            bracket_position=position,
            team_ids=team_ids,
            imports_by_fixture=imports_by_fixture,
            now_utc=_current_now(now_utc),
        )
        totals = _add_results(totals, entity_result)
        bracket = get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
        )
    return totals


def _snapshot_entities(
    snapshot: FootballDataKnockoutSnapshot,
) -> tuple[_ExternalEntity, ...]:
    from app.football_data_provider import ROUND_FINAL

    entities: list[_ExternalEntity] = []
    for tie in snapshot.two_legged_ties:
        entities.append(
            _ExternalEntity(
                round_key=tie.round_key,
                matches=(tie.first_leg, tie.second_leg),
                external_tie_id=_external_tie_id(
                    tie.round_key,
                    tie.first_leg.home_team.external_team_id,
                    tie.first_leg.away_team.external_team_id,
                ),
            )
        )
    # A single reported leg does not identify whether it is leg 1 or leg 2.
    # Wait for the complete pair instead of permanently assigning wrong ledger
    # metadata when the provider publishes the return leg first.
    if snapshot.final is not None:
        entities.append(
            _ExternalEntity(
                round_key=ROUND_FINAL,
                matches=(snapshot.final,),
                external_tie_id=None,
            )
        )
    round_order = {
        "playoff": 0,
        "round_of_16": 1,
        "quarterfinal": 2,
        "semifinal": 3,
        "final": 4,
    }
    entities.sort(
        key=lambda entity: (
            round_order[entity.round_key],
            entity.matches[0].starts_at_utc,
            entity.matches[0].external_match_id,
        )
    )
    return tuple(entities)


def _resolve_external_team_mappings(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    snapshot: FootballDataKnockoutSnapshot,
) -> tuple[dict[str, int], int]:
    from app.champions_league_bracket import (
        list_external_team_links,
        set_external_team_link,
    )
    from app.shared_tournament_service import get_shared_tournament_details

    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    local_teams_by_name = {team.name: team.id for team in details.teams}
    links = list_external_team_links(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
        source=snapshot.source,
    )
    team_ids = {link.external_team_id: link.team_id for link in links}
    external_ids_by_local_id = {link.team_id: link.external_team_id for link in links}
    conflicts = 0
    provider_teams = {
        team.external_team_id: team
        for match in _snapshot_matches(snapshot)
        for team in (match.home_team, match.away_team)
    }
    for external_team_id, provider_team in sorted(provider_teams.items()):
        if external_team_id in team_ids:
            continue
        local_team_id = local_teams_by_name.get(provider_team.name)
        if local_team_id is None:
            conflicts += 1
            continue
        existing_external_id = external_ids_by_local_id.get(local_team_id)
        if (
            existing_external_id is not None
            and existing_external_id != external_team_id
        ):
            conflicts += 1
            continue
        link = set_external_team_link(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            source=snapshot.source,
            team_id=local_team_id,
            external_team_id=external_team_id,
        )
        team_ids[link.external_team_id] = link.team_id
        external_ids_by_local_id[link.team_id] = link.external_team_id
    return team_ids, conflicts


def _snapshot_matches(
    snapshot: FootballDataKnockoutSnapshot,
) -> tuple[ExternalKnockoutMatch, ...]:
    matches: dict[str, ExternalKnockoutMatch] = {}
    for tie in snapshot.two_legged_ties:
        matches[tie.first_leg.external_match_id] = tie.first_leg
        matches[tie.second_leg.external_match_id] = tie.second_leg
    for match in snapshot.pending_matches:
        matches[match.external_match_id] = match
    if snapshot.final is not None:
        matches[snapshot.final.external_match_id] = snapshot.final
    return tuple(matches.values())


def _resolve_entity_position(
    *,
    entity: _ExternalEntity,
    imports_by_fixture: dict[str, object],
    bracket: object,
    team_ids: dict[str, int],
    used_positions: set[tuple[str, int]],
) -> int | None:
    existing = [
        imports_by_fixture[match.external_match_id]
        for match in entity.matches
        if match.external_match_id in imports_by_fixture
    ]
    existing_positions = {
        (item.round_key, item.bracket_position)  # type: ignore[attr-defined]
        for item in existing
    }
    if len(existing_positions) > 1:
        return None
    if existing_positions:
        existing_round, position = next(iter(existing_positions))
        return position if existing_round == entity.round_key else None
    if entity.round_key == "final":
        final_node = next(
            node
            for node in bracket.nodes  # type: ignore[attr-defined]
            if node.round_key == "final"
        )
        if ("final", 1) not in used_positions:
            return 1
        first_match = entity.matches[0]
        local_team_pair = {
            team_ids.get(first_match.home_team.external_team_id),
            team_ids.get(first_match.away_team.external_team_id),
        }
        if (
            None not in local_team_pair
            and {
                final_node.resolved_first_team_id,
                final_node.resolved_second_team_id,
            }
            == local_team_pair
        ):
            return 1
        return None

    first_match = entity.matches[0]
    local_team_pair = {
        team_ids.get(first_match.home_team.external_team_id),
        team_ids.get(first_match.away_team.external_team_id),
    }
    if None not in local_team_pair:
        matching_nodes = [
            node
            for node in bracket.nodes  # type: ignore[attr-defined]
            if node.round_key == entity.round_key
            and {
                node.resolved_first_team_id,
                node.resolved_second_team_id,
            }
            == local_team_pair
        ]
        if len(matching_nodes) == 1:
            return matching_nodes[0].bracket_position
        if len(matching_nodes) > 1:
            return None
    round_nodes = [
        node
        for node in bracket.nodes  # type: ignore[attr-defined]
        if node.round_key == entity.round_key
    ]
    for node in round_nodes:
        if (entity.round_key, node.bracket_position) not in used_positions:
            return node.bracket_position
    return None


def _external_tie_id(round_key: str, first_team_id: str, second_team_id: str) -> str:
    ordered = sorted((first_team_id, second_team_id), key=lambda value: int(value))
    return f"{round_key}:{ordered[0]}:{ordered[1]}"


def _apply_external_entity(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    snapshot: FootballDataKnockoutSnapshot,
    entity: _ExternalEntity,
    bracket_position: int,
    team_ids: dict[str, int],
    imports_by_fixture: dict[str, object],
    now_utc: datetime,
) -> TournamentSyncResult:
    from app.champions_league_bracket import (
        configure_bracket_node,
        get_external_tie_link,
        get_champions_league_bracket,
        mark_fixture_conflict,
        record_fixture_seen,
    )

    # This is the generation boundary for one pair/final. Disabling sync does
    # not cancel an entity that passed this gate: finishing its ledger, node,
    # fixture and result mutations avoids partial two-leg state. The next
    # entity gets a fresh gate check.
    _require_target_source_enabled(
        database_path=database_path,
        target=target,
        source=snapshot.source,
        external_event_id=snapshot.external_event_id,
    )
    fixture_imports = []
    if any(
        existing is not None and existing.import_status == "tombstoned"  # type: ignore[attr-defined]
        for existing in (
            imports_by_fixture.get(match.external_match_id) for match in entity.matches
        )
    ):
        return TournamentSyncResult()
    entity_conflict = _validate_existing_fixture_metadata(
        entity=entity,
        bracket_position=bracket_position,
        imports_by_fixture=imports_by_fixture,
    )
    if entity_conflict is not None:
        for match in entity.matches:
            existing = imports_by_fixture.get(match.external_match_id)
            if existing is not None:
                mark_fixture_conflict(
                    database_path=database_path,
                    fixture_import_id=existing.id,  # type: ignore[attr-defined]
                    error=entity_conflict,
                    expected_version=existing.version,  # type: ignore[attr-defined]
                )
        return TournamentSyncResult(conflict_count=1)

    for index, match in enumerate(entity.matches, start=1):
        leg_number = None if entity.round_key == "final" else index
        fixture_import = record_fixture_seen(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            source=snapshot.source,
            external_event_id=snapshot.external_event_id,
            external_fixture_id=match.external_match_id,
            external_tie_id=entity.external_tie_id,
            round_key=entity.round_key,  # type: ignore[arg-type]
            bracket_position=bracket_position,
            leg_number=leg_number,
            payload=_serialize_fixture(match),
            provider_updated_at=match.last_updated_at_utc,
        )
        fixture_imports.append(fixture_import)
        imports_by_fixture[match.external_match_id] = fixture_import
    if any(item.import_status == "tombstoned" for item in fixture_imports):
        return TournamentSyncResult()
    if entity.external_tie_id is not None:
        external_tie_link = get_external_tie_link(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            source=snapshot.source,
            external_event_id=snapshot.external_event_id,
            external_tie_id=entity.external_tie_id,
        )
        if (
            external_tie_link is not None
            and external_tie_link.tombstoned_at is not None
        ):
            _mark_imports_conflict(
                database_path=database_path,
                fixture_imports=fixture_imports,
                message=("Удалённое внешнее противостояние не будет создано повторно."),
            )
            return TournamentSyncResult(conflict_count=1)

    first_match = entity.matches[0]
    first_team_id = team_ids.get(first_match.home_team.external_team_id)
    second_team_id = team_ids.get(first_match.away_team.external_team_id)
    if first_team_id is None or second_team_id is None:
        _mark_imports_conflict(
            database_path=database_path,
            fixture_imports=fixture_imports,
            message=_missing_team_mapping_error(entity=entity, team_ids=team_ids),
        )
        return TournamentSyncResult(conflict_count=1)

    bracket = get_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    node = next(
        item
        for item in bracket.nodes
        if item.round_key == entity.round_key
        and item.bracket_position == bracket_position
    )
    sources = _resolve_source_node_ids(
        database_path=database_path,
        target=target,
        bracket=bracket,
        node=node,
        first_team_id=first_team_id,
        second_team_id=second_team_id,
    )
    first_start = first_match.starts_at_utc
    second_start = entity.matches[1].starts_at_utc if len(entity.matches) == 2 else None
    is_materialized = (
        node.materialized_shared_tie_id is not None
        or node.materialized_shared_match_id is not None
    )
    if not is_materialized:
        reconciliation = configure_bracket_node(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            round_key=entity.round_key,  # type: ignore[arg-type]
            bracket_position=bracket_position,
            first_source_node_id=sources[0],
            second_source_node_id=sources[1],
            resolved_first_team_id=first_team_id,
            resolved_second_team_id=second_team_id,
            first_leg_starts_at_utc=first_start,
            second_leg_starts_at_utc=second_start,
            expected_version=node.version,
            resolve_exact_provider_conflict=True,
        )
        node = reconciliation.node
        if reconciliation.action == "conflict":
            _mark_imports_conflict(
                database_path=database_path,
                fixture_imports=fixture_imports,
                message=node.sync_error or "Конфликт узла автоматической сетки.",
            )
            return TournamentSyncResult(conflict_count=1)
    elif {
        node.resolved_first_team_id,
        node.resolved_second_team_id,
    } != {first_team_id, second_team_id}:
        _mark_imports_conflict(
            database_path=database_path,
            fixture_imports=fixture_imports,
            message="Источник изменил команды уже материализованного узла.",
        )
        return TournamentSyncResult(conflict_count=1)
    elif (
        node.first_source_node_id != sources[0]
        or node.second_source_node_id != sources[1]
    ):
        _mark_imports_conflict(
            database_path=database_path,
            fixture_imports=fixture_imports,
            message=("Источник изменил связи уже материализованного узла сетки."),
        )
        return TournamentSyncResult(conflict_count=1)

    if not entity.is_complete:
        return TournamentSyncResult()
    if not is_materialized and not _source_links_ready(
        entity=entity,
        bracket=bracket,
        sources=sources,
        first_team_id=first_team_id,
        second_team_id=second_team_id,
    ):
        return TournamentSyncResult()
    if not is_materialized and not _can_create_entity(
        entity=entity,
        enabled_at=target.enabled_at,
        now_utc=now_utc,
    ):
        _mark_imports_conflict(
            database_path=database_path,
            fixture_imports=fixture_imports,
            message=(
                "Фикстура уже началась либо не имеет подтверждённого будущего "
                "статуса; ретроактивное создание запрещено."
            ),
        )
        return TournamentSyncResult(conflict_count=1)

    materialized, was_created = _materialize_entity(
        database_path=database_path,
        target=target,
        entity=entity,
        bracket_position=bracket_position,
        first_team_id=first_team_id,
        second_team_id=second_team_id,
        node=node,
        now_utc=now_utc,
        claim_token=_claim_external_tie_for_materialization(
            database_path=database_path,
            target=target,
            snapshot=snapshot,
            entity=entity,
            bracket_position=bracket_position,
            node=node,
            now_utc=now_utc,
        ),
    )
    result = TournamentSyncResult(
        created_tie_count=int(was_created and entity.round_key != "final"),
        created_match_count=int(was_created and entity.round_key == "final"),
    )
    sync_result = _sync_materialized_entity(
        database_path=database_path,
        target=target,
        entity=entity,
        bracket_position=bracket_position,
        materialized=materialized,
        fixture_imports=fixture_imports,
        team_ids=team_ids,
        now_utc=now_utc,
    )
    return _add_results(result, sync_result)


def _validate_existing_fixture_metadata(
    *,
    entity: _ExternalEntity,
    bracket_position: int,
    imports_by_fixture: dict[str, object],
) -> str | None:
    for index, match in enumerate(entity.matches, start=1):
        existing = imports_by_fixture.get(match.external_match_id)
        if existing is None:
            continue
        expected_leg = None if entity.round_key == "final" else index
        if (
            existing.round_key != entity.round_key  # type: ignore[attr-defined]
            or existing.bracket_position != bracket_position  # type: ignore[attr-defined]
            or existing.leg_number != expected_leg  # type: ignore[attr-defined]
            or existing.external_tie_id != entity.external_tie_id  # type: ignore[attr-defined]
        ):
            return "Источник изменил принадлежность ранее увиденной фикстуры."
    return None


def _serialize_fixture(match: ExternalKnockoutMatch) -> str:
    return json.dumps(
        asdict(match),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _missing_team_mapping_error(
    *, entity: _ExternalEntity, team_ids: dict[str, int]
) -> str:
    missing_teams = {
        team.external_team_id: team.name
        for match in entity.matches
        for team in (match.home_team, match.away_team)
        if team.external_team_id not in team_ids
    }
    team_diagnostics = ", ".join(
        f"name={_sanitize_diagnostic(name, limit=120)}, "
        f"external_team_id={_sanitize_diagnostic(external_team_id, limit=64)}"
        for external_team_id, name in sorted(missing_teams.items())
    )
    fixture_diagnostics = ", ".join(
        _sanitize_diagnostic(match.external_match_id, limit=64)
        for match in entity.matches
    )
    return (
        "Не найдена точная привязка внешней команды к составу общего турнира: "
        f"round={entity.round_key}; fixtures={fixture_diagnostics}; "
        f"teams=[{team_diagnostics}]."
    )


def _sanitize_diagnostic(value: str, *, limit: int) -> str:
    normalized = " ".join(value.split())[:limit]
    return json.dumps(escape(normalized, quote=True), ensure_ascii=False)


def _format_provider_conflicts(
    conflicts: tuple[ProviderConflict, ...],
) -> str | None:
    if not conflicts:
        return None
    limit = 5
    entries = []
    for conflict in conflicts[:limit]:
        fixture = (
            _sanitize_diagnostic(conflict.external_match_id, limit=48)
            if conflict.external_match_id is not None
            else "null"
        )
        entries.append(
            "code="
            f"{_sanitize_diagnostic(conflict.code, limit=48)}, "
            f"fixture={fixture}, "
            f"message={_sanitize_diagnostic(conflict.message, limit=160)}"
        )
    omitted = len(conflicts) - len(entries)
    suffix = f"; ещё конфликтов: {omitted}" if omitted else ""
    return f"Конфликты snapshot: {'; '.join(entries)}{suffix}."


def _mark_imports_conflict(
    *, database_path: Path, fixture_imports: list[object], message: str
) -> list[object]:
    from app.champions_league_bracket import mark_fixture_conflict

    updated_imports: list[object] = []
    for fixture_import in fixture_imports:
        updated_imports.append(
            mark_fixture_conflict(
                database_path=database_path,
                fixture_import_id=fixture_import.id,  # type: ignore[attr-defined]
                error=message,
                expected_version=fixture_import.version,  # type: ignore[attr-defined]
            )
        )
    return updated_imports


def _resolve_source_node_ids(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    bracket: object,
    node: object,
    first_team_id: int,
    second_team_id: int,
) -> tuple[int | None, int | None]:
    from app.shared_tournament_service import get_shared_tournament_details

    previous_round = {
        "round_of_16": "playoff",
        "quarterfinal": "round_of_16",
        "semifinal": "quarterfinal",
        "final": "semifinal",
    }.get(node.round_key)  # type: ignore[attr-defined]
    if previous_round is None:
        return (
            node.first_source_node_id,  # type: ignore[attr-defined]
            node.second_source_node_id,  # type: ignore[attr-defined]
        )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    winners_by_node_id: dict[int, int] = {}
    for candidate in bracket.nodes:  # type: ignore[attr-defined]
        if candidate.round_key != previous_round:
            continue
        winner = None
        if candidate.materialized_shared_tie_id is not None:
            tie = next(
                (
                    item
                    for item in details.two_legged_ties
                    if item.id == candidate.materialized_shared_tie_id
                ),
                None,
            )
            winner = tie.advancing_team_id if tie is not None else None
        elif candidate.materialized_shared_match_id is not None:
            match = next(
                (
                    item
                    for item in details.matches
                    if item.id == candidate.materialized_shared_match_id
                ),
                None,
            )
            winner = match.advancing_team_id if match is not None else None
        if winner is not None:
            winners_by_node_id[candidate.id] = winner

    def source_for(team_id: int, current_source_id: int | None) -> int | None:
        if current_source_id is not None:
            return current_source_id
        candidates = [
            node_id
            for node_id, winner_id in winners_by_node_id.items()
            if winner_id == team_id
        ]
        return candidates[0] if len(candidates) == 1 else None

    return (
        source_for(
            first_team_id,
            node.first_source_node_id,  # type: ignore[attr-defined]
        ),
        source_for(
            second_team_id,
            node.second_source_node_id,  # type: ignore[attr-defined]
        ),
    )


def _source_links_ready(
    *,
    entity: _ExternalEntity,
    bracket: object,
    sources: tuple[int | None, int | None],
    first_team_id: int,
    second_team_id: int,
) -> bool:
    if entity.round_key == "playoff":
        return True
    if entity.round_key != "round_of_16":
        return sources[0] is not None and sources[1] is not None
    playoff_team_ids = {
        team_id
        for node in bracket.nodes  # type: ignore[attr-defined]
        if node.round_key == "playoff"
        for team_id in (
            node.resolved_first_team_id,
            node.resolved_second_team_id,
        )
        if team_id is not None
    }
    return all(
        source is not None or team_id not in playoff_team_ids
        for source, team_id in zip(
            sources,
            (first_team_id, second_team_id),
            strict=True,
        )
    )


def _can_create_entity(
    *,
    entity: _ExternalEntity,
    enabled_at: str | None,
    now_utc: datetime,
) -> bool:
    cutoff = now_utc
    if enabled_at is not None:
        cutoff = max(cutoff, _parse_timestamp(enabled_at))
    return all(
        match.status == "TIMED" and _parse_timestamp(match.starts_at_utc) > cutoff
        for match in entity.matches
    )


def _claim_external_tie_for_materialization(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    snapshot: FootballDataKnockoutSnapshot,
    entity: _ExternalEntity,
    bracket_position: int,
    node: object,
    now_utc: datetime,
) -> str | None:
    if entity.external_tie_id is None:
        return None
    from app.champions_league_bracket import (
        ChampionsLeagueBracketConflictError,
        claim_external_tie_for_materialization,
    )

    claim_token = f"ucl-sync-{uuid4().hex}"
    claimed = claim_external_tie_for_materialization(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
        source=snapshot.source,
        external_event_id=snapshot.external_event_id,
        external_tie_id=entity.external_tie_id,
        round_key=entity.round_key,
        bracket_position=bracket_position,
        claim_token=claim_token,
        now_utc=now_utc,
    )
    if claimed.tombstoned_at is not None:
        raise ChampionsLeagueBracketConflictError(
            "Удалённое внешнее противостояние не будет создано повторно."
        )
    expected_shared_tie_id = node.materialized_shared_tie_id  # type: ignore[attr-defined]
    if (
        claimed.shared_tie_id is not None
        and claimed.shared_tie_id != expected_shared_tie_id
    ):
        raise ChampionsLeagueBracketConflictError(
            "Внешнее противостояние уже связано с другой позицией сетки."
        )
    if claimed.shared_tie_id is None and claimed.materialization_claim != claim_token:
        raise ChampionsLeagueBracketConflictError(
            "Не удалось получить claim внешнего противостояния."
        )
    return claim_token


def _materialize_entity(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    entity: _ExternalEntity,
    bracket_position: int,
    first_team_id: int,
    second_team_id: int,
    node: object,
    now_utc: datetime,
    claim_token: str | None,
) -> tuple[object, bool]:
    from app.champions_league_bracket import (
        get_champions_league_bracket,
        mark_bracket_node_materialized,
        record_external_tie_link,
    )
    from app.shared_tournament_service import (
        create_shared_match,
        create_shared_two_legged_tie,
        get_shared_tournament_details,
    )

    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    if entity.round_key == "final":
        materialized = next(
            (
                match
                for match in details.matches
                if match.shared_tie_id is None
                and match.round_key == "final"
                and match.bracket_position == bracket_position
            ),
            None,
        )
        expected_materialized_id = node.materialized_shared_match_id  # type: ignore[attr-defined]
    else:
        materialized = next(
            (
                tie
                for tie in details.two_legged_ties
                if tie.round_key == entity.round_key
                and tie.bracket_position == bracket_position
            ),
            None,
        )
        expected_materialized_id = node.materialized_shared_tie_id  # type: ignore[attr-defined]

    if expected_materialized_id is not None:
        if materialized is None or materialized.id != expected_materialized_id:
            raise RuntimeError("Материализованный узел сетки повреждён.")
    was_created = False
    if expected_materialized_id is None and materialized is None:
        if entity.round_key == "final":
            materialized = create_shared_match(
                database_path=database_path,
                shared_tournament_id=target.shared_tournament_id,
                home_team_id=first_team_id,
                away_team_id=second_team_id,
                starts_at_utc=entity.matches[0].starts_at_utc,
                best_of=None,
                actor_telegram_user_id=target.actor_telegram_user_id,
                now_utc=now_utc,
                round_key="final",
                bracket_position=bracket_position,
            )
        else:
            materialized = create_shared_two_legged_tie(
                database_path=database_path,
                shared_tournament_id=target.shared_tournament_id,
                first_team_id=first_team_id,
                second_team_id=second_team_id,
                first_leg_starts_at_utc=entity.matches[0].starts_at_utc,
                second_leg_starts_at_utc=entity.matches[1].starts_at_utc,
                actor_telegram_user_id=target.actor_telegram_user_id,
                now_utc=now_utc,
                round_key=entity.round_key,
                bracket_position=bracket_position,
            )
        was_created = True
    _require_materialized_entity_matches(
        entity=entity,
        materialized=materialized,
        first_team_id=first_team_id,
        second_team_id=second_team_id,
    )

    bracket = get_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    current_node = next(item for item in bracket.nodes if item.id == node.id)  # type: ignore[attr-defined]
    if current_node.materialized_shared_tie_id is None and (
        current_node.materialized_shared_match_id is None
    ):
        current_node = mark_bracket_node_materialized(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            node_id=current_node.id,
            expected_version=current_node.version,
            shared_tie_id=(materialized.id if entity.round_key != "final" else None),
            shared_match_id=(materialized.id if entity.round_key == "final" else None),
        )
    if entity.external_tie_id is not None:
        if claim_token is None:
            raise RuntimeError("Для внешнего противостояния отсутствует claim.")
        record_external_tie_link(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            source=FOOTBALL_DATA_SOURCE,
            external_event_id=entity.matches[0].external_event_id,
            external_tie_id=entity.external_tie_id,
            shared_tie_id=materialized.id,
            claim_token=claim_token,
        )
    return materialized, was_created


def _require_materialized_entity_matches(
    *,
    entity: _ExternalEntity,
    materialized: object,
    first_team_id: int,
    second_team_id: int,
) -> None:
    if entity.round_key == "final":
        actual_teams = {
            materialized.home_team.id,  # type: ignore[attr-defined]
            materialized.away_team.id,  # type: ignore[attr-defined]
        }
    else:
        actual_teams = {
            materialized.first_team.id,  # type: ignore[attr-defined]
            materialized.second_team.id,  # type: ignore[attr-defined]
        }
    if actual_teams != {first_team_id, second_team_id}:
        raise RuntimeError(
            "Команды существующей позиции сетки не совпадают с источником."
        )


def _sync_materialized_entity(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    entity: _ExternalEntity,
    bracket_position: int,
    materialized: object,
    fixture_imports: list[object],
    team_ids: dict[str, int],
    now_utc: datetime,
) -> TournamentSyncResult:
    from app.champions_league_bracket import (
        get_champions_league_bracket,
        mark_fixture_imported,
        reconcile_downstream_bracket_nodes,
    )
    from app.shared_tournament_service import get_shared_tournament_details

    updated_starts = 0
    saved_results = 0
    conflicts = 0
    bracket = get_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    bracket_node = next(
        item
        for item in bracket.nodes
        if item.round_key == entity.round_key
        and item.bracket_position == bracket_position
    )
    if entity.round_key == "final":
        match_arguments = {
            "database_path": database_path,
            "target": target,
            "external_match": entity.matches[0],
            "shared_match": materialized,
            "team_ids": team_ids,
            "now_utc": now_utc,
            "is_two_legged": False,
            "allow_schedule_update": (
                fixture_imports[0].shared_match_id == materialized.id  # type: ignore[attr-defined]
                and fixture_imports[0].import_status == "pending"  # type: ignore[attr-defined]
                and materialized.starts_at_utc  # type: ignore[attr-defined]
                == bracket_node.first_leg_starts_at_utc
            ),
        }
        preflight = _sync_one_match(
            **match_arguments,
            apply_changes=False,
        )
        match_result = (
            preflight
            if preflight[2]
            else _sync_one_match(
                **match_arguments,
                apply_changes=True,
            )
        )
        updated_starts += match_result[0]
        saved_results += match_result[1]
        conflicts += match_result[2]
        details = get_shared_tournament_details(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
        )
        materialized = next(
            match
            for match in details.matches
            if match.id == materialized.id  # type: ignore[attr-defined]
        )
        if conflicts == 0:
            fixture_imports[0] = mark_fixture_imported(
                database_path=database_path,
                fixture_import_id=fixture_imports[0].id,  # type: ignore[attr-defined]
                shared_match_id=materialized.id,
                expected_version=fixture_imports[0].version,  # type: ignore[attr-defined]
            )
            if materialized.status == "finished":
                conflicts += _save_champion_from_final_if_enabled(
                    database_path=database_path,
                    target=target,
                    final_match=materialized,
                    now_utc=now_utc,
                )
    else:
        operations = list(
            enumerate(
                zip(
                    entity.matches,
                    (materialized.first_leg, materialized.second_leg),  # type: ignore[attr-defined]
                    fixture_imports,
                    strict=True,
                )
            )
        )
        match_arguments = []
        for index, (external_match, shared_match, fixture_import) in operations:
            match_arguments.append(
                {
                    "database_path": database_path,
                    "target": target,
                    "external_match": external_match,
                    "shared_match": shared_match,
                    "team_ids": team_ids,
                    "now_utc": now_utc,
                    "is_two_legged": True,
                    "allow_schedule_update": (
                        fixture_import.shared_match_id == shared_match.id  # type: ignore[attr-defined]
                        and fixture_import.import_status == "pending"  # type: ignore[attr-defined]
                        and shared_match.starts_at_utc  # type: ignore[attr-defined]
                        == (
                            bracket_node.first_leg_starts_at_utc
                            if index == 0
                            else bracket_node.second_leg_starts_at_utc
                        )
                    ),
                }
            )
        preflight_results = [
            _sync_one_match(**arguments, apply_changes=False)
            for arguments in match_arguments
        ]
        if any(item[2] for item in preflight_results):
            conflicts += 1
        else:
            if (
                _parse_timestamp(entity.matches[0].starts_at_utc)
                >= _parse_timestamp(materialized.second_leg.starts_at_utc)  # type: ignore[attr-defined]
            ):
                operations.reverse()
            for index, _operation in operations:
                match_result = _sync_one_match(
                    **match_arguments[index],
                    apply_changes=True,
                )
                updated_starts += match_result[0]
                saved_results += match_result[1]
                conflicts += match_result[2]
        if conflicts == 0:
            for index, (_external, shared_match, fixture_import) in enumerate(
                zip(
                    entity.matches,
                    (materialized.first_leg, materialized.second_leg),  # type: ignore[attr-defined]
                    fixture_imports,
                    strict=True,
                )
            ):
                fixture_imports[index] = mark_fixture_imported(
                    database_path=database_path,
                    fixture_import_id=fixture_import.id,  # type: ignore[attr-defined]
                    shared_match_id=shared_match.id,
                    shared_tie_id=materialized.id,  # type: ignore[attr-defined]
                    expected_version=fixture_import.version,  # type: ignore[attr-defined]
                )
            tie_result = _sync_two_legged_tie_result(
                database_path=database_path,
                target=target,
                entity=entity,
                shared_tie_id=materialized.id,  # type: ignore[attr-defined]
                team_ids=team_ids,
                now_utc=now_utc,
            )
            saved_results += tie_result[0]
            conflicts += tie_result[1]

    if conflicts:
        _mark_imports_conflict(
            database_path=database_path,
            fixture_imports=fixture_imports,
            message="Источник конфликтует с уже сохранённой фикстурой или результатом.",
        )
    else:
        _mirror_materialized_node_dates(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            round_key=entity.round_key,
            bracket_position=bracket_position,
            shared_entity_id=materialized.id,  # type: ignore[attr-defined]
        )
        bracket = get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
        )
        node = next(
            item
            for item in bracket.nodes
            if item.round_key == entity.round_key
            and item.bracket_position == bracket_position
        )
        if (
            entity.round_key == "final" and materialized.status == "finished"  # type: ignore[attr-defined]
        ) or (
            entity.round_key != "final"
            and _shared_tie_is_resolved(
                database_path=database_path,
                shared_tournament_id=target.shared_tournament_id,
                shared_tie_id=materialized.id,  # type: ignore[attr-defined]
            )
        ):
            downstream = reconcile_downstream_bracket_nodes(
                database_path=database_path,
                shared_tournament_id=target.shared_tournament_id,
                source_node_id=node.id,
            )
            conflicts += sum(item.action == "conflict" for item in downstream)
    return TournamentSyncResult(
        updated_start_count=updated_starts,
        saved_result_count=saved_results,
        conflict_count=conflicts,
    )


def _sync_one_match(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    external_match: ExternalKnockoutMatch,
    shared_match: object,
    team_ids: dict[str, int],
    now_utc: datetime,
    is_two_legged: bool,
    allow_schedule_update: bool,
    apply_changes: bool = True,
) -> tuple[int, int, int]:
    from app.shared_tournament_service import (
        save_shared_match_result,
        update_shared_match_start,
    )

    now_utc = _current_now(now_utc)
    expected_home_team_id = team_ids.get(external_match.home_team.external_team_id)
    expected_away_team_id = team_ids.get(external_match.away_team.external_team_id)
    if expected_home_team_id is None or expected_away_team_id is None:
        return 0, 0, 1
    if (
        shared_match.home_team.id != expected_home_team_id  # type: ignore[attr-defined]
        or shared_match.away_team.id != expected_away_team_id  # type: ignore[attr-defined]
    ):
        return 0, 0, 1

    external_start = _parse_timestamp(external_match.starts_at_utc)
    local_start = _parse_timestamp(shared_match.starts_at_utc)  # type: ignore[attr-defined]
    starts_differ = external_start != local_start
    local_status = shared_match.status  # type: ignore[attr-defined]

    if external_match.status in {"SCHEDULED", "TIMED"}:
        if local_status in {"finished", "cancelled"}:
            return 0, 0, 1
        if not starts_differ:
            return 0, 0, 0
        if (
            external_match.status != "TIMED"
            or not allow_schedule_update
            or local_status != "scheduled"
            or local_start <= now_utc
            or external_start <= now_utc
        ):
            return 0, 0, 1
        if not apply_changes:
            return 1, 0, 0
        update_shared_match_start(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            shared_match_id=shared_match.id,  # type: ignore[attr-defined]
            starts_at_utc=external_match.starts_at_utc,
            expected_version=shared_match.version,  # type: ignore[attr-defined]
            actor_telegram_user_id=target.actor_telegram_user_id,
            now_utc=now_utc,
        )
        return 1, 0, 0

    if external_match.status in {
        "IN_PLAY",
        "PAUSED",
        "EXTRA_TIME",
        "PENALTY_SHOOTOUT",
    }:
        if local_status in {"finished", "cancelled"} or starts_differ:
            return 0, 0, 1
        return 0, 0, 0

    if external_match.status != "FINISHED":
        # Suspended, postponed, cancelled and awarded fixtures require a human
        # decision.  In particular, the worker never deletes a fixture.
        return 0, 0, 1
    if external_match.score is None or external_start > now_utc or starts_differ:
        return 0, 0, 1

    score = external_match.score
    advancing_team_id: int | None = None
    if not is_two_legged:
        advancing_team_id = _resolve_single_match_winner(
            external_match=external_match,
            team_ids=team_ids,
        )
        if advancing_team_id is None:
            return 0, 0, 1
    if local_status == "cancelled":
        return 0, 0, 1
    if local_status == "finished":
        current_state = (
            shared_match.home_score,  # type: ignore[attr-defined]
            shared_match.away_score,  # type: ignore[attr-defined]
            shared_match.advancing_team_id,  # type: ignore[attr-defined]
        )
        expected_state = (
            score.regular_home,
            score.regular_away,
            advancing_team_id,
        )
        return (0, 0, 0) if current_state == expected_state else (0, 0, 1)

    if not apply_changes:
        return 0, 1, 0
    save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
        shared_match_id=shared_match.id,  # type: ignore[attr-defined]
        home_score=score.regular_home,
        away_score=score.regular_away,
        advancing_team_id=advancing_team_id,
        expected_version=shared_match.version,  # type: ignore[attr-defined]
        actor_telegram_user_id=target.actor_telegram_user_id,
        actor_first_name=target.actor_first_name,
        actor_last_name=target.actor_last_name,
        actor_username=target.actor_username,
        now_utc=now_utc,
        trusted_result_source=FOOTBALL_DATA_SOURCE,
    )
    return 0, 1, 0


def _resolve_single_match_winner(
    *,
    external_match: ExternalKnockoutMatch,
    team_ids: dict[str, int],
) -> int | None:
    score = external_match.score
    if score is None or score.winner_external_team_id is None:
        return None
    winner_team_id = team_ids.get(score.winner_external_team_id)
    if winner_team_id is None:
        return None
    if score.duration == "REGULAR":
        if score.regular_home == score.regular_away:
            return None
        expected_external_winner = (
            external_match.home_team.external_team_id
            if score.regular_home > score.regular_away
            else external_match.away_team.external_team_id
        )
    elif score.duration == "EXTRA_TIME":
        if (
            score.regular_home != score.regular_away
            or score.extra_time_home is None
            or score.extra_time_away is None
            or score.extra_time_home == score.extra_time_away
            or score.penalty_home is not None
            or score.penalty_away is not None
        ):
            return None
        expected_external_winner = (
            external_match.home_team.external_team_id
            if score.extra_time_home > score.extra_time_away
            else external_match.away_team.external_team_id
        )
    elif score.duration == "PENALTY_SHOOTOUT":
        if (
            score.regular_home != score.regular_away
            or score.extra_time_home is None
            or score.extra_time_away is None
            or score.extra_time_home != score.extra_time_away
            or score.penalty_home is None
            or score.penalty_away is None
            or score.penalty_home == score.penalty_away
        ):
            return None
        expected_external_winner = (
            external_match.home_team.external_team_id
            if score.penalty_home > score.penalty_away
            else external_match.away_team.external_team_id
        )
    else:
        return None
    if score.winner_external_team_id != expected_external_winner:
        return None
    return winner_team_id


def _sync_two_legged_tie_result(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    entity: _ExternalEntity,
    shared_tie_id: int,
    team_ids: dict[str, int],
    now_utc: datetime,
) -> tuple[int, int]:
    from app.shared_tournament_service import (
        get_shared_tournament_details,
        save_shared_two_legged_tie_result,
    )

    if len(entity.matches) != 2 or any(
        match.status != "FINISHED" or match.score is None for match in entity.matches
    ):
        return 0, 0
    first_external, second_external = entity.matches
    first_score = first_external.score
    second_score = second_external.score
    if first_score is None or second_score is None:
        return 0, 1
    if first_score.duration != "REGULAR":
        return 0, 1

    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    tie = next(
        (item for item in details.two_legged_ties if item.id == shared_tie_id),
        None,
    )
    if tie is None:
        return 0, 1
    aggregate_first = tie.aggregate_first_team_score
    aggregate_second = tie.aggregate_second_team_score
    if aggregate_first is None or aggregate_second is None:
        return 0, 1

    if aggregate_first != aggregate_second:
        if (
            second_score.duration != "REGULAR"
            or second_score.extra_time_home is not None
            or second_score.extra_time_away is not None
            or second_score.penalty_home is not None
            or second_score.penalty_away is not None
        ):
            return 0, 1
        expected_winner = (
            tie.first_team.id
            if aggregate_first > aggregate_second
            else tie.second_team.id
        )
        if tie.advancing_team_id != expected_winner:
            return 0, 1
        return 0, 0

    expected_winner, expected_extra, expected_penalties = (
        _resolve_tied_aggregate_provider_result(
            external_match=second_external,
            team_ids=team_ids,
        )
    )
    if expected_winner is None or expected_extra is None:
        return 0, 1
    requested_state = (
        expected_winner,
        expected_extra[0],
        expected_extra[1],
        expected_penalties[0] if expected_penalties is not None else None,
        expected_penalties[1] if expected_penalties is not None else None,
    )
    current_state = (
        tie.advancing_team_id,
        tie.second_leg_extra_time_home_score,
        tie.second_leg_extra_time_away_score,
        tie.second_leg_home_penalty_score,
        tie.second_leg_away_penalty_score,
    )
    if tie.advancing_team_id is not None:
        return (0, 0) if current_state == requested_state else (0, 1)

    save_shared_two_legged_tie_result(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
        shared_tie_id=shared_tie_id,
        advancing_team_id=expected_winner,
        second_leg_extra_time_home_score=expected_extra[0],
        second_leg_extra_time_away_score=expected_extra[1],
        second_leg_home_penalty_score=(
            expected_penalties[0] if expected_penalties is not None else None
        ),
        second_leg_away_penalty_score=(
            expected_penalties[1] if expected_penalties is not None else None
        ),
        expected_version=tie.version,
        actor_telegram_user_id=target.actor_telegram_user_id,
        actor_first_name=target.actor_first_name,
        actor_last_name=target.actor_last_name,
        actor_username=target.actor_username,
        now_utc=now_utc,
    )
    return 1, 0


def _resolve_tied_aggregate_provider_result(
    *,
    external_match: ExternalKnockoutMatch,
    team_ids: dict[str, int],
) -> tuple[int | None, tuple[int, int] | None, tuple[int, int] | None]:
    score = external_match.score
    if (
        score is None
        or score.duration not in {"EXTRA_TIME", "PENALTY_SHOOTOUT"}
        or score.extra_time_home is None
        or score.extra_time_away is None
    ):
        return None, None, None
    home_team_id = team_ids.get(external_match.home_team.external_team_id)
    away_team_id = team_ids.get(external_match.away_team.external_team_id)
    if home_team_id is None or away_team_id is None:
        return None, None, None
    penalties: tuple[int, int] | None = None
    if score.duration == "EXTRA_TIME":
        if (
            score.extra_time_home == score.extra_time_away
            or score.penalty_home is not None
            or score.penalty_away is not None
        ):
            return None, None, None
        winner_team_id = (
            home_team_id
            if score.extra_time_home > score.extra_time_away
            else away_team_id
        )
    else:
        if (
            score.extra_time_home != score.extra_time_away
            or score.penalty_home is None
            or score.penalty_away is None
            or score.penalty_home == score.penalty_away
        ):
            return None, None, None
        penalties = (score.penalty_home, score.penalty_away)
        winner_team_id = (
            home_team_id if score.penalty_home > score.penalty_away else away_team_id
        )
    return (
        winner_team_id,
        (score.extra_time_home, score.extra_time_away),
        penalties,
    )


def _save_champion_from_final_if_enabled(
    *,
    database_path: Path,
    target: ChampionsLeagueSyncTarget,
    final_match: object,
    now_utc: datetime,
) -> int:
    from app.shared_tournament_service import (
        SharedTournamentResultUnavailableError,
        get_shared_tournament_details,
        save_shared_champion_result,
    )

    champion_team_id = final_match.advancing_team_id  # type: ignore[attr-defined]
    if champion_team_id is None:
        return 1
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=target.shared_tournament_id,
    )
    settings = details.champion_prediction
    if not settings.is_enabled:
        return 0
    if settings.actual_champion is not None:
        return int(settings.actual_champion.id != champion_team_id)
    try:
        save_shared_champion_result(
            database_path=database_path,
            shared_tournament_id=target.shared_tournament_id,
            champion_team_id=champion_team_id,
            expected_version=details.tournament.version,
            actor_telegram_user_id=target.actor_telegram_user_id,
            actor_first_name=target.actor_first_name,
            actor_last_name=target.actor_last_name,
            actor_username=target.actor_username,
            now_utc=now_utc,
        )
    except SharedTournamentResultUnavailableError:
        return 1
    return 0


def _mirror_materialized_node_dates(
    *,
    database_path: Path,
    shared_tournament_id: int,
    round_key: str,
    bracket_position: int,
    shared_entity_id: int,
) -> None:
    from app.champions_league_bracket import (
        get_champions_league_bracket,
        sync_materialized_node_dates,
    )
    from app.shared_tournament_service import get_shared_tournament_details

    bracket = get_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
    )
    node = next(
        item
        for item in bracket.nodes
        if item.round_key == round_key and item.bracket_position == bracket_position
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
    )
    if round_key == "final":
        match = next(item for item in details.matches if item.id == shared_entity_id)
        first_start = match.starts_at_utc
        second_start = None
    else:
        tie = next(
            item for item in details.two_legged_ties if item.id == shared_entity_id
        )
        first_start = tie.first_leg.starts_at_utc
        second_start = tie.second_leg.starts_at_utc
    if (
        node.first_leg_starts_at_utc == first_start
        and node.second_leg_starts_at_utc == second_start
    ):
        return
    sync_materialized_node_dates(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        node_id=node.id,
        expected_version=node.version,
        first_leg_starts_at_utc=first_start,
        second_leg_starts_at_utc=second_start,
    )


def _shared_tie_is_resolved(
    *, database_path: Path, shared_tournament_id: int, shared_tie_id: int
) -> bool:
    from app.shared_tournament_service import get_shared_tournament_details

    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
    )
    return any(
        tie.id == shared_tie_id and tie.advancing_team_id is not None
        for tie in details.two_legged_ties
    )


async def _record_target_failures(
    *,
    backend: ChampionsLeagueSyncBackend,
    database_path: Path,
    targets: tuple[ChampionsLeagueSyncTarget, ...],
    source: str,
    external_event_id: str,
    error_message: str,
    now_utc: datetime,
) -> None:
    results = await asyncio.gather(
        *(
            asyncio.to_thread(
                backend.record_failure,
                database_path=database_path,
                target=target,
                source=source,
                external_event_id=external_event_id,
                error_message=error_message,
                now_utc=now_utc,
            )
            for target in targets
        ),
        return_exceptions=True,
    )
    for target, result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            LOGGER.error(
                "Could not record Champions League provider failure for "
                "tournament %s: %s.",
                target.shared_tournament_id,
                type(result).__name__,
            )


async def synchronize_champions_league_once(
    *,
    database_path: Path,
    client: FootballDataClient | None,
    backend: ChampionsLeagueSyncBackend,
    season_start_year: int,
    now_utc: datetime | None = None,
    target_shared_tournament_id: int | None = None,
    require_target: bool = False,
) -> ChampionsLeagueSyncCycleResult:
    _require_supported_season(season_start_year)
    if client is None:
        return _empty_cycle_result()
    now_floor = _resolve_now(now_utc) if now_utc is not None else None
    external_event_id = f"CL:{season_start_year}"
    targets = await asyncio.to_thread(
        backend.list_enabled_targets,
        database_path=database_path,
        source=FOOTBALL_DATA_SOURCE,
        external_event_id=external_event_id,
    )
    if target_shared_tournament_id is not None:
        targets = tuple(
            target
            for target in targets
            if target.shared_tournament_id == target_shared_tournament_id
        )
    if not targets:
        if require_target:
            raise ChampionsLeagueSyncUnavailableError(
                "Синхронизация источника не включена для этого общего турнира."
            )
        return _empty_cycle_result()

    try:
        snapshot = await client.fetch_champions_league_knockout()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        error_message = _safe_error_message(error)
        failure_now = _current_now(now_floor)
        await _record_target_failures(
            backend=backend,
            database_path=database_path,
            targets=targets,
            source=FOOTBALL_DATA_SOURCE,
            external_event_id=external_event_id,
            error_message=error_message,
            now_utc=failure_now,
        )
        return ChampionsLeagueSyncCycleResult(
            target_count=len(targets),
            fetched=False,
            successful_target_count=0,
            failed_target_count=len(targets),
            provider_conflict_count=0,
            created_tie_count=0,
            created_match_count=0,
            updated_start_count=0,
            saved_result_count=0,
            conflict_count=0,
            provider_error=error_message,
        )
    if (
        snapshot.source != FOOTBALL_DATA_SOURCE
        or snapshot.external_event_id != external_event_id
        or snapshot.season_start_year != season_start_year
    ):
        error_message = "Provider snapshot identity does not match sync configuration."
        failure_now = _current_now(now_floor)
        await _record_target_failures(
            backend=backend,
            database_path=database_path,
            targets=targets,
            source=FOOTBALL_DATA_SOURCE,
            external_event_id=external_event_id,
            error_message=error_message,
            now_utc=failure_now,
        )
        return ChampionsLeagueSyncCycleResult(
            target_count=len(targets),
            fetched=True,
            successful_target_count=0,
            failed_target_count=len(targets),
            provider_conflict_count=len(snapshot.conflicts),
            created_tie_count=0,
            created_match_count=0,
            updated_start_count=0,
            saved_result_count=0,
            conflict_count=0,
            provider_error=error_message,
        )

    successful_count = 0
    failed_count = 0
    totals = TournamentSyncResult()
    for target in targets:
        target_now = _current_now(now_floor)
        try:
            result = await asyncio.to_thread(
                backend.apply_snapshot,
                database_path=database_path,
                target=target,
                snapshot=snapshot,
                now_utc=target_now,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failed_count += 1
            error_message = _safe_error_message(error)
            try:
                await asyncio.to_thread(
                    backend.record_failure,
                    database_path=database_path,
                    target=target,
                    source=FOOTBALL_DATA_SOURCE,
                    external_event_id=external_event_id,
                    error_message=error_message,
                    now_utc=target_now,
                )
            except Exception:
                LOGGER.exception(
                    "Could not record Champions League sync failure for tournament %s.",
                    target.shared_tournament_id,
                )
            continue
        successful_count += 1
        totals = TournamentSyncResult(
            created_tie_count=(totals.created_tie_count + result.created_tie_count),
            created_match_count=(
                totals.created_match_count + result.created_match_count
            ),
            updated_start_count=(
                totals.updated_start_count + result.updated_start_count
            ),
            saved_result_count=(totals.saved_result_count + result.saved_result_count),
            conflict_count=totals.conflict_count + result.conflict_count,
        )
    return ChampionsLeagueSyncCycleResult(
        target_count=len(targets),
        fetched=True,
        successful_target_count=successful_count,
        failed_target_count=failed_count,
        provider_conflict_count=len(snapshot.conflicts),
        created_tie_count=totals.created_tie_count,
        created_match_count=totals.created_match_count,
        updated_start_count=totals.updated_start_count,
        saved_result_count=totals.saved_result_count,
        conflict_count=totals.conflict_count,
    )


async def synchronize_champions_league_tournament_once(
    *,
    database_path: Path,
    client: FootballDataClient | None,
    shared_tournament_id: int,
    season_start_year: int,
    now_utc: datetime | None = None,
    backend: ChampionsLeagueSyncBackend | None = None,
) -> ChampionsLeagueSyncCycleResult:
    _require_supported_season(season_start_year)
    if client is None:
        raise ChampionsLeagueSyncUnavailableError(
            "FOOTBALL_DATA_API_TOKEN не настроен; синхронизация недоступна."
        )
    return await synchronize_champions_league_once(
        database_path=database_path,
        client=client,
        backend=backend or DatabaseChampionsLeagueSyncBackend(),
        season_start_year=season_start_year,
        now_utc=now_utc,
        target_shared_tournament_id=shared_tournament_id,
        require_target=True,
    )


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


async def run_champions_league_sync_worker(
    *,
    database_path: Path,
    client: FootballDataClient | None,
    season_start_year: int,
    interval_minutes: int,
    backend: ChampionsLeagueSyncBackend | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock | None = None,
) -> None:
    _require_supported_season(season_start_year)
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive.")
    selected_backend = backend or DatabaseChampionsLeagueSyncBackend()
    selected_clock = clock or (lambda: datetime.now(timezone.utc))
    consecutive_failures = 0
    idle_was_logged = False
    while True:
        try:
            if client is None:
                if not idle_was_logged:
                    LOGGER.info(
                        "Champions League synchronization is idle: "
                        "FOOTBALL_DATA_API_TOKEN is not configured."
                    )
                    idle_was_logged = True
                result = _empty_cycle_result()
            else:
                result = await synchronize_champions_league_once(
                    database_path=database_path,
                    client=client,
                    backend=selected_backend,
                    season_start_year=season_start_year,
                    now_utc=selected_clock(),
                )
                LOGGER.info(
                    "Champions League sync completed: targets=%s successful=%s "
                    "failed=%s ties_created=%s matches_created=%s starts_updated=%s "
                    "results_saved=%s conflicts=%s provider_conflicts=%s.",
                    result.target_count,
                    result.successful_target_count,
                    result.failed_target_count,
                    result.created_tie_count,
                    result.created_match_count,
                    result.updated_start_count,
                    result.saved_result_count,
                    result.conflict_count,
                    result.provider_conflict_count,
                )
            consecutive_failures = (
                consecutive_failures + 1 if result.retryable_failure else 0
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            LOGGER.exception("Champions League synchronization cycle failed.")
        multiplier = 2 ** min(consecutive_failures, 10)
        delay_minutes = min(
            interval_minutes * multiplier,
            MAX_BACKOFF_MINUTES,
        )
        await sleep(delay_minutes * 60)


def _empty_cycle_result() -> ChampionsLeagueSyncCycleResult:
    return ChampionsLeagueSyncCycleResult(
        target_count=0,
        fetched=False,
        successful_target_count=0,
        failed_target_count=0,
        provider_conflict_count=0,
        created_tie_count=0,
        created_match_count=0,
        updated_start_count=0,
        saved_result_count=0,
        conflict_count=0,
    )


def _safe_error_message(error: Exception) -> str:
    message = " ".join(str(error).split())
    if message:
        return message[:500]
    return f"{type(error).__name__} during Champions League synchronization."


def _require_supported_season(season_start_year: int) -> None:
    if season_start_year != CHAMPIONS_LEAGUE_TEMPLATE_SEASON_START_YEAR:
        raise ValueError(
            "season_start_year must be 2026 for the fixed 2026/27 template."
        )


def _resolve_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now_utc must include a timezone.")
    return value.astimezone(timezone.utc)


def _current_now(floor: datetime | None = None) -> datetime:
    current = datetime.now(timezone.utc)
    return max(current, floor) if floor is not None else current


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("Provider timestamp is not valid ISO 8601.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Provider timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _add_result(
    result: TournamentSyncResult,
    *,
    created_tie_count: int = 0,
    created_match_count: int = 0,
    updated_start_count: int = 0,
    saved_result_count: int = 0,
    conflict_count: int = 0,
) -> TournamentSyncResult:
    return TournamentSyncResult(
        created_tie_count=result.created_tie_count + created_tie_count,
        created_match_count=result.created_match_count + created_match_count,
        updated_start_count=result.updated_start_count + updated_start_count,
        saved_result_count=result.saved_result_count + saved_result_count,
        conflict_count=result.conflict_count + conflict_count,
    )


def _add_results(
    first: TournamentSyncResult, second: TournamentSyncResult
) -> TournamentSyncResult:
    return _add_result(
        first,
        created_tie_count=second.created_tie_count,
        created_match_count=second.created_match_count,
        updated_start_count=second.updated_start_count,
        saved_result_count=second.saved_result_count,
        conflict_count=second.conflict_count,
    )

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import app.champions_league_sync as champions_league_sync
from app.champions_league_sync import (
    ChampionsLeagueSyncUnavailableError,
    ChampionsLeagueSyncTarget,
    TournamentSyncResult,
    run_champions_league_sync_worker,
    synchronize_champions_league_once,
    synchronize_champions_league_tournament_once,
)
from app.champions_league_bracket import (
    claim_external_tie_for_materialization,
    configure_bracket_node,
    configure_external_source,
    ensure_champions_league_bracket,
    get_champions_league_bracket,
    get_external_source,
    get_external_tie_link,
    list_fixture_imports,
    mark_bracket_node_materialized,
)
from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import create_champions_league_2026_27_contest
from app.database import create_connection, initialize_database
from app.football_data_provider import (
    FOOTBALL_DATA_SOURCE,
    ExternalKnockoutMatch,
    ExternalMatchScore,
    ExternalTeam,
    FootballDataKnockoutSnapshot,
    ExternalTwoLeggedTie,
    ProviderConflict,
)
from app.shared_tournament_service import (
    create_shared_tournament,
    create_shared_two_legged_tie,
    delete_shared_two_legged_tie,
    get_shared_tournament_details,
    save_shared_champion_settings,
    save_shared_match_result,
    save_shared_tournament_teams,
    save_shared_two_legged_tie_result,
    update_shared_match_start,
)


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
OWNER_ID = 4242


def _snapshot() -> FootballDataKnockoutSnapshot:
    return FootballDataKnockoutSnapshot(
        source=FOOTBALL_DATA_SOURCE,
        external_event_id="CL:2026",
        season_start_year=2026,
        two_legged_ties=(),
        final=None,
        pending_matches=(),
        conflicts=(),
        ignored_match_count=0,
    )


def _external_match(
    match_id: int,
    *,
    home_id: int,
    home_name: str,
    away_id: int,
    away_name: str,
    starts_at: str,
    round_key: str = "playoff",
    provider_stage: str = "PLAYOFFS",
    status: str = "TIMED",
    score: ExternalMatchScore | None = None,
    last_updated: str = "2026-09-02T12:00:00Z",
) -> ExternalKnockoutMatch:
    return ExternalKnockoutMatch(
        external_match_id=str(match_id),
        external_event_id="CL:2026",
        round_key=round_key,
        provider_stage=provider_stage,
        home_team=ExternalTeam(str(home_id), home_name),
        away_team=ExternalTeam(str(away_id), away_name),
        starts_at_utc=starts_at,
        status=status,
        score=score,
        last_updated_at_utc=last_updated,
    )


def _playoff_snapshot(*, finished: bool = False) -> FootballDataKnockoutSnapshot:
    first_score = None
    second_score = None
    status = "TIMED"
    last_updated = "2026-09-02T12:00:00Z"
    if finished:
        status = "FINISHED"
        last_updated = "2027-02-18T00:00:00Z"
        first_score = ExternalMatchScore(
            regular_home=2,
            regular_away=1,
            extra_time_home=None,
            extra_time_away=None,
            penalty_home=None,
            penalty_away=None,
            winner_external_team_id="10",
            duration="REGULAR",
        )
        second_score = ExternalMatchScore(
            regular_home=1,
            regular_away=0,
            extra_time_home=0,
            extra_time_away=0,
            penalty_home=5,
            penalty_away=4,
            winner_external_team_id="20",
            duration="PENALTY_SHOOTOUT",
        )
    first = _external_match(
        101,
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-02-10T20:00:00Z",
        status=status,
        score=first_score,
        last_updated=last_updated,
    )
    second = _external_match(
        102,
        home_id=20,
        home_name="Beta FC",
        away_id=10,
        away_name="Alpha FC",
        starts_at="2027-02-17T20:00:00Z",
        status=status,
        score=second_score,
        last_updated=last_updated,
    )
    return FootballDataKnockoutSnapshot(
        source=FOOTBALL_DATA_SOURCE,
        external_event_id="CL:2026",
        season_start_year=2026,
        two_legged_ties=(ExternalTwoLeggedTie("playoff", first, second),),
        final=None,
        pending_matches=(),
        conflicts=(),
        ignored_match_count=0,
    )


def _create_enabled_tournament(database_path: Path) -> int:
    initialize_database(database_path)
    shared = create_shared_tournament(
        database_path=database_path,
        name="Лига чемпионов 2026/27",
        template_key="champions_league_2026_27",
        actor_telegram_user_id=OWNER_ID,
    )
    shared = save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        team_names=["Alpha FC", "Beta FC", "Gamma FC", "Delta FC"],
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )
    configure_external_source(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=FOOTBALL_DATA_SOURCE,
        external_event_id="CL:2026",
        sync_enabled=True,
        expected_tournament_version=shared.tournament.version,
        now_utc=NOW,
    )
    return shared.tournament.id


def _attach_contest(database_path: Path, *, tournament_id: int) -> int:
    return create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=-1004242,
        chat_title="Чат ЛЧ",
        telegram_user_id=OWNER_ID,
        first_name="Owner",
        last_name=None,
        username="owner",
        contest_name="Прогнозы ЛЧ",
        idempotency_key="ucl-sync-contest",
        audit_actor=AuditActor(
            telegram_chat_id=-1004242,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        shared_tournament_id=tournament_id,
    ).contest.id


def _materialize_finished_semifinal(
    database_path: Path,
    *,
    tournament_id: int,
    bracket_position: int,
    first_team_id: int,
    second_team_id: int,
    winner_team_id: int,
) -> None:
    bracket = get_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    node = next(
        item
        for item in bracket.nodes
        if item.round_key == "semifinal" and item.bracket_position == bracket_position
    )
    node = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        round_key="semifinal",
        bracket_position=bracket_position,
        first_source_node_id=None,
        second_source_node_id=None,
        resolved_first_team_id=first_team_id,
        resolved_second_team_id=second_team_id,
        first_leg_starts_at_utc="2027-05-01T20:00:00Z",
        second_leg_starts_at_utc="2027-05-08T20:00:00Z",
        expected_version=node.version,
    ).node
    tie = create_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        first_team_id=first_team_id,
        second_team_id=second_team_id,
        first_leg_starts_at_utc="2027-05-01T20:00:00Z",
        second_leg_starts_at_utc="2027-05-08T20:00:00Z",
        actor_telegram_user_id=OWNER_ID,
        now_utc=NOW,
        round_key="semifinal",
        bracket_position=bracket_position,
    )
    mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        node_id=node.id,
        expected_version=node.version,
        shared_tie_id=tie.id,
    )
    result_time = datetime(2027, 5, 9, 12, 0, tzinfo=timezone.utc)
    save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        shared_match_id=tie.first_leg.id,
        home_score=1,
        away_score=0,
        advancing_team_id=None,
        expected_version=tie.first_leg.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username="owner",
        now_utc=result_time,
    )
    save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        shared_match_id=tie.second_leg.id,
        home_score=0,
        away_score=0,
        advancing_team_id=None,
        expected_version=tie.second_leg.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username="owner",
        now_utc=result_time,
    )
    current_tie = next(
        item
        for item in get_shared_tournament_details(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).two_legged_ties
        if item.id == tie.id
    )
    save_shared_two_legged_tie_result(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        shared_tie_id=tie.id,
        advancing_team_id=winner_team_id,
        second_leg_extra_time_home_score=None,
        second_leg_extra_time_away_score=None,
        second_leg_home_penalty_score=None,
        second_leg_away_penalty_score=None,
        expected_version=current_tie.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username="owner",
        now_utc=result_time,
    )


def _snapshot_with_round_of_16() -> FootballDataKnockoutSnapshot:
    playoff = _playoff_snapshot(finished=True).two_legged_ties[0]
    first = _external_match(
        201,
        home_id=20,
        home_name="Beta FC",
        away_id=30,
        away_name="Gamma FC",
        starts_at="2027-03-10T20:00:00Z",
        round_key="round_of_16",
        provider_stage="LAST_16",
        last_updated="2027-02-18T00:00:00Z",
    )
    second = _external_match(
        202,
        home_id=30,
        home_name="Gamma FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-03-17T20:00:00Z",
        round_key="round_of_16",
        provider_stage="LAST_16",
        last_updated="2027-02-18T00:00:00Z",
    )
    return FootballDataKnockoutSnapshot(
        source=FOOTBALL_DATA_SOURCE,
        external_event_id="CL:2026",
        season_start_year=2026,
        two_legged_ties=(
            playoff,
            ExternalTwoLeggedTie("round_of_16", first, second),
        ),
        final=None,
        pending_matches=(),
        conflicts=(),
        ignored_match_count=0,
    )


class FakeClient:
    def __init__(self, result: object) -> None:
        self.result = result
        self.call_count = 0

    async def fetch_champions_league_knockout(self):
        self.call_count += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeBackend:
    def __init__(self, target_ids: tuple[int, ...]) -> None:
        self.targets = tuple(ChampionsLeagueSyncTarget(item) for item in target_ids)
        self.list_calls: list[tuple[str, str]] = []
        self.apply_calls: list[int] = []
        self.failure_calls: list[tuple[int, str]] = []
        self.fail_target_id: int | None = None

    def list_enabled_targets(
        self,
        *,
        database_path: Path,
        source: str,
        external_event_id: str,
    ):
        _ = database_path
        self.list_calls.append((source, external_event_id))
        return self.targets

    def apply_snapshot(
        self,
        *,
        database_path: Path,
        target: ChampionsLeagueSyncTarget,
        snapshot: FootballDataKnockoutSnapshot,
        now_utc: datetime,
    ) -> TournamentSyncResult:
        _ = (database_path, snapshot, now_utc)
        self.apply_calls.append(target.shared_tournament_id)
        if target.shared_tournament_id == self.fail_target_id:
            raise RuntimeError("synthetic target failure")
        return TournamentSyncResult(
            created_tie_count=1,
            created_match_count=2,
            updated_start_count=3,
            saved_result_count=4,
            conflict_count=5,
        )

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
        _ = (database_path, source, external_event_id, now_utc)
        self.failure_calls.append((target.shared_tournament_id, error_message))


def test_cycle_does_not_fetch_without_enabled_targets(tmp_path: Path) -> None:
    client = FakeClient(_snapshot())
    backend = FakeBackend(())

    result = asyncio.run(
        synchronize_champions_league_once(
            database_path=tmp_path / "db.sqlite3",
            client=client,  # type: ignore[arg-type]
            backend=backend,
            season_start_year=2026,
            now_utc=NOW,
        )
    )

    assert result.target_count == 0
    assert result.fetched is False
    assert client.call_count == 0
    assert backend.list_calls == [(FOOTBALL_DATA_SOURCE, "CL:2026")]


def test_cycle_rejects_cross_season_configuration_before_database_or_fetch(
    tmp_path: Path,
) -> None:
    client = FakeClient(_snapshot())
    backend = FakeBackend((10,))

    with pytest.raises(ValueError, match="fixed 2026/27 template"):
        asyncio.run(
            synchronize_champions_league_once(
                database_path=tmp_path / "db.sqlite3",
                client=client,  # type: ignore[arg-type]
                backend=backend,
                season_start_year=2027,
                now_utc=NOW,
            )
        )

    assert client.call_count == 0
    assert backend.list_calls == []


def test_provider_failure_is_recorded_for_every_target(tmp_path: Path) -> None:
    client = FakeClient(RuntimeError("provider unavailable"))
    backend = FakeBackend((10, 20))

    result = asyncio.run(
        synchronize_champions_league_once(
            database_path=tmp_path / "db.sqlite3",
            client=client,  # type: ignore[arg-type]
            backend=backend,
            season_start_year=2026,
            now_utc=NOW,
        )
    )

    assert result.failed is True
    assert result.failed_target_count == 2
    assert result.provider_error == "provider unavailable"
    assert backend.apply_calls == []
    assert backend.failure_calls == [
        (10, "provider unavailable"),
        (20, "provider unavailable"),
    ]


def test_cycle_isolates_target_failure_and_aggregates_success(tmp_path: Path) -> None:
    client = FakeClient(_snapshot())
    backend = FakeBackend((10, 20))
    backend.fail_target_id = 20

    result = asyncio.run(
        synchronize_champions_league_once(
            database_path=tmp_path / "db.sqlite3",
            client=client,  # type: ignore[arg-type]
            backend=backend,
            season_start_year=2026,
            now_utc=NOW,
        )
    )

    assert result.successful_target_count == 1
    assert result.failed_target_count == 1
    assert result.created_tie_count == 1
    assert result.created_match_count == 2
    assert result.updated_start_count == 3
    assert result.saved_result_count == 4
    assert result.conflict_count == 5
    assert backend.apply_calls == [10, 20]
    assert backend.failure_calls == [(20, "synthetic target failure")]


def test_one_tournament_run_never_applies_other_enabled_target(
    tmp_path: Path,
) -> None:
    client = FakeClient(_snapshot())
    backend = FakeBackend((10, 20))

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=tmp_path / "db.sqlite3",
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=20,
            season_start_year=2026,
            now_utc=NOW,
            backend=backend,
        )
    )

    assert result.target_count == 1
    assert backend.apply_calls == [20]
    assert client.call_count == 1


def test_one_tournament_run_fails_closed_before_fetch_when_disabled(
    tmp_path: Path,
) -> None:
    client = FakeClient(_snapshot())
    backend = FakeBackend((10, 20))

    with pytest.raises(ChampionsLeagueSyncUnavailableError):
        asyncio.run(
            synchronize_champions_league_tournament_once(
                database_path=tmp_path / "db.sqlite3",
                client=client,  # type: ignore[arg-type]
                shared_tournament_id=30,
                season_start_year=2026,
                now_utc=NOW,
                backend=backend,
            )
        )

    assert backend.apply_calls == []
    assert client.call_count == 0


def test_database_backend_stops_before_entity_if_source_is_disabled_during_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "ucl-disable-race.db"
    tournament_id = _create_enabled_tournament(database_path)
    original = champions_league_sync._resolve_external_team_mappings

    def disable_after_mapping(**kwargs):
        result = original(**kwargs)
        source = get_external_source(
            database_path=database_path,
            shared_tournament_id=tournament_id,
            source=FOOTBALL_DATA_SOURCE,
        )
        assert source is not None
        configure_external_source(
            database_path=database_path,
            shared_tournament_id=tournament_id,
            source=FOOTBALL_DATA_SOURCE,
            external_event_id="CL:2026",
            sync_enabled=False,
            expected_version=source.version,
            now_utc=NOW,
        )
        return result

    monkeypatch.setattr(
        champions_league_sync,
        "_resolve_external_team_mappings",
        disable_after_mapping,
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=FakeClient(_playoff_snapshot()),  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )

    assert result.successful_target_count == 0
    assert result.failed_target_count == 1
    assert (
        get_shared_tournament_details(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).two_legged_ties
        == ()
    )
    assert (
        list_fixture_imports(
            database_path=database_path,
            shared_tournament_id=tournament_id,
            source=FOOTBALL_DATA_SOURCE,
        )
        == ()
    )
    playoff_node = next(
        node
        for node in get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).nodes
        if node.round_key == "playoff" and node.bracket_position == 1
    )
    assert playoff_node.resolved_first_team_id is None
    assert playoff_node.resolved_second_team_id is None
    assert playoff_node.materialized_shared_tie_id is None


def test_disable_after_entity_gate_finishes_current_tie_and_stops_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "ucl-disable-after-gate.db"
    tournament_id = _create_enabled_tournament(database_path)
    first_tie = _playoff_snapshot().two_legged_ties[0]
    second_tie = ExternalTwoLeggedTie(
        "playoff",
        _external_match(
            103,
            home_id=30,
            home_name="Gamma FC",
            away_id=40,
            away_name="Delta FC",
            starts_at="2027-02-11T20:00:00Z",
        ),
        _external_match(
            104,
            home_id=40,
            home_name="Delta FC",
            away_id=30,
            away_name="Gamma FC",
            starts_at="2027-02-18T20:00:00Z",
        ),
    )
    snapshot = replace(
        _playoff_snapshot(),
        two_legged_ties=(first_tie, second_tie),
    )
    original = champions_league_sync._require_target_source_enabled
    gate_calls = 0

    def disable_immediately_after_first_entity_gate(**kwargs):
        nonlocal gate_calls
        gate_calls += 1
        source_config = original(**kwargs)
        if gate_calls == 2:
            configure_external_source(
                database_path=database_path,
                shared_tournament_id=tournament_id,
                source=FOOTBALL_DATA_SOURCE,
                external_event_id="CL:2026",
                sync_enabled=False,
                expected_version=source_config.version,
                now_utc=NOW,
            )
        return source_config

    monkeypatch.setattr(
        champions_league_sync,
        "_require_target_source_enabled",
        disable_immediately_after_first_entity_gate,
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=FakeClient(snapshot),  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    imports = list_fixture_imports(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
    )
    bracket = get_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    first_node = next(
        node
        for node in bracket.nodes
        if node.round_key == "playoff" and node.bracket_position == 1
    )
    second_node = next(
        node
        for node in bracket.nodes
        if node.round_key == "playoff" and node.bracket_position == 2
    )

    assert gate_calls == 3
    assert result.successful_target_count == 0
    assert result.failed_target_count == 1
    assert len(details.two_legged_ties) == 1
    assert {item.external_fixture_id for item in imports} == {"101", "102"}
    assert {item.import_status for item in imports} == {"imported"}
    assert all(item.shared_match_id is not None for item in imports)
    assert first_node.materialized_shared_tie_id == details.two_legged_ties[0].id
    assert second_node.materialized_shared_tie_id is None
    assert second_node.resolved_first_team_id is None
    assert second_node.resolved_second_team_id is None


def test_same_second_disable_reenable_invalidates_in_flight_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "ucl-same-second-generation.db"
    tournament_id = _create_enabled_tournament(database_path)
    first_tie = _playoff_snapshot().two_legged_ties[0]
    second_tie = ExternalTwoLeggedTie(
        "playoff",
        _external_match(
            103,
            home_id=30,
            home_name="Gamma FC",
            away_id=40,
            away_name="Delta FC",
            starts_at="2027-02-11T20:00:00Z",
        ),
        _external_match(
            104,
            home_id=40,
            home_name="Delta FC",
            away_id=30,
            away_name="Gamma FC",
            starts_at="2027-02-18T20:00:00Z",
        ),
    )
    snapshot = replace(
        _playoff_snapshot(),
        two_legged_ties=(first_tie, second_tie),
    )
    original = champions_league_sync._require_target_source_enabled
    gate_calls = 0

    def reenable_immediately_after_first_entity_gate(**kwargs):
        nonlocal gate_calls
        gate_calls += 1
        source_config = original(**kwargs)
        if gate_calls == 2:
            disabled = configure_external_source(
                database_path=database_path,
                shared_tournament_id=tournament_id,
                source=FOOTBALL_DATA_SOURCE,
                external_event_id="CL:2026",
                sync_enabled=False,
                expected_version=source_config.version,
                now_utc=NOW,
            )
            reenabled = configure_external_source(
                database_path=database_path,
                shared_tournament_id=tournament_id,
                source=FOOTBALL_DATA_SOURCE,
                external_event_id="CL:2026",
                sync_enabled=True,
                expected_version=disabled.version,
                now_utc=NOW,
            )
            assert reenabled.enabled_at == source_config.enabled_at
            assert reenabled.sync_generation == source_config.sync_generation + 2
        return source_config

    monkeypatch.setattr(
        champions_league_sync,
        "_require_target_source_enabled",
        reenable_immediately_after_first_entity_gate,
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=FakeClient(snapshot),  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    imports = list_fixture_imports(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
    )

    assert gate_calls == 3
    assert result.successful_target_count == 0
    assert result.failed_target_count == 1
    assert len(details.two_legged_ties) == 1
    assert {item.external_fixture_id for item in imports} == {"101", "102"}


def test_database_backend_materializes_idempotently_and_saves_strict_90_results(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-sync.db"
    tournament_id = _create_enabled_tournament(database_path)
    contest_id = _attach_contest(database_path, tournament_id=tournament_id)
    client = FakeClient(_playoff_snapshot())

    first = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    repeated = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )

    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    assert first.created_tie_count == 1
    assert first.conflict_count == 0
    assert repeated.created_tie_count == 0
    assert repeated.conflict_count == 0
    assert len(details.two_legged_ties) == 1
    assert details.two_legged_ties[0].round_key == "playoff"
    assert details.two_legged_ties[0].bracket_position == 1
    with create_connection(database_path) as connection:
        local_rows = connection.execute(
            """
            SELECT matches.leg_number, matches.home_score_final,
                   matches.away_score_final
            FROM shared_match_links AS link
            JOIN matches ON matches.id = link.match_id
            WHERE link.contest_id = ?
            ORDER BY matches.leg_number
            """,
            (contest_id,),
        ).fetchall()
    assert [int(row["leg_number"]) for row in local_rows] == [1, 2]
    assert {
        item.import_status
        for item in list_fixture_imports(
            database_path=database_path,
            shared_tournament_id=tournament_id,
            source=FOOTBALL_DATA_SOURCE,
        )
    } == {"imported"}

    client.result = _playoff_snapshot(finished=True)
    finished = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2027, 2, 18, 12, 0, tzinfo=timezone.utc),
        )
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    tie = details.two_legged_ties[0]

    assert finished.saved_result_count == 3
    assert finished.conflict_count == 0
    assert (tie.first_leg.home_score, tie.first_leg.away_score) == (2, 1)
    assert (tie.second_leg.home_score, tie.second_leg.away_score) == (1, 0)
    assert (
        tie.second_leg_extra_time_home_score,
        tie.second_leg_extra_time_away_score,
    ) == (0, 0)
    assert (tie.second_leg_home_penalty_score, tie.second_leg_away_penalty_score) == (
        5,
        4,
    )
    assert tie.advancing_team_id == tie.second_team.id
    with create_connection(database_path) as connection:
        local_rows = connection.execute(
            """
            SELECT matches.leg_number, matches.home_score_final,
                   matches.away_score_final
            FROM shared_match_links AS link
            JOIN matches ON matches.id = link.match_id
            WHERE link.contest_id = ?
            ORDER BY matches.leg_number
            """,
            (contest_id,),
        ).fetchall()
    assert [
        (int(row["home_score_final"]), int(row["away_score_final"]))
        for row in local_rows
    ] == [(2, 1), (1, 0)]

    client.result = _snapshot_with_round_of_16()
    downstream = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2027, 2, 18, 13, 0, tzinfo=timezone.utc),
        )
    )
    bracket = get_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    playoff_node = next(
        node
        for node in bracket.nodes
        if node.round_key == "playoff" and node.bracket_position == 1
    )
    round_of_16_node = next(
        node
        for node in bracket.nodes
        if node.round_key == "round_of_16" and node.bracket_position == 1
    )
    assert downstream.created_tie_count == 1
    assert round_of_16_node.first_source_node_id == playoff_node.id
    assert round_of_16_node.resolved_first_team_id == tie.second_team.id

    finished_snapshot = _playoff_snapshot(finished=True)
    finished_tie = finished_snapshot.two_legged_ties[0]
    changed_score = replace(finished_tie.first_leg.score, regular_home=3)
    client.result = replace(
        finished_snapshot,
        two_legged_ties=(
            replace(
                finished_tie,
                first_leg=replace(
                    finished_tie.first_leg,
                    score=changed_score,
                    last_updated_at_utc="2027-02-19T00:00:00Z",
                ),
            ),
        ),
    )
    conflict = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2027, 2, 19, 12, 0, tzinfo=timezone.utc),
        )
    )
    unchanged = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]
    assert conflict.conflict_count >= 1
    assert (unchanged.first_leg.home_score, unchanged.first_leg.away_score) == (2, 1)


def test_database_backend_final_saves_90_score_winner_and_champion_fanout(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-final.db"
    tournament_id = _create_enabled_tournament(database_path)
    contest_id = _attach_contest(database_path, tournament_id=tournament_id)
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    details = save_shared_champion_settings(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        enabled=True,
        deadline_at="2027-05-30T20:00:00Z",
        points=10,
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username="owner",
        now_utc=NOW,
    )
    teams = {team.name: team.id for team in details.teams}
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    _materialize_finished_semifinal(
        database_path,
        tournament_id=tournament_id,
        bracket_position=1,
        first_team_id=teams["Alpha FC"],
        second_team_id=teams["Beta FC"],
        winner_team_id=teams["Alpha FC"],
    )
    _materialize_finished_semifinal(
        database_path,
        tournament_id=tournament_id,
        bracket_position=2,
        first_team_id=teams["Gamma FC"],
        second_team_id=teams["Delta FC"],
        winner_team_id=teams["Gamma FC"],
    )

    scheduled_final = _external_match(
        501,
        home_id=10,
        home_name="Alpha FC",
        away_id=30,
        away_name="Gamma FC",
        starts_at="2027-05-31T20:00:00Z",
        round_key="final",
        provider_stage="FINAL",
        last_updated="2027-05-10T12:00:00Z",
    )
    scheduled_snapshot = replace(_snapshot(), final=scheduled_final)
    client = FakeClient(scheduled_snapshot)
    scheduled = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2027, 5, 10, 12, 0, tzinfo=timezone.utc),
        )
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    shared_final = next(
        match for match in details.matches if match.round_key == "final"
    )
    assert scheduled.created_match_count == 1
    assert scheduled.conflict_count == 0
    assert shared_final.status == "scheduled"

    final_score = ExternalMatchScore(
        regular_home=1,
        regular_away=1,
        extra_time_home=0,
        extra_time_away=0,
        penalty_home=5,
        penalty_away=4,
        winner_external_team_id="10",
        duration="PENALTY_SHOOTOUT",
    )
    client.result = replace(
        scheduled_snapshot,
        final=replace(
            scheduled_final,
            status="FINISHED",
            score=final_score,
            last_updated_at_utc="2027-06-01T00:00:00Z",
        ),
    )
    finished = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2027, 6, 1, 12, 0, tzinfo=timezone.utc),
        )
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    shared_final = next(
        match for match in details.matches if match.round_key == "final"
    )

    assert finished.saved_result_count == 1
    assert finished.conflict_count == 0
    assert (shared_final.home_score, shared_final.away_score) == (1, 1)
    assert shared_final.advancing_team_id == teams["Alpha FC"]
    assert details.champion_prediction.actual_champion is not None
    assert details.champion_prediction.actual_champion.id == teams["Alpha FC"]
    with create_connection(database_path) as connection:
        local_final = connection.execute(
            """
            SELECT matches.home_score_final, matches.away_score_final,
                   ties.advancing_team_id
            FROM shared_match_links AS link
            JOIN matches ON matches.id = link.match_id
            JOIN ties ON ties.id = matches.tie_id
            WHERE link.shared_match_id = ? AND link.contest_id = ?
            """,
            (shared_final.id, contest_id),
        ).fetchone()
        local_champion = connection.execute(
            "SELECT champion_team_id FROM contests WHERE id = ?",
            (contest_id,),
        ).fetchone()
    assert local_final is not None
    assert (
        int(local_final["home_score_final"]),
        int(local_final["away_score_final"]),
    ) == (1, 1)
    assert int(local_final["advancing_team_id"]) == teams["Alpha FC"]
    assert local_champion is not None
    assert int(local_champion["champion_team_id"]) == teams["Alpha FC"]


def test_database_backend_tombstone_prevents_recreation(tmp_path: Path) -> None:
    database_path = tmp_path / "ucl-tombstone.db"
    tournament_id = _create_enabled_tournament(database_path)
    client = FakeClient(_playoff_snapshot())
    asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    tie = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]
    delete_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        shared_tie_id=tie.id,
        expected_version=tie.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username=None,
        now_utc=NOW,
    )

    repeated = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    imports = list_fixture_imports(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
    )
    assert repeated.created_tie_count == 0
    assert details.two_legged_ties == ()
    assert {item.import_status for item in imports} == {"tombstoned"}

    original_tie = _playoff_snapshot().two_legged_ties[0]
    client.result = replace(
        _playoff_snapshot(),
        two_legged_ties=(
            replace(
                original_tie,
                first_leg=replace(
                    original_tie.first_leg,
                    external_match_id="111",
                    last_updated_at_utc="2026-09-03T00:00:00Z",
                ),
                second_leg=replace(
                    original_tie.second_leg,
                    external_match_id="112",
                    last_updated_at_utc="2026-09-03T00:00:00Z",
                ),
            ),
        ),
    )
    changed_provider_ids = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    assert changed_provider_ids.created_tie_count == 0
    assert changed_provider_ids.conflict_count >= 1
    assert (
        get_shared_tournament_details(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).two_legged_ties
        == ()
    )


def test_database_backend_recovers_claim_after_crash_between_create_and_bind(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-claim-recovery.db"
    tournament_id = _create_enabled_tournament(database_path)
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    bracket = ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    node = next(
        item
        for item in bracket.nodes
        if item.round_key == "playoff" and item.bracket_position == 1
    )
    node = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        round_key="playoff",
        bracket_position=1,
        first_source_node_id=None,
        second_source_node_id=None,
        resolved_first_team_id=details.teams[0].id,
        resolved_second_team_id=details.teams[1].id,
        first_leg_starts_at_utc="2027-02-10T20:00:00Z",
        second_leg_starts_at_utc="2027-02-17T20:00:00Z",
        expected_version=node.version,
    ).node
    claim_external_tie_for_materialization(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
        external_event_id="CL:2026",
        external_tie_id="playoff:10:20",
        round_key="playoff",
        bracket_position=1,
        claim_token="crashed-worker",
        now_utc=NOW,
    )
    tie = create_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        first_team_id=details.teams[0].id,
        second_team_id=details.teams[1].id,
        first_leg_starts_at_utc="2027-02-10T20:00:00Z",
        second_leg_starts_at_utc="2027-02-17T20:00:00Z",
        actor_telegram_user_id=OWNER_ID,
        now_utc=NOW,
        round_key="playoff",
        bracket_position=1,
    )
    mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        node_id=node.id,
        expected_version=node.version,
        shared_tie_id=tie.id,
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=FakeClient(_playoff_snapshot()),  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2026, 9, 2, 12, 16, tzinfo=timezone.utc),
        )
    )
    link = get_external_tie_link(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
        external_event_id="CL:2026",
        external_tie_id="playoff:10:20",
    )
    assert result.failed_target_count == 0
    assert result.created_tie_count == 0
    assert link is not None
    assert link.shared_tie_id == tie.id
    assert link.materialization_claim is None


def test_crashed_unbound_tie_delete_is_tombstoned_and_never_resurrected(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-crash-delete.db"
    tournament_id = _create_enabled_tournament(database_path)
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    teams = {team.name: team.id for team in details.teams}
    bracket = ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    node = next(
        item
        for item in bracket.nodes
        if item.round_key == "playoff" and item.bracket_position == 1
    )
    configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        round_key="playoff",
        bracket_position=1,
        first_source_node_id=None,
        second_source_node_id=None,
        resolved_first_team_id=teams["Alpha FC"],
        resolved_second_team_id=teams["Beta FC"],
        first_leg_starts_at_utc="2027-02-10T20:00:00Z",
        second_leg_starts_at_utc="2027-02-17T20:00:00Z",
        expected_version=node.version,
    )
    claim_external_tie_for_materialization(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
        external_event_id="CL:2026",
        external_tie_id="playoff:10:20",
        round_key="playoff",
        bracket_position=1,
        claim_token="crashed-before-bind",
        now_utc=NOW,
    )
    unbound_tie = create_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        first_team_id=teams["Alpha FC"],
        second_team_id=teams["Beta FC"],
        first_leg_starts_at_utc="2027-02-10T20:00:00Z",
        second_leg_starts_at_utc="2027-02-17T20:00:00Z",
        actor_telegram_user_id=OWNER_ID,
        now_utc=NOW,
        round_key="playoff",
        bracket_position=1,
    )
    delete_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        shared_tie_id=unbound_tie.id,
        expected_version=unbound_tie.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username="owner",
        now_utc=NOW,
    )
    tombstone = get_external_tie_link(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
        external_event_id="CL:2026",
        external_tie_id="playoff:10:20",
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=FakeClient(_playoff_snapshot()),  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2026, 9, 2, 12, 16, tzinfo=timezone.utc),
        )
    )

    assert tombstone is not None
    assert tombstone.tombstoned_at is not None
    assert result.created_tie_count == 0
    assert (
        get_shared_tournament_details(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).two_legged_ties
        == ()
    )


def test_database_backend_updates_schedule_only_before_current_deadline(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-schedule.db"
    tournament_id = _create_enabled_tournament(database_path)
    initial = _playoff_snapshot()
    client = FakeClient(initial)
    asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )

    initial_tie = initial.two_legged_ties[0]
    corrected_tie = replace(
        initial_tie,
        first_leg=replace(
            initial_tie.first_leg,
            starts_at_utc="2027-02-11T20:00:00Z",
            last_updated_at_utc="2026-09-03T12:00:00Z",
        ),
        second_leg=replace(
            initial_tie.second_leg,
            starts_at_utc="2027-02-18T20:00:00Z",
            last_updated_at_utc="2026-09-03T12:00:00Z",
        ),
    )
    corrected_snapshot = replace(initial, two_legged_ties=(corrected_tie,))
    client.result = corrected_snapshot
    corrected = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        )
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    bracket_node = next(
        node
        for node in get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).nodes
        if node.round_key == "playoff" and node.bracket_position == 1
    )
    assert corrected.updated_start_count == 2
    assert corrected.conflict_count == 0
    assert details.two_legged_ties[0].first_leg.starts_at_utc == (
        "2027-02-11T20:00:00Z"
    )
    assert details.two_legged_ties[0].second_leg.starts_at_utc == (
        "2027-02-18T20:00:00Z"
    )
    assert bracket_node.first_leg_starts_at_utc == "2027-02-11T20:00:00Z"
    assert bracket_node.second_leg_starts_at_utc == "2027-02-18T20:00:00Z"

    late_tie = replace(
        corrected_tie,
        first_leg=replace(
            corrected_tie.first_leg,
            starts_at_utc="2027-02-12T20:00:00Z",
            last_updated_at_utc="2027-02-11T21:00:00Z",
        ),
        second_leg=replace(
            corrected_tie.second_leg,
            last_updated_at_utc="2027-02-11T21:00:00Z",
        ),
    )
    client.result = replace(corrected_snapshot, two_legged_ties=(late_tie,))
    rejected = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2027, 2, 11, 21, 0, tzinfo=timezone.utc),
        )
    )
    unchanged = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]
    assert rejected.updated_start_count == 0
    assert rejected.conflict_count == 1
    assert unchanged.first_leg.starts_at_utc == "2027-02-11T20:00:00Z"


def test_database_backend_never_moves_deadline_from_scheduled_provider_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-scheduled-deadline.db"
    tournament_id = _create_enabled_tournament(database_path)
    initial = _playoff_snapshot()
    client = FakeClient(initial)
    asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    provider_tie = initial.two_legged_ties[0]
    client.result = replace(
        initial,
        two_legged_ties=(
            replace(
                provider_tie,
                first_leg=replace(
                    provider_tie.first_leg,
                    status="SCHEDULED",
                    starts_at_utc="2027-02-11T20:00:00Z",
                    last_updated_at_utc="2026-09-03T12:00:00Z",
                ),
                second_leg=replace(
                    provider_tie.second_leg,
                    status="SCHEDULED",
                    starts_at_utc="2027-02-18T20:00:00Z",
                    last_updated_at_utc="2026-09-03T12:00:00Z",
                ),
            ),
        ),
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        )
    )
    tie = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]

    assert result.updated_start_count == 0
    assert result.conflict_count == 1
    assert tie.first_leg.starts_at_utc == "2027-02-10T20:00:00Z"
    assert tie.second_leg.starts_at_utc == "2027-02-17T20:00:00Z"


def test_database_backend_moves_both_legs_in_safe_order_after_preflight(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-atomic-schedule-order.db"
    tournament_id = _create_enabled_tournament(database_path)
    initial = _playoff_snapshot()
    client = FakeClient(initial)
    asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    provider_tie = initial.two_legged_ties[0]
    client.result = replace(
        initial,
        two_legged_ties=(
            replace(
                provider_tie,
                first_leg=replace(
                    provider_tie.first_leg,
                    starts_at_utc="2027-03-01T20:00:00Z",
                    last_updated_at_utc="2026-09-03T12:00:00Z",
                ),
                second_leg=replace(
                    provider_tie.second_leg,
                    starts_at_utc="2027-03-08T20:00:00Z",
                    last_updated_at_utc="2026-09-03T12:00:00Z",
                ),
            ),
        ),
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        )
    )
    tie = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]

    assert result.updated_start_count == 2
    assert result.conflict_count == 0
    assert tie.first_leg.starts_at_utc == "2027-03-01T20:00:00Z"
    assert tie.second_leg.starts_at_utc == "2027-03-08T20:00:00Z"


def test_database_backend_never_overwrites_manual_schedule_correction(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-manual-schedule.db"
    tournament_id = _create_enabled_tournament(database_path)
    initial = _playoff_snapshot()
    client = FakeClient(initial)
    asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    tie = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]
    update_shared_match_start(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        shared_match_id=tie.first_leg.id,
        starts_at_utc="2027-02-12T20:00:00Z",
        expected_version=tie.first_leg.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
    )

    provider_tie = initial.two_legged_ties[0]
    client.result = replace(
        initial,
        two_legged_ties=(
            replace(
                provider_tie,
                first_leg=replace(
                    provider_tie.first_leg,
                    starts_at_utc="2027-02-11T20:00:00Z",
                    last_updated_at_utc="2026-09-04T12:00:00Z",
                ),
                second_leg=replace(
                    provider_tie.second_leg,
                    starts_at_utc="2027-02-18T20:00:00Z",
                    last_updated_at_utc="2026-09-04T12:00:00Z",
                ),
            ),
        ),
    )
    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
        )
    )
    current_tie = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]
    bracket_node = next(
        node
        for node in get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).nodes
        if node.round_key == "playoff" and node.bracket_position == 1
    )

    assert result.updated_start_count == 0
    assert result.conflict_count == 1
    assert current_tie.first_leg.starts_at_utc == "2027-02-12T20:00:00Z"
    assert current_tie.second_leg.starts_at_utc == "2027-02-17T20:00:00Z"
    assert bracket_node.first_leg_starts_at_utc == "2027-02-10T20:00:00Z"


def test_database_backend_does_not_retroactively_create_finished_tie(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-retro.db"
    tournament_id = _create_enabled_tournament(database_path)
    client = FakeClient(_playoff_snapshot(finished=True))

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2027, 2, 18, 12, 0, tzinfo=timezone.utc),
        )
    )

    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    assert result.created_tie_count == 0
    assert result.conflict_count == 1
    assert details.two_legged_ties == ()
    assert {
        item.import_status
        for item in list_fixture_imports(
            database_path=database_path,
            shared_tournament_id=tournament_id,
            source=FOOTBALL_DATA_SOURCE,
        )
    } == {"conflict"}


def test_cycle_refreshes_now_after_fetch_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "ucl-stale-now.db"
    tournament_id = _create_enabled_tournament(database_path)
    after_kickoff = datetime(2027, 2, 18, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        champions_league_sync,
        "_current_now",
        lambda _floor=None: after_kickoff,
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=FakeClient(_playoff_snapshot()),  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )

    assert result.created_tie_count == 0
    assert result.conflict_count == 1
    assert (
        get_shared_tournament_details(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).two_legged_ties
        == ()
    )


def test_database_backend_waits_for_timed_status_before_materialization(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-status-confirmation.db"
    tournament_id = _create_enabled_tournament(database_path)
    timed_snapshot = _playoff_snapshot()
    timed_tie = timed_snapshot.two_legged_ties[0]
    scheduled_snapshot = replace(
        timed_snapshot,
        two_legged_ties=(
            replace(
                timed_tie,
                first_leg=replace(timed_tie.first_leg, status="SCHEDULED"),
                second_leg=replace(timed_tie.second_leg, status="SCHEDULED"),
            ),
        ),
    )
    client = FakeClient(scheduled_snapshot)

    unconfirmed = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    client.result = timed_snapshot
    confirmed = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )

    assert unconfirmed.created_tie_count == 0
    assert unconfirmed.conflict_count == 1
    assert confirmed.created_tie_count == 1
    assert confirmed.conflict_count == 0


def test_database_backend_rejects_finished_snapshot_with_changed_start_atomically(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-finished-start-conflict.db"
    tournament_id = _create_enabled_tournament(database_path)
    client = FakeClient(_playoff_snapshot())
    asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    finished_snapshot = _playoff_snapshot(finished=True)
    finished_tie = finished_snapshot.two_legged_ties[0]
    client.result = replace(
        finished_snapshot,
        two_legged_ties=(
            replace(
                finished_tie,
                first_leg=replace(
                    finished_tie.first_leg,
                    starts_at_utc="2027-02-11T20:00:00Z",
                ),
            ),
        ),
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=client,  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=datetime(2027, 2, 18, 12, 0, tzinfo=timezone.utc),
        )
    )
    tie = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]

    assert result.saved_result_count == 0
    assert result.conflict_count == 1
    assert tie.first_leg.status == "scheduled"
    assert tie.second_leg.status == "scheduled"
    assert tie.first_leg.home_score is None
    assert tie.second_leg.home_score is None


def test_database_backend_records_sanitized_exact_mapping_diagnostics(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-mapping-diagnostic.db"
    tournament_id = _create_enabled_tournament(database_path)
    provider_tie = _playoff_snapshot().two_legged_ties[0]
    unmapped = ExternalTeam("99", "Unmapped\n<script> FC")
    snapshot = replace(
        _playoff_snapshot(),
        two_legged_ties=(
            replace(
                provider_tie,
                first_leg=replace(provider_tie.first_leg, away_team=unmapped),
                second_leg=replace(provider_tie.second_leg, home_team=unmapped),
            ),
        ),
    )

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=FakeClient(snapshot),  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    imports = list_fixture_imports(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
    )

    assert result.created_tie_count == 0
    assert result.conflict_count >= 1
    assert {item.external_fixture_id for item in imports} == {"101", "102"}
    assert {item.round_key for item in imports} == {"playoff"}
    assert {item.import_status for item in imports} == {"conflict"}
    for item in imports:
        assert item.last_error is not None
        assert 'name="Unmapped &lt;script&gt; FC"' in item.last_error
        assert 'external_team_id="99"' in item.last_error
        assert 'fixtures="101", "102"' in item.last_error
        assert "\n" not in item.last_error


def test_database_backend_records_bounded_provider_conflicts_on_source(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ucl-provider-diagnostic.db"
    tournament_id = _create_enabled_tournament(database_path)
    conflicts = tuple(
        ProviderConflict(
            code=f"invalid_{index}\ncode",
            external_match_id=str(700 + index),
            message=f"Malformed\nfixture {index} " + "x" * 400,
        )
        for index in range(7)
    )
    snapshot = replace(_snapshot(), conflicts=conflicts)

    result = asyncio.run(
        synchronize_champions_league_tournament_once(
            database_path=database_path,
            client=FakeClient(snapshot),  # type: ignore[arg-type]
            shared_tournament_id=tournament_id,
            season_start_year=2026,
            now_utc=NOW,
        )
    )
    source = get_external_source(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        source=FOOTBALL_DATA_SOURCE,
    )

    assert result.provider_conflict_count == 7
    assert result.conflict_count == 0
    assert source is not None
    assert source.last_error is not None
    assert 'code="invalid_0 code"' in source.last_error
    assert 'fixture="700"' in source.last_error
    assert 'message="Malformed fixture 0 ' in source.last_error
    assert "ещё конфликтов: 2" in source.last_error
    assert "invalid_5" not in source.last_error
    assert "\n" not in source.last_error
    assert len(source.last_error) < 1_800


def test_worker_is_idle_without_token_or_database_access(tmp_path: Path) -> None:
    backend = FakeBackend((10,))
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_champions_league_sync_worker(
                database_path=tmp_path / "missing.sqlite3",
                client=None,
                season_start_year=2026,
                interval_minutes=10,
                backend=backend,
                sleep=stop_after_sleep,
                clock=lambda: NOW,
            )
        )

    assert delays == [600]
    assert backend.list_calls == []


def test_worker_uses_exponential_backoff_after_provider_failure(
    tmp_path: Path,
) -> None:
    backend = FakeBackend((10,))
    client = FakeClient(RuntimeError("provider unavailable"))
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_champions_league_sync_worker(
                database_path=tmp_path / "db.sqlite3",
                client=client,  # type: ignore[arg-type]
                season_start_year=2026,
                interval_minutes=10,
                backend=backend,
                sleep=stop_after_sleep,
                clock=lambda: NOW,
            )
        )

    assert delays == [1_200]
    assert backend.failure_calls == [(10, "provider unavailable")]


def test_worker_keeps_normal_interval_for_persistent_fixture_conflicts(
    tmp_path: Path,
) -> None:
    backend = FakeBackend((10,))
    client = FakeClient(_snapshot())
    delays: list[float] = []

    async def stop_after_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            run_champions_league_sync_worker(
                database_path=tmp_path / "db.sqlite3",
                client=client,  # type: ignore[arg-type]
                season_start_year=2026,
                interval_minutes=10,
                backend=backend,
                sleep=stop_after_sleep,
                clock=lambda: NOW,
            )
        )

    assert delays == [600]
    assert backend.apply_calls == [10]

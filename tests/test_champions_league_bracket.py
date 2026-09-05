from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.champions_league_bracket import (
    CHAMPIONS_LEAGUE_BRACKET_NODE_COUNT,
    ChampionsLeagueBracketConflictError,
    claim_external_tie_for_materialization,
    configure_bracket_node,
    configure_external_source,
    ensure_champions_league_bracket,
    get_champions_league_bracket,
    get_external_tie_link,
    list_enabled_sync_targets,
    list_external_team_links,
    list_fixture_imports,
    mark_bracket_node_materialized,
    mark_fixture_imported,
    record_external_tie_link,
    record_fixture_seen,
    record_sync_attempt,
    record_sync_failure,
    record_sync_success,
    set_external_team_link,
    sync_materialized_node_dates,
)
from app.database import create_connection, initialize_database
from app.shared_tournament_service import (
    create_shared_match,
    create_shared_tournament,
    create_shared_two_legged_tie,
    get_shared_tournament_details,
    save_shared_tournament_teams,
    update_shared_match_start,
)


OWNER_ID = 123
SOURCE = "football-data.org"
EVENT = "CL:2026"


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _create_ucl(database_path: Path, *, suffix: str = "A", team_count: int = 4):
    details = create_shared_tournament(
        database_path=database_path,
        name=f"Лига чемпионов {suffix}",
        template_key="champions_league_2026_27",
        actor_telegram_user_id=OWNER_ID,
    )
    return save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        team_names=[
            f"Команда {suffix}-{number}" for number in range(1, team_count + 1)
        ],
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )


def _node(database_path: Path, tournament_id: int, round_key: str, position: int):
    return next(
        node
        for node in get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).nodes
        if node.round_key == round_key and node.bracket_position == position
    )


def _configure_playoff_node(database_path: Path, shared):
    tournament_id = shared.tournament.id
    node = _node(database_path, tournament_id, "playoff", 1)
    return configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        round_key="playoff",
        bracket_position=1,
        first_source_node_id=None,
        second_source_node_id=None,
        resolved_first_team_id=shared.teams[0].id,
        resolved_second_team_id=shared.teams[1].id,
        first_leg_starts_at_utc="2030-02-01T18:00:00Z",
        second_leg_starts_at_utc="2030-02-08T18:00:00Z",
        expected_version=node.version,
    ).node


def _create_playoff_tie(database_path: Path, shared):
    return create_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        first_team_id=shared.teams[0].id,
        second_team_id=shared.teams[1].id,
        first_leg_starts_at_utc="2030-02-01T18:00:00Z",
        second_leg_starts_at_utc="2030-02-08T18:00:00Z",
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-01-01T00:00:00Z"),
        round_key="playoff",
        bracket_position=1,
    )


def test_ensure_creates_exact_canonical_skeleton_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "bracket.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path)

    first = ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    second = ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )

    assert len(first.nodes) == CHAMPIONS_LEAGUE_BRACKET_NODE_COUNT == 23
    assert second == first
    assert [
        (node.round_key, node.round_name, node.node_format)
        for node in first.nodes
        if node.bracket_position == 1
    ] == [
        ("playoff", "Стыковые матчи", "two_legged"),
        ("round_of_16", "1/8 финала", "two_legged"),
        ("quarterfinal", "1/4 финала", "two_legged"),
        ("semifinal", "1/2 финала", "two_legged"),
        ("final", "Финал", "single"),
    ]
    assert all(node.sync_status == "pending" for node in first.nodes)
    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_bracket_nodes WHERE shared_tournament_id = ?",
                (shared.tournament.id,),
            ).fetchone()[0]
            == 23
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_external_source_uses_tournament_optimistic_lock_and_allows_same_event(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "source.db"
    initialize_database(database_path)
    first = _create_ucl(database_path, suffix="A")
    second = _create_ucl(database_path, suffix="B")

    first_config = configure_external_source(
        database_path=database_path,
        shared_tournament_id=first.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        sync_enabled=True,
        expected_tournament_version=first.tournament.version,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    second_config = configure_external_source(
        database_path=database_path,
        shared_tournament_id=second.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        sync_enabled=True,
        expected_tournament_version=second.tournament.version,
        now_utc=_time("2029-01-02T00:00:00Z"),
    )

    assert first_config.enabled_at == "2029-01-01T00:00:00Z"
    assert second_config.enabled_at == "2029-01-02T00:00:00Z"
    assert first_config.sync_generation == 1
    assert second_config.sync_generation == 1
    assert {
        item.shared_tournament_id
        for item in list_enabled_sync_targets(database_path=database_path)
    } == {
        first.tournament.id,
        second.tournament.id,
    }
    with pytest.raises(ChampionsLeagueBracketConflictError):
        configure_external_source(
            database_path=database_path,
            shared_tournament_id=first.tournament.id,
            source=SOURCE,
            external_event_id="CL:2027",
            sync_enabled=True,
            expected_version=first_config.version,
            expected_tournament_version=first.tournament.version,
            now_utc=_time("2029-01-03T00:00:00Z"),
        )

    current = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=first.tournament.id,
    )
    changed = configure_external_source(
        database_path=database_path,
        shared_tournament_id=first.tournament.id,
        source=SOURCE,
        external_event_id="CL:2027",
        sync_enabled=True,
        expected_version=first_config.version,
        expected_tournament_version=current.tournament.version,
        now_utc=_time("2029-01-03T00:00:00Z"),
    )
    assert changed.enabled_at == "2029-01-03T00:00:00Z"
    assert changed.sync_generation == first_config.sync_generation + 1


def test_materialization_is_idempotent_and_upstream_correction_fails_closed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "materialization.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    node = _configure_playoff_node(database_path, shared)
    tie = _create_playoff_tie(database_path, shared)

    materialized = mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        node_id=node.id,
        expected_version=node.version,
        shared_tie_id=tie.id,
    )
    with create_connection(database_path) as connection:
        versions_before = tuple(
            row["version"]
            for row in connection.execute(
                "SELECT version FROM shared_matches WHERE shared_tie_id = ? ORDER BY leg_number",
                (tie.id,),
            )
        )
        tie_version_before = connection.execute(
            "SELECT version FROM shared_two_legged_ties WHERE id = ?", (tie.id,)
        ).fetchone()[0]
    repeated = mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        node_id=node.id,
        expected_version=materialized.version,
        shared_tie_id=tie.id,
    )
    with create_connection(database_path) as connection:
        versions_after = tuple(
            row["version"]
            for row in connection.execute(
                "SELECT version FROM shared_matches WHERE shared_tie_id = ? ORDER BY leg_number",
                (tie.id,),
            )
        )
        tie_version_after = connection.execute(
            "SELECT version FROM shared_two_legged_ties WHERE id = ?", (tie.id,)
        ).fetchone()[0]

    assert repeated == materialized
    assert versions_after == versions_before
    assert tie_version_after == tie_version_before

    correction = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        round_key="playoff",
        bracket_position=1,
        first_source_node_id=None,
        second_source_node_id=None,
        resolved_first_team_id=shared.teams[2].id,
        resolved_second_team_id=shared.teams[1].id,
        first_leg_starts_at_utc=materialized.first_leg_starts_at_utc,
        second_leg_starts_at_utc=materialized.second_leg_starts_at_utc,
        expected_version=materialized.version,
    )
    assert correction.action == "conflict"
    assert correction.node.sync_status == "conflict"
    assert correction.node.resolved_first_team_id == shared.teams[0].id
    assert correction.node.materialized_shared_tie_id == tie.id


def test_date_only_sync_requires_underlying_materialized_match_update(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "dates.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    node = _configure_playoff_node(database_path, shared)
    tie = _create_playoff_tie(database_path, shared)
    materialized = mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        node_id=node.id,
        expected_version=node.version,
        shared_tie_id=tie.id,
    )

    with pytest.raises(ChampionsLeagueBracketConflictError):
        sync_materialized_node_dates(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            node_id=node.id,
            expected_version=materialized.version,
            first_leg_starts_at_utc="2030-02-02T18:00:00Z",
            second_leg_starts_at_utc="2030-02-08T18:00:00Z",
        )

    updated_match = update_shared_match_start(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=tie.first_leg.id,
        starts_at_utc="2030-02-02T18:00:00Z",
        expected_version=tie.first_leg.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-01-01T00:00:00Z"),
    )
    assert updated_match.starts_at_utc == "2030-02-02T18:00:00Z"
    synced = sync_materialized_node_dates(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        node_id=node.id,
        expected_version=materialized.version,
        first_leg_starts_at_utc="2030-02-02T18:00:00Z",
        second_leg_starts_at_utc="2030-02-08T18:00:00Z",
    )
    assert synced.first_leg_starts_at_utc == "2030-02-02T18:00:00Z"
    assert synced.sync_status == "materialized"

    conflict = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        round_key="playoff",
        bracket_position=1,
        first_source_node_id=None,
        second_source_node_id=None,
        resolved_first_team_id=shared.teams[2].id,
        resolved_second_team_id=shared.teams[1].id,
        first_leg_starts_at_utc=synced.first_leg_starts_at_utc,
        second_leg_starts_at_utc=synced.second_leg_starts_at_utc,
        expected_version=synced.version,
    ).node
    current_tie = next(
        item
        for item in get_shared_tournament_details(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
        ).two_legged_ties
        if item.id == tie.id
    )
    update_shared_match_start(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        shared_match_id=current_tie.second_leg.id,
        starts_at_utc="2030-02-09T18:00:00Z",
        expected_version=current_tie.second_leg.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-01-01T00:00:00Z"),
    )
    conflict_after_date_sync = sync_materialized_node_dates(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        node_id=node.id,
        expected_version=conflict.version,
        first_leg_starts_at_utc="2030-02-02T18:00:00Z",
        second_leg_starts_at_utc="2030-02-09T18:00:00Z",
    )
    assert conflict_after_date_sync.sync_status == "conflict"
    assert conflict_after_date_sync.sync_error == conflict.sync_error


def test_source_edges_are_adjacent_unique_and_match_known_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "sources.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path, team_count=6)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    playoff_node = _configure_playoff_node(database_path, shared)
    tie = _create_playoff_tie(database_path, shared)
    playoff_node = mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        node_id=playoff_node.id,
        expected_version=playoff_node.version,
        shared_tie_id=tie.id,
    )
    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE shared_two_legged_ties SET advancing_team_id = ? WHERE id = ?",
            (shared.teams[0].id, tie.id),
        )

    quarterfinal = _node(database_path, shared.tournament.id, "quarterfinal", 1)
    with pytest.raises(ChampionsLeagueBracketConflictError):
        configure_bracket_node(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            round_key="quarterfinal",
            bracket_position=1,
            first_source_node_id=playoff_node.id,
            second_source_node_id=None,
            resolved_first_team_id=shared.teams[0].id,
            resolved_second_team_id=shared.teams[2].id,
            first_leg_starts_at_utc="2030-03-01T18:00:00Z",
            second_leg_starts_at_utc="2030-03-08T18:00:00Z",
            expected_version=quarterfinal.version,
        )

    round_of_16 = _node(database_path, shared.tournament.id, "round_of_16", 1)
    with pytest.raises(ChampionsLeagueBracketConflictError, match="победителем"):
        configure_bracket_node(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            round_key="round_of_16",
            bracket_position=1,
            first_source_node_id=playoff_node.id,
            second_source_node_id=None,
            resolved_first_team_id=shared.teams[3].id,
            resolved_second_team_id=shared.teams[2].id,
            first_leg_starts_at_utc="2030-03-01T18:00:00Z",
            second_leg_starts_at_utc="2030-03-08T18:00:00Z",
            expected_version=round_of_16.version,
        )
    configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        round_key="round_of_16",
        bracket_position=1,
        first_source_node_id=playoff_node.id,
        second_source_node_id=None,
        resolved_first_team_id=shared.teams[0].id,
        resolved_second_team_id=shared.teams[2].id,
        first_leg_starts_at_utc="2030-03-01T18:00:00Z",
        second_leg_starts_at_utc="2030-03-08T18:00:00Z",
        expected_version=round_of_16.version,
    )
    duplicate = _node(database_path, shared.tournament.id, "round_of_16", 2)
    with pytest.raises(ChampionsLeagueBracketConflictError, match="уже используется"):
        configure_bracket_node(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            round_key="round_of_16",
            bracket_position=2,
            first_source_node_id=playoff_node.id,
            second_source_node_id=None,
            resolved_first_team_id=shared.teams[0].id,
            resolved_second_team_id=shared.teams[3].id,
            first_leg_starts_at_utc="2030-03-02T18:00:00Z",
            second_leg_starts_at_utc="2030-03-09T18:00:00Z",
            expected_version=duplicate.version,
        )


def test_external_ledgers_are_conflict_safe_and_tombstone_deleted_fixture(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ledger.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    current = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    configure_external_source(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        sync_enabled=True,
        expected_tournament_version=current.tournament.version,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    first_link = set_external_team_link(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        team_id=shared.teams[0].id,
        external_team_id="10",
    )
    assert (
        set_external_team_link(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            source=SOURCE,
            team_id=shared.teams[0].id,
            external_team_id="10",
        )
        == first_link
    )
    with pytest.raises(ChampionsLeagueBracketConflictError):
        set_external_team_link(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            source=SOURCE,
            team_id=shared.teams[1].id,
            external_team_id="10",
        )
    assert list_external_team_links(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
    ) == (first_link,)

    node = _configure_playoff_node(database_path, shared)
    reservation = claim_external_tie_for_materialization(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_tie_id="playoff:10:20",
        round_key="playoff",
        bracket_position=1,
        claim_token="worker-a",
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    assert reservation.shared_tie_id is None
    assert reservation.materialization_claim == "worker-a"
    with pytest.raises(ChampionsLeagueBracketConflictError, match="другим процессом"):
        claim_external_tie_for_materialization(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            source=SOURCE,
            external_event_id=EVENT,
            external_tie_id="playoff:10:20",
            round_key="playoff",
            bracket_position=1,
            claim_token="worker-b",
            now_utc=_time("2029-01-01T00:01:00Z"),
        )
    tie = _create_playoff_tie(database_path, shared)
    materialized = mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        node_id=node.id,
        expected_version=node.version,
        shared_tie_id=tie.id,
    )
    record_external_tie_link(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_tie_id="playoff:10:20",
        shared_tie_id=tie.id,
        claim_token="worker-a",
    )
    fixture = record_fixture_seen(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_fixture_id="1001",
        external_tie_id="playoff:10:20",
        round_key="playoff",
        bracket_position=1,
        leg_number=1,
        payload='{"status":"TIMED"}',
        provider_updated_at="2029-01-01T01:00:00Z",
    )
    imported = mark_fixture_imported(
        database_path=database_path,
        fixture_import_id=fixture.id,
        shared_match_id=tie.first_leg.id,
        shared_tie_id=tie.id,
        expected_version=fixture.version,
    )
    assert imported.import_status == "imported"

    newer = record_fixture_seen(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_fixture_id="1001",
        external_tie_id="playoff:10:20",
        round_key="playoff",
        bracket_position=1,
        leg_number=1,
        payload='{"status":"FINISHED"}',
        provider_updated_at="2029-01-02T01:00:00Z",
    )
    assert newer.import_status == "pending"
    with pytest.raises(ChampionsLeagueBracketConflictError):
        mark_fixture_imported(
            database_path=database_path,
            fixture_import_id=fixture.id,
            shared_match_id=tie.first_leg.id,
            shared_tie_id=tie.id,
            expected_version=imported.version,
        )
    with pytest.raises(ChampionsLeagueBracketConflictError, match="устаревший"):
        record_fixture_seen(
            database_path=database_path,
            shared_tournament_id=shared.tournament.id,
            source=SOURCE,
            external_event_id=EVENT,
            external_fixture_id="1001",
            external_tie_id="playoff:10:20",
            round_key="playoff",
            bracket_position=1,
            leg_number=1,
            payload='{"status":"TIMED"}',
            provider_updated_at="2029-01-01T00:00:00Z",
        )

    moved = record_fixture_seen(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_fixture_id="1001",
        external_tie_id="playoff:10:20",
        round_key="playoff",
        bracket_position=2,
        leg_number=1,
        payload='{"status":"TIMED","corrected":true}',
    )
    assert moved.import_status == "conflict"
    assert moved.bracket_position == 1
    assert moved.shared_bracket_node_id == materialized.id
    assert moved.provider_updated_at == "2029-01-02T01:00:00Z"

    with create_connection(database_path) as connection:
        connection.execute("DELETE FROM shared_two_legged_ties WHERE id = ?", (tie.id,))
    tombstone = list_fixture_imports(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
    )[0]
    tie_link = get_external_tie_link(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_tie_id="playoff:10:20",
    )
    deleted_node = _node(database_path, shared.tournament.id, "playoff", 1)

    assert tombstone.import_status == "tombstoned"
    assert tombstone.shared_tie_id is None
    assert tombstone.shared_match_id is None
    assert tombstone.tombstoned_at is not None
    assert tie_link is not None and tie_link.shared_tie_id is None
    assert tie_link.tombstoned_at is not None
    assert tie_link.materialization_claim is None
    assert deleted_node.materialized_shared_tie_id is None
    assert deleted_node.sync_status == "conflict"
    seen_again = record_fixture_seen(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_fixture_id="1001",
        external_tie_id="playoff:10:20",
        round_key="playoff",
        bracket_position=1,
        leg_number=1,
        payload='{"status":"TIMED"}',
        provider_updated_at="2020-01-01T00:00:00Z",
    )
    assert seen_again.import_status == "tombstoned"
    assert seen_again.provider_updated_at == "2029-01-02T01:00:00Z"
    with pytest.raises(ChampionsLeagueBracketConflictError):
        mark_fixture_imported(
            database_path=database_path,
            fixture_import_id=tombstone.id,
            shared_match_id=tie.first_leg.id,
            shared_tie_id=tie.id,
            expected_version=tombstone.version,
        )
    with create_connection(database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_exact_conflict_self_heal_requires_explicit_provider_policy(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "exact-conflict-self-heal.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    configured = _configure_playoff_node(database_path, shared)
    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE shared_bracket_nodes
            SET sync_status = 'conflict', sync_error = 'transient conflict',
                version = version + 1
            WHERE id = ?
            """,
            (configured.id,),
        )
    conflicted = _node(database_path, shared.tournament.id, "playoff", 1)

    manual_noop = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        round_key="playoff",
        bracket_position=1,
        first_source_node_id=conflicted.first_source_node_id,
        second_source_node_id=conflicted.second_source_node_id,
        resolved_first_team_id=conflicted.resolved_first_team_id,
        resolved_second_team_id=conflicted.resolved_second_team_id,
        first_leg_starts_at_utc=conflicted.first_leg_starts_at_utc,
        second_leg_starts_at_utc=conflicted.second_leg_starts_at_utc,
        expected_version=conflicted.version,
    )

    assert manual_noop.action == "noop"
    assert manual_noop.node == conflicted

    healed = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        round_key="playoff",
        bracket_position=1,
        first_source_node_id=conflicted.first_source_node_id,
        second_source_node_id=conflicted.second_source_node_id,
        resolved_first_team_id=conflicted.resolved_first_team_id,
        resolved_second_team_id=conflicted.resolved_second_team_id,
        first_leg_starts_at_utc=conflicted.first_leg_starts_at_utc,
        second_leg_starts_at_utc=conflicted.second_leg_starts_at_utc,
        expected_version=conflicted.version,
        resolve_exact_provider_conflict=True,
    )

    assert healed.action == "updated"
    assert healed.node.sync_status == "pending"
    assert healed.node.sync_error is None
    assert healed.node.version == conflicted.version + 1
    assert healed.node.materialized_shared_tie_id is None

    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE shared_bracket_nodes
            SET sync_status = 'conflict', sync_error = 'manual correction needed',
                version = version + 1
            WHERE id = ?
            """,
            (configured.id,),
        )
    manual_conflict = _node(database_path, shared.tournament.id, "playoff", 1)
    manual_correction = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        round_key="playoff",
        bracket_position=1,
        first_source_node_id=manual_conflict.first_source_node_id,
        second_source_node_id=manual_conflict.second_source_node_id,
        resolved_first_team_id=manual_conflict.resolved_first_team_id,
        resolved_second_team_id=manual_conflict.resolved_second_team_id,
        first_leg_starts_at_utc="2030-02-02T18:00:00Z",
        second_leg_starts_at_utc=manual_conflict.second_leg_starts_at_utc,
        expected_version=manual_conflict.version,
    )

    assert manual_correction.action == "updated"
    assert manual_correction.node.sync_status == "pending"
    assert manual_correction.node.sync_error is None
    assert manual_correction.node.first_leg_starts_at_utc == "2030-02-02T18:00:00Z"


def test_unbound_tie_is_tombstoned_by_bracket_position_after_crash(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unbound-tie-delete.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    configure_external_source(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        sync_enabled=True,
        expected_tournament_version=shared.tournament.version,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    node = _configure_playoff_node(database_path, shared)
    fixture = record_fixture_seen(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_fixture_id="crash-leg-1",
        external_tie_id="crash-tie",
        round_key="playoff",
        bracket_position=1,
        leg_number=1,
        payload='{"status":"TIMED"}',
    )
    reservation = claim_external_tie_for_materialization(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_tie_id="crash-tie",
        round_key="playoff",
        bracket_position=1,
        claim_token="crashed-worker",
        now_utc=_time("2029-01-01T00:01:00Z"),
    )
    tie = _create_playoff_tie(database_path, shared)

    assert fixture.shared_tie_id is None
    assert reservation.shared_tie_id is None
    assert node.materialized_shared_tie_id is None
    with create_connection(database_path) as connection:
        connection.execute("DELETE FROM shared_two_legged_ties WHERE id = ?", (tie.id,))

    deleted_fixture = list_fixture_imports(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
    )[0]
    deleted_link = get_external_tie_link(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_tie_id="crash-tie",
    )
    deleted_node = _node(database_path, shared.tournament.id, "playoff", 1)
    assert deleted_fixture.import_status == "tombstoned"
    assert deleted_fixture.shared_tie_id is None
    assert deleted_fixture.shared_match_id is None
    assert deleted_link is not None and deleted_link.tombstoned_at is not None
    assert deleted_link.materialization_claim is None
    assert deleted_link.round_key == "playoff"
    assert deleted_link.bracket_position == 1
    assert deleted_node.sync_status == "conflict"
    assert deleted_node.materialized_shared_tie_id is None
    seen_again = claim_external_tie_for_materialization(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_tie_id="crash-tie",
        round_key="playoff",
        bracket_position=1,
        claim_token="retry-worker",
        now_utc=_time("2029-01-02T00:00:00Z"),
    )
    assert seen_again.tombstoned_at == deleted_link.tombstoned_at
    assert seen_again.materialization_claim is None


def test_unbound_final_ledger_and_node_are_tombstoned_by_position(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "unbound-final-delete.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    configure_external_source(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        sync_enabled=True,
        expected_tournament_version=shared.tournament.version,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    final_node = _node(database_path, shared.tournament.id, "final", 1)
    configured = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        round_key="final",
        bracket_position=1,
        first_source_node_id=None,
        second_source_node_id=None,
        resolved_first_team_id=shared.teams[0].id,
        resolved_second_team_id=shared.teams[1].id,
        first_leg_starts_at_utc="2030-05-30T18:00:00Z",
        second_leg_starts_at_utc=None,
        expected_version=final_node.version,
    ).node
    fixture = record_fixture_seen(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        external_fixture_id="crash-final",
        round_key="final",
        bracket_position=1,
        leg_number=None,
        payload='{"status":"TIMED"}',
    )
    final_match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-05-30T18:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2030-01-01T00:00:00Z"),
        round_key="final",
        bracket_position=1,
    )
    assert fixture.shared_match_id is None
    assert configured.materialized_shared_match_id is None

    with create_connection(database_path) as connection:
        connection.execute("DELETE FROM shared_matches WHERE id = ?", (final_match.id,))

    deleted_fixture = list_fixture_imports(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
    )[0]
    deleted_node = _node(database_path, shared.tournament.id, "final", 1)
    assert deleted_fixture.import_status == "tombstoned"
    assert deleted_fixture.shared_match_id is None
    assert deleted_node.sync_status == "conflict"
    assert deleted_node.materialized_shared_match_id is None


def test_sync_status_updates_keep_last_success_and_clear_error(tmp_path: Path) -> None:
    database_path = tmp_path / "sync-status.db"
    initialize_database(database_path)
    shared = _create_ucl(database_path)
    configured = configure_external_source(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id=EVENT,
        sync_enabled=True,
        expected_tournament_version=shared.tournament.version,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    attempted = record_sync_attempt(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        attempted_at=_time("2029-01-01T12:00:00Z"),
    )
    success = record_sync_success(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        completed_at=_time("2029-01-02T00:00:00Z"),
    )
    failure = record_sync_failure(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        attempted_at=_time("2029-01-03T00:00:00Z"),
        error="provider unavailable",
    )
    recovered = record_sync_success(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        completed_at=_time("2029-01-04T00:00:00Z"),
    )

    assert success.last_success_at == "2029-01-02T00:00:00Z"
    assert attempted.sync_generation == configured.sync_generation
    assert success.sync_generation == configured.sync_generation
    assert failure.sync_generation == configured.sync_generation
    assert recovered.sync_generation == configured.sync_generation
    assert failure.last_success_at == success.last_success_at
    assert failure.last_error == "provider unavailable"
    assert recovered.last_success_at == "2029-01-04T00:00:00Z"
    assert recovered.last_error is None
    stale_failure = record_sync_failure(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        attempted_at=_time("2029-01-03T12:00:00Z"),
        error="late old failure",
    )
    assert stale_failure == recovered

    current = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    next_event = configure_external_source(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source=SOURCE,
        external_event_id="CL:2027",
        sync_enabled=True,
        expected_version=recovered.version,
        expected_tournament_version=current.tournament.version,
        now_utc=_time("2029-01-05T00:00:00Z"),
    )
    assert next_event.last_attempt_at is None
    assert next_event.last_success_at is None
    assert next_event.last_error is None
    assert next_event.sync_generation == configured.sync_generation + 1

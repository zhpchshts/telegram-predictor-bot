from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import tma_api
from app.audit_service import AuditActor, AuditActorRole
from app.champions_league_bracket import (
    configure_bracket_node,
    configure_external_source,
    ensure_champions_league_bracket,
    mark_bracket_node_materialized,
    record_sync_success,
)
from app.champions_league_sync import ChampionsLeagueSyncCycleResult
from app.contest_service import (
    create_champions_league_2026_27_contest,
    get_contest_details,
)
from app.database import create_connection, initialize_database
from app.shared_tournament_service import (
    create_shared_tournament,
    create_shared_two_legged_tie,
    get_shared_tournament_details,
    save_shared_tournament_teams,
)


OWNER_ID = 123


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _create_shared_ucl(database_path: Path):
    details = create_shared_tournament(
        database_path=database_path,
        name="Лига чемпионов 2026/27",
        template_key="champions_league_2026_27",
        actor_telegram_user_id=OWNER_ID,
    )
    return save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        team_names=["Alpha FC", "Beta FC"],
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )


def _settings(database_path: Path, *, token: str | None = None):
    return SimpleNamespace(
        database_path=database_path,
        football_data_api_token=token,
        champions_league_sync_season=2026,
        champions_league_sync_interval_minutes=10,
    )


def test_shared_ucl_payload_contains_full_empty_bracket_and_safe_sync_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared-ucl.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )

    payload = tma_api._serialize_shared_tournament_details(
        details,
        database_path=database_path,
        football_data_token_configured=False,
        sync_interval_minutes=10,
    )

    bracket = payload["playoff_bracket"]
    assert [round_payload["name"] for round_payload in bracket["rounds"]] == [
        "Стыковые матчи",
        "1/8 финала",
        "1/4 финала",
        "1/2 финала",
        "Финал",
    ]
    assert [len(round_payload["nodes"]) for round_payload in bracket["rounds"]] == [
        8,
        8,
        4,
        2,
        1,
    ]
    assert payload["fixture_sync"]["state"] == "disabled"
    assert payload["fixture_sync"]["token_configured"] is False
    assert payload["teams_locked"] is False
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "api_token" not in serialized
    assert "X-Auth-Token" not in serialized


def test_linked_contest_bracket_uses_local_materialized_entity_id(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "linked-ucl.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path)
    bracket = ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    contest = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=-1001,
        chat_title="Чат ЛЧ",
        telegram_user_id=OWNER_ID,
        first_name="Owner",
        last_name=None,
        username=None,
        contest_name="Прогнозы ЛЧ",
        idempotency_key="linked-ucl",
        audit_actor=AuditActor(
            telegram_chat_id=-1001,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        shared_tournament_id=shared.tournament.id,
    ).contest
    node = next(
        item
        for item in bracket.nodes
        if item.round_key == "playoff" and item.bracket_position == 1
    )
    node = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
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
    tie = create_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        first_team_id=shared.teams[0].id,
        second_team_id=shared.teams[1].id,
        first_leg_starts_at_utc="2030-02-01T18:00:00Z",
        second_leg_starts_at_utc="2030-02-08T18:00:00Z",
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
        round_key="playoff",
        bracket_position=1,
    )
    mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        node_id=node.id,
        expected_version=node.version,
        shared_tie_id=tie.id,
    )
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=-1001,
        contest_id=contest.id,
        telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    with create_connection(database_path) as connection:
        local_tie_id = int(
            connection.execute(
                """
                SELECT tie_id FROM shared_tie_links
                WHERE shared_tie_id = ? AND contest_id = ?
                """,
                (tie.id, contest.id),
            ).fetchone()[0]
        )

    payload = tma_api._serialize_contest_details(details, database_path=database_path)
    playoff_node = payload["playoff_bracket"]["rounds"][0]["nodes"][0]

    assert playoff_node["entity"] == {
        "type": "two_legged_tie",
        "id": local_tie_id,
    }
    assert playoff_node["state"] == "scheduled"


def test_fixture_sync_toggle_uses_tournament_version_and_never_returns_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "toggle-ucl.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path)
    monkeypatch.setattr(tma_api, "load_settings", lambda: _settings(database_path))

    response = asyncio.run(
        tma_api.save_tma_shared_fixture_sync(
            shared.tournament.id,
            tma_api.SaveSharedFixtureSyncRequest(
                enabled=True,
                expected_version=shared.tournament.version,
            ),
            object(),
        )
    )

    tournament = response["shared_tournament"]
    assert tournament["version"] == shared.tournament.version + 1
    assert tournament["fixture_sync"]["enabled"] is True
    assert tournament["fixture_sync"]["state"] == "idle"
    assert tournament["fixture_sync"]["token_configured"] is False
    assert "secret" not in json.dumps(response)

    with pytest.raises(HTTPException) as stale_error:
        asyncio.run(
            tma_api.save_tma_shared_fixture_sync(
                shared.tournament.id,
                tma_api.SaveSharedFixtureSyncRequest(
                    enabled=False,
                    expected_version=shared.tournament.version,
                ),
                object(),
            )
        )
    assert stale_error.value.status_code == 409

    with pytest.raises(HTTPException) as token_error:
        asyncio.run(
            tma_api.run_tma_shared_fixture_sync(
                shared.tournament.id,
                tma_api.SharedTournamentVersionRequest(
                    expected_version=tournament["version"]
                ),
                object(),
            )
        )
    assert token_error.value.status_code == 503


def test_successful_empty_fixture_snapshot_stays_in_waiting_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "empty-snapshot-ucl.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path)
    completed_at = _time("2029-01-01T12:00:00Z")
    configure_external_source(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source="football-data.org",
        external_event_id="CL:2026",
        sync_enabled=True,
        expected_tournament_version=shared.tournament.version,
        now_utc=_time("2029-01-01T11:55:00Z"),
    )
    record_sync_success(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source="football-data.org",
        completed_at=completed_at,
    )
    details = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )

    payload = tma_api._serialize_shared_tournament_details(
        details,
        database_path=database_path,
        football_data_token_configured=True,
        sync_interval_minutes=10,
        now_utc=completed_at,
    )

    assert payload["fixture_sync"]["last_success_at"] == "2029-01-01T12:00:00Z"
    assert payload["fixture_sync"]["stats"]["fixtures_seen"] == 0
    assert payload["fixture_sync"]["state"] == "idle"


def test_manual_fixture_sync_returns_domain_conflicts_but_rejects_apply_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "manual-sync-result.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path)
    monkeypatch.setattr(
        tma_api,
        "load_settings",
        lambda: _settings(database_path, token="football-data-token"),
    )

    result = ChampionsLeagueSyncCycleResult(
        target_count=1,
        fetched=True,
        successful_target_count=1,
        failed_target_count=0,
        provider_conflict_count=1,
        created_tie_count=2,
        created_match_count=0,
        updated_start_count=1,
        saved_result_count=0,
        conflict_count=2,
    )

    async def synchronize_with_conflicts(**_kwargs) -> ChampionsLeagueSyncCycleResult:
        return result

    monkeypatch.setattr(
        tma_api,
        "synchronize_champions_league_tournament_once",
        synchronize_with_conflicts,
    )
    response = asyncio.run(
        tma_api.run_tma_shared_fixture_sync(
            shared.tournament.id,
            tma_api.SharedTournamentVersionRequest(
                expected_version=shared.tournament.version
            ),
            object(),
        )
    )

    assert response["sync_result"] == {
        "fixtures_fetched": True,
        "ties_created": 2,
        "matches_created": 0,
        "matches_updated": 1,
        "results_saved": 0,
        "conflicts": 3,
    }
    assert response["shared_tournament"]["id"] == shared.tournament.id

    async def synchronize_with_apply_failure(
        **_kwargs,
    ) -> ChampionsLeagueSyncCycleResult:
        return ChampionsLeagueSyncCycleResult(
            target_count=1,
            fetched=True,
            successful_target_count=0,
            failed_target_count=1,
            provider_conflict_count=0,
            created_tie_count=0,
            created_match_count=0,
            updated_start_count=0,
            saved_result_count=0,
            conflict_count=0,
        )

    monkeypatch.setattr(
        tma_api,
        "synchronize_champions_league_tournament_once",
        synchronize_with_apply_failure,
    )
    with pytest.raises(HTTPException) as failure:
        asyncio.run(
            tma_api.run_tma_shared_fixture_sync(
                shared.tournament.id,
                tma_api.SharedTournamentVersionRequest(
                    expected_version=shared.tournament.version
                ),
                object(),
            )
        )
    assert failure.value.status_code == 502

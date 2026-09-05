from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import tma_api
from app.access_control import (
    AccessDecision,
    AccessRole,
    AccessVerificationStatus,
    TelegramAdministratorsSnapshot,
)
from app.audit_service import AuditActor, AuditActorRole
from app.champions_league_bracket import (
    configure_bracket_node,
    configure_external_source,
    ensure_champions_league_bracket,
    get_champions_league_bracket,
    mark_bracket_node_materialized,
    mark_fixture_conflict,
    record_fixture_seen,
)
from app.contest_service import (
    MatchCreationConflictError,
    TwoLeggedTieCreationConflictError,
    create_champions_league_2026_27_contest,
    create_match,
    create_two_legged_tie,
    get_contest_details,
    save_tournament_teams,
)
from app.database import create_connection, initialize_database
from app.shared_tournament_service import (
    SharedTournamentCompletionUnavailableError,
    SharedTournamentResultUnavailableError,
    archive_shared_tournament,
    create_shared_match,
    create_shared_tournament,
    create_shared_two_legged_tie,
    get_shared_tournament_details,
    save_shared_champion_result,
    save_shared_champion_settings,
    save_shared_match_result,
    save_shared_tournament_teams,
)
from app.tma_context import TmaChatContext, TmaContext, TmaUserContext


CHAT_ID = -1001
OWNER_ID = 123
ACTOR = AuditActor(
    telegram_chat_id=CHAT_ID,
    telegram_user_id=OWNER_ID,
    role=AuditActorRole.TELEGRAM_ADMIN,
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _settings(database_path: Path):
    return SimpleNamespace(database_path=database_path)


def _shared_management():
    return SimpleNamespace(
        user=SimpleNamespace(
            telegram_user_id=OWNER_ID,
            first_name="Owner",
            last_name=None,
            username=None,
        )
    )


def _contest_management() -> tma_api.ContestManagementContext:
    context = TmaContext(
        user=TmaUserContext(
            telegram_user_id=OWNER_ID,
            first_name="Owner",
            last_name=None,
            username=None,
        ),
        chat=TmaChatContext(
            telegram_chat_id=CHAT_ID,
            chat_type="supergroup",
            title="Чат ЛЧ",
        ),
    )
    access = AccessDecision(
        verification_status=AccessVerificationStatus.VERIFIED,
        role=AccessRole.TELEGRAM_ADMIN,
        can_manage_contests=True,
        can_manage_roles=True,
        administrators=TelegramAdministratorsSnapshot(
            telegram_user_ids=frozenset({OWNER_ID})
        ),
    )
    return tma_api.ContestManagementContext(context=context, access=access)


def _create_independent_ucl(database_path: Path, *, team_count: int = 0):
    contest = create_champions_league_2026_27_contest(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        chat_title="Чат ЛЧ",
        telegram_user_id=OWNER_ID,
        first_name="Owner",
        last_name=None,
        username=None,
        contest_name="Прогнозы ЛЧ",
        idempotency_key="independent-ucl",
        audit_actor=ACTOR,
    ).contest
    if team_count == 0:
        return contest, ()
    teams = save_tournament_teams(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        team_names=[f"Команда {number}" for number in range(1, team_count + 1)],
        audit_actor=ACTOR,
    ).teams
    return contest, teams


def _create_shared_ucl(
    database_path: Path,
    *,
    team_count: int = 6,
    suffix: str = "A",
):
    details = create_shared_tournament(
        database_path=database_path,
        name=f"Общая Лига чемпионов {suffix}",
        template_key="champions_league_2026_27",
        actor_telegram_user_id=OWNER_ID,
    )
    return save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        team_names=[
            f"Shared Team {suffix}-{number}" for number in range(1, team_count + 1)
        ],
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )


def _bracket_node(
    database_path: Path, tournament_id: int, round_key: str, position: int
):
    return next(
        node
        for node in get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=tournament_id,
        ).nodes
        if node.round_key == round_key and node.bracket_position == position
    )


@pytest.mark.parametrize(
    ("round_key", "capacity"),
    [
        ("playoff", 8),
        ("round_of_16", 8),
        ("quarterfinal", 4),
        ("semifinal", 2),
    ],
)
def test_independent_ucl_rejects_extra_two_legged_round_slot_before_insert(
    tmp_path: Path,
    round_key: str,
    capacity: int,
) -> None:
    database_path = tmp_path / f"{round_key}.db"
    initialize_database(database_path)
    contest, teams = _create_independent_ucl(
        database_path,
        team_count=(capacity + 1) * 2,
    )

    for position in range(capacity):
        create_two_legged_tie(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest.id,
            telegram_user_id=OWNER_ID,
            first_name="Owner",
            last_name=None,
            username=None,
            first_team_id=teams[position * 2].id,
            second_team_id=teams[position * 2 + 1].id,
            first_leg_starts_at_utc="2099-02-01T18:00:00Z",
            second_leg_starts_at_utc="2099-02-08T18:00:00Z",
            idempotency_key=f"{round_key}-{position + 1}",
            audit_actor=ACTOR,
            now_utc=_time("2098-01-01T00:00:00Z"),
            round_key=round_key,
        )

    with pytest.raises(
        TwoLeggedTieCreationConflictError,
        match="заполнены все позиции",
    ):
        create_two_legged_tie(
            database_path=database_path,
            telegram_chat_id=CHAT_ID,
            contest_id=contest.id,
            telegram_user_id=OWNER_ID,
            first_name="Owner",
            last_name=None,
            username=None,
            first_team_id=teams[capacity * 2].id,
            second_team_id=teams[capacity * 2 + 1].id,
            first_leg_starts_at_utc="2099-02-01T18:00:00Z",
            second_leg_starts_at_utc="2099-02-08T18:00:00Z",
            idempotency_key=f"{round_key}-overflow",
            audit_actor=ACTOR,
            now_utc=_time("2098-01-01T00:00:00Z"),
            round_key=round_key,
        )

    with create_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS tie_count,
                   SUM((SELECT COUNT(*) FROM matches WHERE matches.tie_id = ties.id))
                       AS match_count
            FROM ties
            JOIN stages ON stages.id = ties.stage_id
            WHERE stages.stage_key = ?
            """,
            (round_key,),
        ).fetchone()
    assert (int(row["tie_count"]), int(row["match_count"])) == (
        capacity,
        capacity * 2,
    )


def test_independent_ucl_second_final_is_api_conflict_without_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "local-final.db"
    initialize_database(database_path)
    contest, teams = _create_independent_ucl(database_path, team_count=4)
    create_match(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=OWNER_ID,
        first_name="Owner",
        last_name=None,
        username=None,
        home_team_id=teams[0].id,
        away_team_id=teams[1].id,
        starts_at_utc="2099-05-25T18:00:00Z",
        idempotency_key="final-1",
        audit_actor=ACTOR,
        round_key="final",
    )
    monkeypatch.setattr(tma_api, "load_settings", lambda: _settings(database_path))

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            tma_api.create_tma_match(
                contest.id,
                tma_api.CreateMatchRequest(
                    home_team_id=teams[2].id,
                    away_team_id=teams[3].id,
                    starts_at_utc="2099-05-25T20:00:00Z",
                    round_key="final",
                ),
                _contest_management(),
                "final-2",
            )
        )

    assert error.value.status_code == 409
    assert isinstance(error.value.__cause__, MatchCreationConflictError)
    with create_connection(database_path) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM ties
                 JOIN stages ON stages.id = ties.stage_id
                 WHERE stages.stage_key = 'final') AS tie_count,
                (SELECT COUNT(*) FROM matches
                 JOIN stages ON stages.id = matches.stage_id
                 WHERE stages.stage_key = 'final') AS match_count,
                (SELECT COUNT(*) FROM match_creation_requests) AS request_count
            """
        ).fetchone()
    assert tuple(counts) == (1, 1, 1)


def test_shared_final_position_is_reserved_before_insert_and_attach_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "shared-final.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path, team_count=6)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    monkeypatch.setattr(tma_api, "load_settings", lambda: _settings(database_path))
    management = _shared_management()

    response = asyncio.run(
        tma_api.create_tma_shared_match(
            shared.tournament.id,
            tma_api.CreateSharedMatchRequest(
                home_team_id=shared.teams[0].id,
                away_team_id=shared.teams[1].id,
                starts_at_utc="2099-05-25T18:00:00Z",
                round_key="final",
                bracket_position=1,
            ),
            management,
        )
    )
    assert response["match"]["bracket_position"] == 1

    for invalid_position in (1, 2):
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                tma_api.create_tma_shared_match(
                    shared.tournament.id,
                    tma_api.CreateSharedMatchRequest(
                        home_team_id=shared.teams[2].id,
                        away_team_id=shared.teams[3].id,
                        starts_at_utc="2099-05-25T20:00:00Z",
                        round_key="final",
                        bracket_position=invalid_position,
                    ),
                    management,
                )
            )
        assert error.value.status_code == 409

    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_matches WHERE shared_tournament_id = ?",
                (shared.tournament.id,),
            ).fetchone()[0]
            == 1
        )

    rollback_shared = _create_shared_ucl(database_path, team_count=4, suffix="B")
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=rollback_shared.tournament.id,
    )

    def conflict_during_attach(**_kwargs):
        return SimpleNamespace(
            action="conflict",
            node=SimpleNamespace(
                sync_error="Имитированный конфликт сетки.",
            ),
        )

    monkeypatch.setattr(
        tma_api,
        "configure_bracket_node",
        conflict_during_attach,
    )
    with pytest.raises(HTTPException) as rollback_error:
        asyncio.run(
            tma_api.create_tma_shared_match(
                rollback_shared.tournament.id,
                tma_api.CreateSharedMatchRequest(
                    home_team_id=rollback_shared.teams[0].id,
                    away_team_id=rollback_shared.teams[1].id,
                    starts_at_utc="2099-05-25T18:00:00Z",
                    round_key="final",
                    bracket_position=1,
                ),
                management,
            )
        )
    assert rollback_error.value.status_code == 409
    with create_connection(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_matches WHERE shared_tournament_id = ?",
                (rollback_shared.tournament.id,),
            ).fetchone()[0]
            == 0
        )


def test_shared_ucl_lifecycle_requires_completed_canonical_final_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared-ucl-lifecycle.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path, team_count=3)
    tournament_id = shared.tournament.id
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    shared = save_shared_champion_settings(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        enabled=True,
        deadline_at="2030-05-01T12:00:00Z",
        points=5,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username=None,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )

    with pytest.raises(
        SharedTournamentResultUnavailableError,
        match="материализации и завершения финала",
    ):
        save_shared_champion_result(
            database_path=database_path,
            shared_tournament_id=tournament_id,
            champion_team_id=shared.teams[0].id,
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Owner",
            actor_last_name=None,
            actor_username=None,
            now_utc=_time("2031-01-01T00:00:00Z"),
        )
    with pytest.raises(
        SharedTournamentCompletionUnavailableError,
        match="материализации и завершения финала",
    ):
        archive_shared_tournament(
            database_path=database_path,
            shared_tournament_id=tournament_id,
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2031-01-01T00:00:00Z"),
        )

    final_node = _bracket_node(database_path, tournament_id, "final", 1)
    configured_final = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        round_key="final",
        bracket_position=1,
        first_source_node_id=None,
        second_source_node_id=None,
        resolved_first_team_id=shared.teams[0].id,
        resolved_second_team_id=shared.teams[1].id,
        first_leg_starts_at_utc="2030-06-01T18:00:00Z",
        second_leg_starts_at_utc=None,
        expected_version=final_node.version,
    ).node
    final_match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        home_team_id=shared.teams[0].id,
        away_team_id=shared.teams[1].id,
        starts_at_utc="2030-06-01T18:00:00Z",
        best_of=None,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
        round_key="final",
        bracket_position=1,
    )
    mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        node_id=configured_final.id,
        expected_version=configured_final.version,
        shared_match_id=final_match.id,
    )
    final_match = save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        shared_match_id=final_match.id,
        home_score=1,
        away_score=1,
        advancing_team_id=shared.teams[0].id,
        expected_version=final_match.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username=None,
        now_utc=_time("2031-01-01T00:00:00Z"),
    )
    assert final_match.status == "finished"

    shared = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    with pytest.raises(
        SharedTournamentResultUnavailableError,
        match="совпадать с победителем финала",
    ):
        save_shared_champion_result(
            database_path=database_path,
            shared_tournament_id=tournament_id,
            champion_team_id=shared.teams[1].id,
            expected_version=shared.tournament.version,
            actor_telegram_user_id=OWNER_ID,
            actor_first_name="Owner",
            actor_last_name=None,
            actor_username=None,
            now_utc=_time("2031-01-01T00:00:00Z"),
        )
    shared = save_shared_champion_result(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        champion_team_id=shared.teams[0].id,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        actor_first_name="Owner",
        actor_last_name=None,
        actor_username=None,
        now_utc=_time("2031-01-01T00:00:00Z"),
    )
    assert shared.champion_prediction.actual_champion == shared.teams[0]

    archived = archive_shared_tournament(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        expected_version=shared.tournament.version,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2031-01-01T00:00:00Z"),
    )
    assert archived.tournament.is_archived is True


def test_manual_shared_tie_result_and_correction_propagate_to_next_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "manual-propagation.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path)
    tournament_id = shared.tournament.id
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    )
    source = _bracket_node(database_path, tournament_id, "playoff", 1)
    source = configure_bracket_node(
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
        expected_version=source.version,
    ).node
    tie = create_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=tournament_id,
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
        shared_tournament_id=tournament_id,
        node_id=source.id,
        expected_version=source.version,
        shared_tie_id=tie.id,
    )
    downstream = _bracket_node(database_path, tournament_id, "round_of_16", 1)
    configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=tournament_id,
        round_key="round_of_16",
        bracket_position=1,
        first_source_node_id=source.id,
        second_source_node_id=None,
        resolved_first_team_id=None,
        resolved_second_team_id=shared.teams[2].id,
        first_leg_starts_at_utc="2030-03-01T18:00:00Z",
        second_leg_starts_at_utc="2030-03-08T18:00:00Z",
        expected_version=downstream.version,
    )
    monkeypatch.setattr(tma_api, "load_settings", lambda: _settings(database_path))
    monkeypatch.setattr(
        tma_api,
        "_utc_now",
        lambda: _time("2031-01-01T00:00:00Z"),
    )
    management = _shared_management()

    for leg in (tie.first_leg, tie.second_leg):
        asyncio.run(
            tma_api.save_tma_shared_match_result(
                tournament_id,
                leg.id,
                tma_api.SaveSharedMatchResultRequest(
                    home_score=0,
                    away_score=0,
                    expected_version=leg.version,
                ),
                management,
            )
        )
    tie = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]

    first_result = asyncio.run(
        tma_api.save_tma_shared_two_legged_tie_result(
            tournament_id,
            tie.id,
            tma_api.SaveSharedTwoLeggedTieResultRequest(
                advancing_team_id=shared.teams[0].id,
                second_leg_extra_time_home_score=0,
                second_leg_extra_time_away_score=0,
                second_leg_home_penalty_score=4,
                second_leg_away_penalty_score=5,
                expected_version=tie.version,
            ),
            management,
        )
    )
    assert first_result["bracket_reconciliation"]["state"] == "updated"
    assert (
        _bracket_node(
            database_path, tournament_id, "round_of_16", 1
        ).resolved_first_team_id
        == shared.teams[0].id
    )

    tie = get_shared_tournament_details(
        database_path=database_path,
        shared_tournament_id=tournament_id,
    ).two_legged_ties[0]
    corrected = asyncio.run(
        tma_api.save_tma_shared_two_legged_tie_result(
            tournament_id,
            tie.id,
            tma_api.SaveSharedTwoLeggedTieResultRequest(
                advancing_team_id=shared.teams[1].id,
                second_leg_extra_time_home_score=0,
                second_leg_extra_time_away_score=0,
                second_leg_home_penalty_score=5,
                second_leg_away_penalty_score=4,
                expected_version=tie.version,
            ),
            management,
        )
    )
    assert corrected["bracket_reconciliation"]["state"] == "updated"
    assert (
        _bracket_node(
            database_path, tournament_id, "round_of_16", 1
        ).resolved_first_team_id
        == shared.teams[1].id
    )


def test_independent_ucl_serializes_empty_canonical_23_node_grid(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "independent-empty.db"
    initialize_database(database_path)
    contest, _ = _create_independent_ucl(database_path)
    details = get_contest_details(
        database_path=database_path,
        telegram_chat_id=CHAT_ID,
        contest_id=contest.id,
        telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )

    payload = tma_api._serialize_contest_details(
        details,
        database_path=database_path,
    )
    bracket = payload["playoff_bracket"]

    assert bracket["mode"] == "manual"
    assert [len(round_payload["nodes"]) for round_payload in bracket["rounds"]] == [
        8,
        8,
        4,
        2,
        1,
    ]
    nodes = [
        node for round_payload in bracket["rounds"] for node in round_payload["nodes"]
    ]
    assert len(nodes) == 23
    assert all(node["entity"] is None for node in nodes)
    assert len({node["id"] for node in nodes}) == 23


def test_fixture_sync_payload_exposes_sanitized_ledger_conflict_without_payload(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "sync-conflict.db"
    initialize_database(database_path)
    shared = _create_shared_ucl(database_path)
    ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
    )
    configure_external_source(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source="football-data.org",
        external_event_id="CL:2026",
        sync_enabled=True,
        expected_tournament_version=shared.tournament.version,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )
    fixture = record_fixture_seen(
        database_path=database_path,
        shared_tournament_id=shared.tournament.id,
        source="football-data.org",
        external_event_id="CL:2026",
        external_fixture_id="fixture-77",
        round_key="playoff",
        bracket_position=1,
        leg_number=1,
        payload='{"raw":"do-not-return","token":"secret-token"}',
    )
    mark_fixture_conflict(
        database_path=database_path,
        fixture_import_id=fixture.id,
        error="Не сопоставлена команда Exact FC (external_team_id=77).",
        expected_version=fixture.version,
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
    )
    sync = payload["fixture_sync"]

    assert sync["state"] == "needs_attention"
    assert sync["conflict_detail_count"] == 1
    assert sync["conflict_details"] == [
        {
            "kind": "fixture",
            "round_key": "playoff",
            "position": 1,
            "fixture_id": "fixture-77",
            "leg_number": 1,
            "status": "conflict",
            "message": "Не сопоставлена команда Exact FC (external_team_id=77).",
        }
    ]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "do-not-return" not in serialized
    assert "secret-token" not in serialized


def test_ucl_ui_distinguishes_final_winner_and_renders_conflict_details() -> None:
    source = Path("tma/app.js").read_text(encoding="utf-8")

    assert 'text: isFinal ? "Кто победит?"' in source
    assert 'isFinal ? "Победитель" : "Прошла дальше"' in source
    assert "sync.conflict_details" in source
    assert "fixture-sync-conflict-list" in source
    assert "fixture-sync-disable-hint" in source
    assert "текущая пара или финал" in source
    assert "Следующая пара или финал уже не начнёт" in source
    assert "nodeCount: 8" in source
    assert "nodeCount: 1" in source

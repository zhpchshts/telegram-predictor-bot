from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

from app.audit_service import AuditActor, AuditActorRole
from app.contest_service import create_the_international_2026_contest
from app.database import create_connection, initialize_database
from app.shared_tournament_service import (
    SharedMatchExternalResolution,
    create_shared_match,
    create_shared_tournament,
    resolve_shared_match_external_link,
    save_shared_tournament_teams,
)


OWNER_ID = 123


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _create_tournament(database_path: Path):
    details = create_shared_tournament(
        database_path=database_path,
        name="Исторический турнир TI 2026",
        template_key="the_international_2026",
        actor_telegram_user_id=OWNER_ID,
    )
    return save_shared_tournament_teams(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        team_names=["Team Liquid", "Team Falcons"],
        expected_version=details.tournament.version,
        actor_telegram_user_id=OWNER_ID,
    )


def _create_linked_contest(database_path: Path, *, shared_tournament_id: int) -> int:
    telegram_chat_id = -1001234567890
    return create_the_international_2026_contest(
        database_path=database_path,
        telegram_chat_id=telegram_chat_id,
        chat_title="Исторические прогнозы",
        telegram_user_id=OWNER_ID,
        first_name="Eugene",
        last_name="Sabir",
        username="evsab",
        contest_name="TI 2026",
        idempotency_key="historical-ti-contest",
        audit_actor=AuditActor(
            telegram_chat_id=telegram_chat_id,
            telegram_user_id=OWNER_ID,
            role=AuditActorRole.TELEGRAM_ADMIN,
        ),
        shared_tournament_id=shared_tournament_id,
    ).contest.id


def _resolve_concurrently(
    *,
    database_path: Path,
    shared_tournament_id: int,
    home_team_id: int,
    away_team_id: int,
    external_match_ids: tuple[str, str],
) -> tuple[SharedMatchExternalResolution, SharedMatchExternalResolution]:
    barrier = Barrier(2)

    def resolve(external_match_id: str) -> SharedMatchExternalResolution:
        barrier.wait(timeout=5)
        return resolve_shared_match_external_link(
            database_path=database_path,
            shared_tournament_id=shared_tournament_id,
            source="test-provider",
            external_event_id="event-1",
            external_match_id=external_match_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            external_starts_at_utc="2030-01-02T12:00:00Z",
            create_starts_at_utc="2030-01-02T12:00:00Z",
            best_of=3,
            actor_telegram_user_id=OWNER_ID,
            now_utc=_time("2029-01-01T00:00:00Z"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(resolve, external_match_id)
            for external_match_id in external_match_ids
        ]
        results = tuple(future.result(timeout=10) for future in futures)
    return results[0], results[1]


def test_concurrent_external_resolution_creates_one_match_for_one_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "same-external-match.db"
    initialize_database(database_path)
    details = _create_tournament(database_path)
    contest_id = _create_linked_contest(
        database_path,
        shared_tournament_id=details.tournament.id,
    )
    teams = {team.name: team.id for team in details.teams}

    results = _resolve_concurrently(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        home_team_id=teams["Team Liquid"],
        away_team_id=teams["Team Falcons"],
        external_match_ids=("match-1", "match-1"),
    )

    assert sum(result.was_created for result in results) == 1
    assert all(result.match is not None for result in results)
    assert results[0].match.id == results[1].match.id
    with create_connection(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_matches").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_match_external_links"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_match_links WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()[0]
            == 1
        )


def test_different_external_identities_adopt_an_unlinked_match_only_once(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "different-external-matches.db"
    initialize_database(database_path)
    details = _create_tournament(database_path)
    contest_id = _create_linked_contest(
        database_path,
        shared_tournament_id=details.tournament.id,
    )
    teams = {team.name: team.id for team in details.teams}
    unlinked_match = create_shared_match(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        home_team_id=teams["Team Liquid"],
        away_team_id=teams["Team Falcons"],
        starts_at_utc="2030-01-02T12:00:00Z",
        best_of=3,
        actor_telegram_user_id=OWNER_ID,
        now_utc=_time("2029-01-01T00:00:00Z"),
    )

    results = _resolve_concurrently(
        database_path=database_path,
        shared_tournament_id=details.tournament.id,
        home_team_id=teams["Team Liquid"],
        away_team_id=teams["Team Falcons"],
        external_match_ids=("match-1", "match-2"),
    )

    assert sum(result.was_created for result in results) == 1
    assert all(result.match is not None for result in results)
    resolved_match_ids = {result.match.id for result in results}
    assert unlinked_match.id in resolved_match_ids
    assert len(resolved_match_ids) == 2
    with create_connection(database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM shared_matches").fetchone()[0] == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_match_external_links"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM shared_match_links WHERE contest_id = ?",
                (contest_id,),
            ).fetchone()[0]
            == 2
        )

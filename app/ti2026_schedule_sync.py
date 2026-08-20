from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from app.database import database_connection
from app.shared_tournament_service import (
    SharedMatch,
    SharedMatchConflictError,
    SharedMatchResultUnavailableError,
    SharedMatchUpdateUnavailableError,
    create_shared_match,
    get_shared_tournament_details,
    save_shared_match_result,
    update_shared_match_start,
)


logger = logging.getLogger(__name__)

VALVE_SOURCE = "valve"
VALVE_EVENT_ID = "19719"
VALVE_LEAGUE_ID = 19719
VALVE_LEAGUE_NAME = "The International 2026"
VALVE_SCHEDULE_URL = (
    "https://www.dota2.com/webapi/IDOTA2League/GetLeagueData/v001"
    "?league_id=19719&delay_seconds=0"
)
SYNC_INTERVAL_SECONDS = 10 * 60
ROLLING_DEADLINE_BUFFER = timedelta(minutes=20)

# Valve team IDs are stable within the official league feed. Names match the
# teams already configured in the shared TI2026 tournament.
VALVE_TEAM_NAMES = {
    2163: "Team Liquid",
    726228: "Vici Gaming",
    2586976: "OG",
    5017210: "Team Resilience",
    7119388: "Team Spirit",
    8255888: "BoomBoys",
    8261500: "Xtreme Gaming",
    9247354: "Team Falcons",
    9467224: "Aurora Gaming",
    9572001: "Team Vision",
    9823272: "Team Yandex",
    9964962: "GamerLegion",
    10136357: "Nigma Galaxy",
    10149530: "HULIGANI",
    10150413: "Iron Wing",
    10150538: "LGD Gaming",
}


@dataclass(frozen=True, slots=True)
class ValveSeries:
    node_id: int
    team_id_1: int
    team_id_2: int
    scheduled_at: datetime
    best_of: int
    has_started: bool
    is_completed: bool
    team_1_wins: int
    team_2_wins: int

    @property
    def is_resolved(self) -> bool:
        return self.team_id_1 > 0 and self.team_id_2 > 0


@dataclass(frozen=True, slots=True)
class SyncResult:
    tournaments: int = 0
    linked: int = 0
    created: int = 0
    deadlines_moved: int = 0
    results_saved: int = 0
    conflicts: int = 0

    def add(self, **changes: int) -> SyncResult:
        values = {
            "tournaments": self.tournaments,
            "linked": self.linked,
            "created": self.created,
            "deadlines_moved": self.deadlines_moved,
            "results_saved": self.results_saved,
            "conflicts": self.conflicts,
        }
        for name, amount in changes.items():
            values[name] += amount
        return SyncResult(**values)


@dataclass(frozen=True, slots=True)
class _TournamentActor:
    tournament_id: int
    telegram_user_id: int
    first_name: str
    last_name: str | None
    username: str | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _require_dict(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Valve response field {field!r} is not an object.")
    return value


def _find_playoff_group(groups: Any) -> dict[str, Any]:
    if not isinstance(groups, list):
        raise ValueError("Valve response does not contain node_groups.")
    for raw_group in groups:
        group = _require_dict(raw_group, field="node_group")
        if group.get("name") == "Playoff":
            return group
        children = group.get("node_groups", [])
        if children:
            try:
                return _find_playoff_group(children)
            except LookupError:
                pass
    raise LookupError("Valve response does not contain the Playoff node group.")


def parse_valve_schedule(payload: Any) -> tuple[ValveSeries, ...]:
    root = _require_dict(payload, field="root")
    info = _require_dict(root.get("info"), field="info")
    if int(info.get("league_id", 0)) != VALVE_LEAGUE_ID:
        raise ValueError("Valve response belongs to a different league.")
    if str(info.get("name", "")) != VALVE_LEAGUE_NAME:
        raise ValueError("Valve response has an unexpected league name.")

    playoff = _find_playoff_group(root.get("node_groups"))
    raw_nodes = playoff.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("Valve Playoff node group is empty.")

    series: list[ValveSeries] = []
    for raw_node in raw_nodes:
        node = _require_dict(raw_node, field="playoff node")
        node_type = int(node.get("node_type", 0))
        best_of = {2: 3, 3: 5}.get(node_type)
        if best_of is None:
            continue
        scheduled_timestamp = int(node.get("scheduled_time", 0))
        if scheduled_timestamp <= 0:
            raise ValueError("Valve playoff node has no scheduled_time.")
        series.append(
            ValveSeries(
                node_id=int(node["node_id"]),
                team_id_1=int(node.get("team_id_1", 0)),
                team_id_2=int(node.get("team_id_2", 0)),
                scheduled_at=datetime.fromtimestamp(
                    scheduled_timestamp, tz=timezone.utc
                ),
                best_of=best_of,
                has_started=bool(node.get("has_started", False)),
                is_completed=bool(node.get("is_completed", False)),
                team_1_wins=int(node.get("team_1_wins", 0)),
                team_2_wins=int(node.get("team_2_wins", 0)),
            )
        )
    if not series:
        raise ValueError("Valve Playoff node group has no supported series.")
    return tuple(sorted(series, key=lambda item: (item.scheduled_at, item.node_id)))


def _download_valve_schedule() -> Any:
    request = Request(
        VALVE_SCHEDULE_URL,
        headers={
            "Accept": "application/json",
            "Referer": "https://www.dota2.com/esports/ti15/schedule",
            "User-Agent": "Klever-TI2026-Sync/1.0",
        },
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError(f"Valve schedule returned HTTP {response.status}.")
        return json.load(response)


async def fetch_valve_schedule() -> tuple[ValveSeries, ...]:
    payload = await asyncio.to_thread(_download_valve_schedule)
    return parse_valve_schedule(payload)


def _load_tournament_actors(database_path: Path) -> tuple[_TournamentActor, ...]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                tournament.id,
                tournament.created_by_telegram_user_id,
                users.first_name,
                users.last_name,
                users.username
            FROM shared_tournaments AS tournament
            LEFT JOIN users
              ON users.telegram_user_id = tournament.created_by_telegram_user_id
            WHERE tournament.template_key = 'the_international_2026'
              AND tournament.is_archived = 0
            ORDER BY tournament.id
            """
        ).fetchall()
    return tuple(
        _TournamentActor(
            tournament_id=int(row["id"]),
            telegram_user_id=int(row["created_by_telegram_user_id"]),
            first_name=(
                str(row["first_name"])
                if row["first_name"] is not None
                else "TI2026 sync"
            ),
            last_name=(str(row["last_name"]) if row["last_name"] is not None else None),
            username=(str(row["username"]) if row["username"] is not None else None),
        )
        for row in rows
    )


def _load_external_match_id(
    database_path: Path, *, tournament_id: int, node_id: int
) -> int | None:
    with database_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT shared_match_id
            FROM shared_match_external_links
            WHERE shared_tournament_id = ?
              AND source = ?
              AND external_event_id = ?
              AND external_match_id = ?
            """,
            (tournament_id, VALVE_SOURCE, VALVE_EVENT_ID, str(node_id)),
        ).fetchone()
    return int(row["shared_match_id"]) if row is not None else None


def _link_external_match(
    database_path: Path,
    *,
    tournament_id: int,
    shared_match_id: int,
    node_id: int,
) -> None:
    with database_connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO shared_match_external_links (
                shared_match_id, shared_tournament_id, source,
                external_event_id, external_match_id
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (shared_tournament_id, source, external_event_id,
                         external_match_id)
            DO NOTHING
            """,
            (
                shared_match_id,
                tournament_id,
                VALVE_SOURCE,
                VALVE_EVENT_ID,
                str(node_id),
            ),
        )


def _load_linked_match_ids(database_path: Path, *, tournament_id: int) -> set[int]:
    with database_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT shared_match_id
            FROM shared_match_external_links
            WHERE shared_tournament_id = ? AND source = ?
              AND external_event_id = ?
            """,
            (tournament_id, VALVE_SOURCE, VALVE_EVENT_ID),
        ).fetchall()
    return {int(row["shared_match_id"]) for row in rows}


def _find_unlinked_pair_match(
    matches: tuple[SharedMatch, ...],
    *,
    linked_match_ids: set[int],
    team_name_1: str,
    team_name_2: str,
    scheduled_at: datetime,
) -> SharedMatch | None:
    expected_pair = frozenset((team_name_1, team_name_2))
    candidates = [
        match
        for match in matches
        if match.id not in linked_match_ids
        and frozenset((match.home_team.name, match.away_team.name)) == expected_pair
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda match: abs(
            (_parse_datetime(match.starts_at_utc) - scheduled_at).total_seconds()
        ),
    )


def _same_result(
    match: SharedMatch,
    *,
    series: ValveSeries,
    team_id_1: int,
    team_id_2: int,
) -> bool:
    if match.home_team.id == team_id_1:
        expected_home_score = series.team_1_wins
        expected_away_score = series.team_2_wins
        expected_winner = (
            team_id_1 if series.team_1_wins > series.team_2_wins else team_id_2
        )
    else:
        expected_home_score = series.team_2_wins
        expected_away_score = series.team_1_wins
        expected_winner = (
            team_id_2 if series.team_2_wins > series.team_1_wins else team_id_1
        )
    return (
        match.home_score == expected_home_score
        and match.away_score == expected_away_score
        and match.advancing_team_id == expected_winner
    )


def _validate_completed_score(series: ValveSeries) -> None:
    required_wins = series.best_of // 2 + 1
    high_score = max(series.team_1_wins, series.team_2_wins)
    low_score = min(series.team_1_wins, series.team_2_wins)
    if high_score != required_wins or low_score < 0 or low_score >= high_score:
        raise ValueError(
            f"Valve node {series.node_id} has an invalid completed score "
            f"{series.team_1_wins}:{series.team_2_wins}."
        )


def _save_result(
    database_path: Path,
    *,
    actor: _TournamentActor,
    match: SharedMatch,
    series: ValveSeries,
    now_utc: datetime,
) -> SharedMatch:
    if match.home_team.name == VALVE_TEAM_NAMES[series.team_id_1]:
        home_score = series.team_1_wins
        away_score = series.team_2_wins
        winner_id = (
            match.home_team.id
            if series.team_1_wins > series.team_2_wins
            else match.away_team.id
        )
    else:
        home_score = series.team_2_wins
        away_score = series.team_1_wins
        winner_id = (
            match.home_team.id
            if series.team_2_wins > series.team_1_wins
            else match.away_team.id
        )
    return save_shared_match_result(
        database_path=database_path,
        shared_tournament_id=actor.tournament_id,
        shared_match_id=match.id,
        home_score=home_score,
        away_score=away_score,
        advancing_team_id=winner_id,
        expected_version=match.version,
        actor_telegram_user_id=actor.telegram_user_id,
        actor_first_name=actor.first_name,
        actor_last_name=actor.last_name,
        actor_username=actor.username,
        now_utc=now_utc,
        trusted_result_source="Valve TI2026 league feed",
    )


def synchronize_ti2026_schedule(
    *,
    database_path: Path,
    series: tuple[ValveSeries, ...],
    now_utc: datetime | None = None,
) -> SyncResult:
    now = (now_utc or _utc_now()).astimezone(timezone.utc)
    result = SyncResult()
    actors = _load_tournament_actors(database_path)
    for actor in actors:
        result = result.add(tournaments=1)
        details = get_shared_tournament_details(
            database_path=database_path,
            shared_tournament_id=actor.tournament_id,
        )
        team_ids_by_name = {team.name: team.id for team in details.teams}
        matches = details.matches
        linked_match_ids = _load_linked_match_ids(
            database_path, tournament_id=actor.tournament_id
        )

        previous: ValveSeries | None = None
        for valve_match in series:
            previous_same_day = (
                previous
                if previous is not None
                and previous.scheduled_at.date() == valve_match.scheduled_at.date()
                else None
            )
            previous = valve_match
            if not valve_match.is_resolved:
                continue

            team_name_1 = VALVE_TEAM_NAMES.get(valve_match.team_id_1)
            team_name_2 = VALVE_TEAM_NAMES.get(valve_match.team_id_2)
            if team_name_1 is None or team_name_2 is None:
                logger.error(
                    "TI2026 Valve node %s contains unknown team IDs %s and %s.",
                    valve_match.node_id,
                    valve_match.team_id_1,
                    valve_match.team_id_2,
                )
                result = result.add(conflicts=1)
                continue
            team_id_1 = team_ids_by_name.get(team_name_1)
            team_id_2 = team_ids_by_name.get(team_name_2)
            if team_id_1 is None or team_id_2 is None:
                logger.error(
                    "TI2026 shared tournament %s is missing team %s or %s.",
                    actor.tournament_id,
                    team_name_1,
                    team_name_2,
                )
                result = result.add(conflicts=1)
                continue

            external_match_id = _load_external_match_id(
                database_path,
                tournament_id=actor.tournament_id,
                node_id=valve_match.node_id,
            )
            match = next(
                (item for item in matches if item.id == external_match_id), None
            )
            if match is None and external_match_id is not None:
                logger.error(
                    "TI2026 external node %s points to missing shared match %s.",
                    valve_match.node_id,
                    external_match_id,
                )
                result = result.add(conflicts=1)
                continue
            if match is None:
                match = _find_unlinked_pair_match(
                    matches,
                    linked_match_ids=linked_match_ids,
                    team_name_1=team_name_1,
                    team_name_2=team_name_2,
                    scheduled_at=valve_match.scheduled_at,
                )
                if match is not None:
                    _link_external_match(
                        database_path,
                        tournament_id=actor.tournament_id,
                        shared_match_id=match.id,
                        node_id=valve_match.node_id,
                    )
                    linked_match_ids.add(match.id)
                    result = result.add(linked=1)

            if match is None:
                if valve_match.has_started or valve_match.is_completed:
                    logger.warning(
                        "TI2026 Valve node %s is already active or completed; "
                        "a missing shared match will not be created retroactively.",
                        valve_match.node_id,
                    )
                    result = result.add(conflicts=1)
                    continue
                initial_start = valve_match.scheduled_at
                if previous_same_day is not None and not previous_same_day.is_completed:
                    initial_start = max(initial_start, now + ROLLING_DEADLINE_BUFFER)
                if initial_start <= now:
                    initial_start = now + ROLLING_DEADLINE_BUFFER
                try:
                    match = create_shared_match(
                        database_path=database_path,
                        shared_tournament_id=actor.tournament_id,
                        home_team_id=team_id_1,
                        away_team_id=team_id_2,
                        starts_at_utc=_format_datetime(initial_start),
                        best_of=valve_match.best_of,
                        actor_telegram_user_id=actor.telegram_user_id,
                        now_utc=now,
                        allow_duplicate_pair=True,
                    )
                except (SharedMatchConflictError, ValueError) as error:
                    logger.warning(
                        "Could not create TI2026 Valve node %s: %s",
                        valve_match.node_id,
                        error,
                    )
                    result = result.add(conflicts=1)
                    continue
                _link_external_match(
                    database_path,
                    tournament_id=actor.tournament_id,
                    shared_match_id=match.id,
                    node_id=valve_match.node_id,
                )
                linked_match_ids.add(match.id)
                matches = (*matches, match)
                result = result.add(created=1)

            if match.status == "finished":
                if valve_match.is_completed:
                    _validate_completed_score(valve_match)
                    if not _same_result(
                        match,
                        series=valve_match,
                        team_id_1=team_id_1,
                        team_id_2=team_id_2,
                    ):
                        logger.error(
                            "TI2026 result conflict for shared match %s / Valve "
                            "node %s; the completed shared match was not changed.",
                            match.id,
                            valve_match.node_id,
                        )
                        result = result.add(conflicts=1)
                continue

            if valve_match.is_completed:
                try:
                    _validate_completed_score(valve_match)
                    match = _save_result(
                        database_path,
                        actor=actor,
                        match=match,
                        series=valve_match,
                        now_utc=now,
                    )
                except (ValueError, SharedMatchResultUnavailableError) as error:
                    logger.warning(
                        "Could not save result for TI2026 Valve node %s: %s",
                        valve_match.node_id,
                        error,
                    )
                    result = result.add(conflicts=1)
                else:
                    result = result.add(results_saved=1)
                continue

            if match.status != "scheduled" or valve_match.has_started:
                continue
            desired_start = max(
                _parse_datetime(match.starts_at_utc), valve_match.scheduled_at
            )
            if previous_same_day is not None and not previous_same_day.is_completed:
                desired_start = max(desired_start, now + ROLLING_DEADLINE_BUFFER)
            if desired_start <= _parse_datetime(match.starts_at_utc):
                continue
            try:
                match = update_shared_match_start(
                    database_path=database_path,
                    shared_tournament_id=actor.tournament_id,
                    shared_match_id=match.id,
                    starts_at_utc=_format_datetime(desired_start),
                    expected_version=match.version,
                    actor_telegram_user_id=actor.telegram_user_id,
                    now_utc=now,
                )
            except (
                SharedMatchConflictError,
                SharedMatchUpdateUnavailableError,
            ) as error:
                logger.warning(
                    "Could not move deadline for TI2026 Valve node %s: %s",
                    valve_match.node_id,
                    error,
                )
                result = result.add(conflicts=1)
            else:
                result = result.add(deadlines_moved=1)
    return result


async def run_ti2026_schedule_sync_worker(*, database_path: Path) -> None:
    while True:
        try:
            series = await fetch_valve_schedule()
            result = await asyncio.to_thread(
                synchronize_ti2026_schedule,
                database_path=database_path,
                series=series,
            )
            logger.info(
                "TI2026 schedule sync completed: tournaments=%s linked=%s "
                "created=%s deadlines_moved=%s results_saved=%s conflicts=%s.",
                result.tournaments,
                result.linked,
                result.created,
                result.deadlines_moved,
                result.results_saved,
                result.conflicts,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TI2026 schedule sync failed; the worker will retry.")
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)

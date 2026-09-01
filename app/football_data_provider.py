from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


FOOTBALL_DATA_SOURCE = "football-data.org"
FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"
CHAMPIONS_LEAGUE_COMPETITION_ID = 2001
CHAMPIONS_LEAGUE_COMPETITION_CODE = "CL"
CHAMPIONS_LEAGUE_TEMPLATE_SEASON_START_YEAR = 2026
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

ROUND_KNOCKOUT_PLAYOFFS = "playoff"
ROUND_OF_16 = "round_of_16"
ROUND_QUARTER_FINALS = "quarterfinal"
ROUND_SEMI_FINALS = "semifinal"
ROUND_FINAL = "final"

TWO_LEGGED_ROUNDS = frozenset(
    {
        ROUND_KNOCKOUT_PLAYOFFS,
        ROUND_OF_16,
        ROUND_QUARTER_FINALS,
        ROUND_SEMI_FINALS,
    }
)
SUPPORTED_ROUNDS = (*TWO_LEGGED_ROUNDS, ROUND_FINAL)

_STAGE_TO_ROUND = {
    "PLAYOFFS": ROUND_KNOCKOUT_PLAYOFFS,
    "LAST_16": ROUND_OF_16,
    "QUARTER_FINALS": ROUND_QUARTER_FINALS,
    "SEMI_FINALS": ROUND_SEMI_FINALS,
    "FINAL": ROUND_FINAL,
}
_KNOWN_NON_KNOCKOUT_STAGES = frozenset(
    {
        "LEAGUE_STAGE",
        "REGULAR_SEASON",
        "GROUP_STAGE",
        "PRELIMINARY_ROUND",
        "QUALIFICATION",
        "QUALIFICATION_ROUND_1",
        "QUALIFICATION_ROUND_2",
        "QUALIFICATION_ROUND_3",
        "PLAYOFF_ROUND_1",
        "PLAYOFF_ROUND_2",
    }
)
_SUPPORTED_STATUSES = frozenset(
    {
        "SCHEDULED",
        "TIMED",
        "IN_PLAY",
        "PAUSED",
        "EXTRA_TIME",
        "PENALTY_SHOOTOUT",
        "FINISHED",
        "SUSPENDED",
        "POSTPONED",
        "CANCELLED",
        "AWARDED",
    }
)


class FootballDataError(RuntimeError):
    """Base error for a failed football-data.org request or response."""


class FootballDataRequestError(FootballDataError):
    """The provider request failed before a usable response was received."""


class FootballDataResponseError(FootballDataError):
    """The provider returned a malformed top-level response."""


@dataclass(frozen=True, slots=True)
class ProviderConflict:
    code: str
    message: str
    external_match_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalTeam:
    external_team_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ExternalMatchScore:
    regular_home: int
    regular_away: int
    extra_time_home: int | None
    extra_time_away: int | None
    penalty_home: int | None
    penalty_away: int | None
    winner_external_team_id: str | None
    duration: str


@dataclass(frozen=True, slots=True)
class ExternalKnockoutMatch:
    external_match_id: str
    external_event_id: str
    round_key: str
    provider_stage: str
    home_team: ExternalTeam
    away_team: ExternalTeam
    starts_at_utc: str
    status: str
    score: ExternalMatchScore | None
    last_updated_at_utc: str | None


@dataclass(frozen=True, slots=True)
class FootballDataMatchBatch:
    source: str
    external_event_id: str
    season_start_year: int
    matches: tuple[ExternalKnockoutMatch, ...]
    conflicts: tuple[ProviderConflict, ...]
    ignored_match_count: int


@dataclass(frozen=True, slots=True)
class ExternalTwoLeggedTie:
    round_key: str
    first_leg: ExternalKnockoutMatch
    second_leg: ExternalKnockoutMatch

    @property
    def external_team_ids(self) -> frozenset[str]:
        return frozenset(
            {
                self.first_leg.home_team.external_team_id,
                self.first_leg.away_team.external_team_id,
            }
        )


@dataclass(frozen=True, slots=True)
class FootballDataKnockoutSnapshot:
    source: str
    external_event_id: str
    season_start_year: int
    two_legged_ties: tuple[ExternalTwoLeggedTie, ...]
    final: ExternalKnockoutMatch | None
    pending_matches: tuple[ExternalKnockoutMatch, ...]
    conflicts: tuple[ProviderConflict, ...]
    ignored_match_count: int


JsonFetcher = Callable[
    [str, Mapping[str, str], float],
    Awaitable[object],
]


class FootballDataClient:
    def __init__(
        self,
        *,
        token: str,
        season_start_year: int,
        base_url: str = FOOTBALL_DATA_BASE_URL,
        timeout_seconds: float = 20.0,
        fetch_json: JsonFetcher | None = None,
    ) -> None:
        normalized_token = token.strip()
        if not normalized_token:
            raise ValueError("football-data.org token must not be empty.")
        if season_start_year != CHAMPIONS_LEAGUE_TEMPLATE_SEASON_START_YEAR:
            raise ValueError(
                "season_start_year must be 2026 for the fixed 2026/27 template."
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self._token = normalized_token
        self._season_start_year = season_start_year
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json or _fetch_json

    async def fetch_champions_league_knockout(
        self,
    ) -> FootballDataKnockoutSnapshot:
        query = urlencode({"season": str(self._season_start_year)})
        url = (
            f"{self._base_url}/competitions/"
            f"{CHAMPIONS_LEAGUE_COMPETITION_CODE}/matches?{query}"
        )
        try:
            payload = await self._fetch_json(
                url,
                {
                    "Accept": "application/json",
                    "X-Auth-Token": self._token,
                    "User-Agent": "Klever-Champions-League-Sync/1.0",
                },
                self._timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except FootballDataError:
            raise
        except Exception as error:
            raise FootballDataRequestError(
                f"football-data.org request failed ({type(error).__name__})."
            ) from error
        batch = parse_football_data_matches(
            payload,
            season_start_year=self._season_start_year,
        )
        return group_football_data_knockout(batch)


async def _fetch_json(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> object:
    return await asyncio.to_thread(
        _download_json,
        url,
        headers,
        timeout_seconds,
    )


def _download_json(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> object:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            status = int(getattr(response, "status", 200))
            if status != 200:
                raise FootballDataRequestError(
                    f"football-data.org returned HTTP {status}."
                )
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > MAX_RESPONSE_BYTES:
                raise FootballDataResponseError(
                    "football-data.org response is unexpectedly large."
                )
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except FootballDataError:
        raise
    except Exception as error:
        raise FootballDataRequestError(
            f"football-data.org request failed ({type(error).__name__})."
        ) from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FootballDataResponseError(
            "football-data.org response is unexpectedly large."
        )
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FootballDataResponseError(
            "football-data.org returned invalid JSON."
        ) from error


def parse_football_data_matches(
    payload: object,
    *,
    season_start_year: int,
) -> FootballDataMatchBatch:
    root = _require_mapping(payload, field_name="response")
    competition = _require_mapping(root.get("competition"), field_name="competition")
    if (
        _parse_positive_external_id(competition.get("id"))
        != str(CHAMPIONS_LEAGUE_COMPETITION_ID)
        or competition.get("code") != CHAMPIONS_LEAGUE_COMPETITION_CODE
    ):
        raise FootballDataResponseError(
            "football-data.org response belongs to a different competition."
        )
    raw_matches = root.get("matches")
    if not isinstance(raw_matches, list):
        raise FootballDataResponseError(
            "football-data.org response does not contain a matches list."
        )
    if len(raw_matches) > 1_000:
        raise FootballDataResponseError(
            "football-data.org response contains too many matches."
        )

    external_event_id = f"{CHAMPIONS_LEAGUE_COMPETITION_CODE}:{season_start_year}"
    parsed_by_id: dict[str, ExternalKnockoutMatch] = {}
    conflicting_ids: set[str] = set()
    conflicts: list[ProviderConflict] = []
    ignored_count = 0
    for raw_match in raw_matches:
        try:
            parsed = _parse_match(
                raw_match,
                external_event_id=external_event_id,
                season_start_year=season_start_year,
            )
        except _IgnoredMatch:
            ignored_count += 1
            continue
        except _MatchConflict as error:
            conflicts.append(
                ProviderConflict(
                    code=error.code,
                    message=str(error),
                    external_match_id=error.external_match_id,
                )
            )
            continue
        existing = parsed_by_id.get(parsed.external_match_id)
        if existing is None and parsed.external_match_id not in conflicting_ids:
            parsed_by_id[parsed.external_match_id] = parsed
            continue
        parsed_by_id.pop(parsed.external_match_id, None)
        if parsed.external_match_id not in conflicting_ids:
            conflicts.append(
                ProviderConflict(
                    code="duplicate_external_match",
                    message=(
                        "football-data.org returned a duplicate external match ID."
                    ),
                    external_match_id=parsed.external_match_id,
                )
            )
            conflicting_ids.add(parsed.external_match_id)

    team_names: dict[str, str] = {}
    ambiguous_team_ids: set[str] = set()
    for match in parsed_by_id.values():
        for team in (match.home_team, match.away_team):
            existing_name = team_names.setdefault(team.external_team_id, team.name)
            if existing_name != team.name:
                ambiguous_team_ids.add(team.external_team_id)
    if ambiguous_team_ids:
        for external_team_id in sorted(ambiguous_team_ids):
            conflicts.append(
                ProviderConflict(
                    code="ambiguous_external_team",
                    message=(
                        "football-data.org returned different names for external "
                        f"team {external_team_id}."
                    ),
                )
            )
        parsed_by_id = {
            match_id: match
            for match_id, match in parsed_by_id.items()
            if not (
                {
                    match.home_team.external_team_id,
                    match.away_team.external_team_id,
                }
                & ambiguous_team_ids
            )
        }

    matches = tuple(
        sorted(
            parsed_by_id.values(),
            key=lambda match: (match.starts_at_utc, match.external_match_id),
        )
    )
    return FootballDataMatchBatch(
        source=FOOTBALL_DATA_SOURCE,
        external_event_id=external_event_id,
        season_start_year=season_start_year,
        matches=matches,
        conflicts=tuple(conflicts),
        ignored_match_count=ignored_count,
    )


def group_football_data_knockout(
    batch: FootballDataMatchBatch,
) -> FootballDataKnockoutSnapshot:
    groups: dict[tuple[str, frozenset[str]], list[ExternalKnockoutMatch]] = {}
    finals: list[ExternalKnockoutMatch] = []
    for match in batch.matches:
        if match.round_key == ROUND_FINAL:
            finals.append(match)
            continue
        key = (
            match.round_key,
            frozenset(
                {
                    match.home_team.external_team_id,
                    match.away_team.external_team_id,
                }
            ),
        )
        groups.setdefault(key, []).append(match)

    conflicts = list(batch.conflicts)
    round_capacities = {
        ROUND_KNOCKOUT_PLAYOFFS: 8,
        ROUND_OF_16: 8,
        ROUND_QUARTER_FINALS: 4,
        ROUND_SEMI_FINALS: 2,
    }
    invalid_group_keys: set[tuple[str, frozenset[str]]] = set()
    for round_key, capacity in round_capacities.items():
        round_group_keys = [key for key in groups if key[0] == round_key]
        if len(round_group_keys) > capacity:
            invalid_group_keys.update(round_group_keys)
            conflicts.append(
                ProviderConflict(
                    code="round_capacity_exceeded",
                    message=(
                        f"Round {round_key} contains more than {capacity} "
                        "distinct team pairs."
                    ),
                )
            )
            continue
        group_keys_by_team: dict[str, list[tuple[str, frozenset[str]]]] = {}
        for group_key in round_group_keys:
            for external_team_id in group_key[1]:
                group_keys_by_team.setdefault(external_team_id, []).append(group_key)
        for external_team_id, team_group_keys in group_keys_by_team.items():
            if len(team_group_keys) <= 1:
                continue
            invalid_group_keys.update(team_group_keys)
            conflicts.append(
                ProviderConflict(
                    code="team_repeated_in_round",
                    message=(
                        f"External team {external_team_id} occurs in more than "
                        f"one {round_key} pair."
                    ),
                )
            )
    ties: list[ExternalTwoLeggedTie] = []
    pending: list[ExternalKnockoutMatch] = []
    for group_key, matches in groups.items():
        round_key, _team_ids = group_key
        if group_key in invalid_group_keys:
            continue
        ordered = sorted(
            matches,
            key=lambda match: (match.starts_at_utc, match.external_match_id),
        )
        if len(ordered) == 1:
            pending.append(ordered[0])
            continue
        if len(ordered) != 2:
            conflicts.append(
                ProviderConflict(
                    code="ambiguous_two_legged_tie",
                    message=(
                        f"Round {round_key} contains {len(ordered)} matches for "
                        "one team pair; exactly two are required."
                    ),
                )
            )
            continue
        first_leg, second_leg = ordered
        if (
            first_leg.home_team.external_team_id
            != second_leg.away_team.external_team_id
            or first_leg.away_team.external_team_id
            != second_leg.home_team.external_team_id
            or first_leg.starts_at_utc == second_leg.starts_at_utc
        ):
            conflicts.append(
                ProviderConflict(
                    code="invalid_two_legged_orientation",
                    message=(
                        f"Round {round_key} has ambiguous leg order or orientation."
                    ),
                    external_match_id=first_leg.external_match_id,
                )
            )
            continue
        ties.append(
            ExternalTwoLeggedTie(
                round_key=round_key,
                first_leg=first_leg,
                second_leg=second_leg,
            )
        )

    final: ExternalKnockoutMatch | None
    if len(finals) == 1:
        final = finals[0]
    elif not finals:
        final = None
    else:
        final = None
        conflicts.append(
            ProviderConflict(
                code="ambiguous_final",
                message="football-data.org returned more than one Champions League final.",
            )
        )

    round_order = {
        ROUND_KNOCKOUT_PLAYOFFS: 0,
        ROUND_OF_16: 1,
        ROUND_QUARTER_FINALS: 2,
        ROUND_SEMI_FINALS: 3,
    }
    ties.sort(
        key=lambda tie: (
            round_order[tie.round_key],
            tie.first_leg.starts_at_utc,
            tie.first_leg.external_match_id,
        )
    )
    pending.sort(key=lambda match: (match.starts_at_utc, match.external_match_id))
    return FootballDataKnockoutSnapshot(
        source=batch.source,
        external_event_id=batch.external_event_id,
        season_start_year=batch.season_start_year,
        two_legged_ties=tuple(ties),
        final=final,
        pending_matches=tuple(pending),
        conflicts=tuple(conflicts),
        ignored_match_count=batch.ignored_match_count,
    )


class _IgnoredMatch(Exception):
    pass


class _MatchConflict(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        external_match_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.external_match_id = external_match_id


def _parse_match(
    value: object,
    *,
    external_event_id: str,
    season_start_year: int,
) -> ExternalKnockoutMatch:
    if not isinstance(value, Mapping):
        raise _MatchConflict(
            "invalid_match",
            "football-data.org match is not an object.",
        )
    external_match_id = _parse_optional_external_id(value.get("id"))
    stage = value.get("stage")
    if not isinstance(stage, str) or not stage.strip():
        raise _MatchConflict(
            "missing_stage",
            "football-data.org match has no stage.",
            external_match_id=external_match_id,
        )
    normalized_stage = stage.strip().upper()
    if normalized_stage in _KNOWN_NON_KNOCKOUT_STAGES:
        raise _IgnoredMatch
    round_key = _STAGE_TO_ROUND.get(normalized_stage)
    if round_key is None:
        raise _MatchConflict(
            "unsupported_stage",
            f"Unsupported football-data.org stage: {normalized_stage}.",
            external_match_id=external_match_id,
        )
    if external_match_id is None:
        raise _MatchConflict(
            "missing_external_match_id",
            "football-data.org knockout match has no valid ID.",
        )

    competition = value.get("competition")
    if not isinstance(competition, Mapping) or (
        _parse_optional_external_id(competition.get("id"))
        != str(CHAMPIONS_LEAGUE_COMPETITION_ID)
        or competition.get("code") != CHAMPIONS_LEAGUE_COMPETITION_CODE
    ):
        raise _MatchConflict(
            "competition_mismatch",
            "football-data.org match belongs to a different competition.",
            external_match_id=external_match_id,
        )

    season = value.get("season")
    if not isinstance(season, Mapping):
        raise _MatchConflict(
            "invalid_season",
            "football-data.org match has an invalid season.",
            external_match_id=external_match_id,
        )
    start_date = season.get("startDate")
    if not isinstance(start_date, str) or not start_date.startswith(
        f"{season_start_year:04d}-"
    ):
        raise _MatchConflict(
            "season_mismatch",
            "football-data.org match belongs to a different season.",
            external_match_id=external_match_id,
        )

    try:
        home_team = _parse_team(value.get("homeTeam"), field_name="homeTeam")
        away_team = _parse_team(value.get("awayTeam"), field_name="awayTeam")
    except ValueError as error:
        raise _MatchConflict(
            "invalid_team",
            str(error),
            external_match_id=external_match_id,
        ) from error
    if home_team.external_team_id == away_team.external_team_id:
        raise _MatchConflict(
            "same_team",
            "football-data.org match contains the same team twice.",
            external_match_id=external_match_id,
        )

    try:
        starts_at = _parse_datetime(value.get("utcDate"), field_name="utcDate")
    except ValueError as error:
        raise _MatchConflict(
            "invalid_start",
            str(error),
            external_match_id=external_match_id,
        ) from error
    status = value.get("status")
    if not isinstance(status, str) or status.strip().upper() not in _SUPPORTED_STATUSES:
        raise _MatchConflict(
            "invalid_status",
            "football-data.org match has an unsupported status.",
            external_match_id=external_match_id,
        )
    normalized_status = status.strip().upper()

    score: ExternalMatchScore | None = None
    if normalized_status == "FINISHED":
        try:
            score = _parse_finished_score(
                value.get("score"),
                home_team=home_team,
                away_team=away_team,
            )
        except ValueError as error:
            raise _MatchConflict(
                "invalid_finished_score",
                str(error),
                external_match_id=external_match_id,
            ) from error

    last_updated = value.get("lastUpdated")
    try:
        normalized_last_updated = _parse_datetime(
            last_updated,
            field_name="lastUpdated",
        )
    except ValueError as error:
        raise _MatchConflict(
            "invalid_last_updated",
            str(error),
            external_match_id=external_match_id,
        ) from error

    return ExternalKnockoutMatch(
        external_match_id=external_match_id,
        external_event_id=external_event_id,
        round_key=round_key,
        provider_stage=normalized_stage,
        home_team=home_team,
        away_team=away_team,
        starts_at_utc=starts_at,
        status=normalized_status,
        score=score,
        last_updated_at_utc=normalized_last_updated,
    )


def _parse_team(value: object, *, field_name: str) -> ExternalTeam:
    team = _require_nested_mapping(value, field_name=field_name)
    external_team_id = _parse_optional_external_id(team.get("id"))
    if external_team_id is None:
        raise ValueError(f"football-data.org {field_name} has no valid ID.")
    name = team.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"football-data.org {field_name} has no name.")
    return ExternalTeam(
        external_team_id=external_team_id,
        name=" ".join(name.split()),
    )


def _parse_finished_score(
    value: object,
    *,
    home_team: ExternalTeam,
    away_team: ExternalTeam,
) -> ExternalMatchScore:
    score = _require_nested_mapping(value, field_name="score")
    duration = score.get("duration")
    if duration not in {"REGULAR", "EXTRA_TIME", "PENALTY_SHOOTOUT"}:
        raise ValueError(
            "football-data.org finished match has an unsupported score duration."
        )
    if duration == "REGULAR":
        regular_home, regular_away = _parse_score_pair(
            score.get("fullTime"),
            field_name="score.fullTime",
        )
        extra_home = extra_away = None
        penalty_home = penalty_away = None
    else:
        regular_home, regular_away = _parse_score_pair(
            score.get("regularTime"),
            field_name="score.regularTime",
        )
        extra_home, extra_away = _parse_score_pair(
            score.get("extraTime"),
            field_name="score.extraTime",
        )
        if duration == "PENALTY_SHOOTOUT":
            penalty_home, penalty_away = _parse_score_pair(
                score.get("penalties"),
                field_name="score.penalties",
            )
            if penalty_home == penalty_away:
                raise ValueError("Penalty shootout score must not be tied.")
        else:
            penalty_home = penalty_away = None

    winner = score.get("winner")
    if winner == "HOME_TEAM":
        winner_external_team_id = home_team.external_team_id
    elif winner == "AWAY_TEAM":
        winner_external_team_id = away_team.external_team_id
    elif winner == "DRAW" and duration == "REGULAR":
        winner_external_team_id = None
    else:
        raise ValueError("football-data.org finished match has an invalid winner.")
    if duration == "REGULAR":
        expected_winner = (
            "DRAW"
            if regular_home == regular_away
            else ("HOME_TEAM" if regular_home > regular_away else "AWAY_TEAM")
        )
    elif duration == "EXTRA_TIME":
        if extra_home == extra_away:
            raise ValueError(
                "Extra-time score must decide a match that ended in extra time."
            )
        expected_winner = "HOME_TEAM" if extra_home > extra_away else "AWAY_TEAM"
    else:
        if extra_home != extra_away:
            raise ValueError("Extra-time score must be tied before a penalty shootout.")
        expected_winner = "HOME_TEAM" if penalty_home > penalty_away else "AWAY_TEAM"
    if winner != expected_winner:
        raise ValueError("football-data.org winner conflicts with the decisive score.")
    return ExternalMatchScore(
        regular_home=regular_home,
        regular_away=regular_away,
        extra_time_home=extra_home,
        extra_time_away=extra_away,
        penalty_home=penalty_home,
        penalty_away=penalty_away,
        winner_external_team_id=winner_external_team_id,
        duration=duration,
    )


def _parse_score_pair(value: object, *, field_name: str) -> tuple[int, int]:
    pair = _require_nested_mapping(value, field_name=field_name)
    modern_keys_present = "home" in pair or "away" in pair
    legacy_keys_present = "homeTeam" in pair or "awayTeam" in pair
    if modern_keys_present and legacy_keys_present:
        modern = (_parse_score(pair.get("home")), _parse_score(pair.get("away")))
        legacy = (
            _parse_score(pair.get("homeTeam")),
            _parse_score(pair.get("awayTeam")),
        )
        if modern != legacy:
            raise ValueError(f"football-data.org {field_name} is ambiguous.")
        return modern
    if modern_keys_present:
        return _parse_score(pair.get("home")), _parse_score(pair.get("away"))
    if legacy_keys_present:
        return (
            _parse_score(pair.get("homeTeam")),
            _parse_score(pair.get("awayTeam")),
        )
    raise ValueError(f"football-data.org {field_name} has no home/away score.")


def _parse_score(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("football-data.org score must be a non-negative integer.")
    return value


def _parse_positive_external_id(value: object) -> str:
    parsed = _parse_optional_external_id(value)
    if parsed is None:
        raise FootballDataResponseError(
            "football-data.org response contains an invalid identifier."
        )
    return parsed


def _parse_optional_external_id(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized.isascii() or not normalized.isdecimal():
        return None
    parsed = int(normalized)
    return str(parsed) if parsed > 0 else None


def _require_mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FootballDataResponseError(
            f"football-data.org {field_name} is not an object."
        )
    return value


def _require_nested_mapping(value: object, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"football-data.org {field_name} is not an object.")
    return value


def _parse_datetime(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"football-data.org {field_name} is missing.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"football-data.org {field_name} is not an ISO datetime."
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"football-data.org {field_name} has no timezone.")
    return (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def external_team_ids(
    matches: Sequence[ExternalKnockoutMatch],
) -> frozenset[str]:
    return frozenset(
        team_id
        for match in matches
        for team_id in (
            match.home_team.external_team_id,
            match.away_team.external_team_id,
        )
    )

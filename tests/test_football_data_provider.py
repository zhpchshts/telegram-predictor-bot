from __future__ import annotations

import asyncio

import pytest

from app.football_data_provider import (
    FootballDataClient,
    FootballDataResponseError,
    ROUND_FINAL,
    ROUND_KNOCKOUT_PLAYOFFS,
    ROUND_OF_16,
    group_football_data_knockout,
    parse_football_data_matches,
)


def _team(team_id: int, name: str) -> dict[str, object]:
    return {"id": team_id, "name": name}


def _match(
    match_id: int,
    *,
    stage: str,
    home_id: int,
    home_name: str,
    away_id: int,
    away_name: str,
    starts_at: str,
    status: str = "TIMED",
    score: object | None = None,
) -> dict[str, object]:
    return {
        "id": match_id,
        "competition": {
            "id": 2001,
            "code": "CL",
            "name": "UEFA Champions League",
        },
        "season": {"id": 2026, "startDate": "2026-07-01"},
        "utcDate": starts_at,
        "status": status,
        "stage": stage,
        "homeTeam": _team(home_id, home_name),
        "awayTeam": _team(away_id, away_name),
        "score": score,
        "lastUpdated": "2026-09-01T12:00:00Z",
    }


def _payload(matches: list[object]) -> dict[str, object]:
    return {
        "competition": {
            "id": 2001,
            "code": "CL",
            "name": "UEFA Champions League",
        },
        "matches": matches,
    }


def test_parser_groups_two_legs_and_keeps_strict_90_minute_score() -> None:
    first_leg = _match(
        101,
        stage="LAST_16",
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-02-10T20:00:00Z",
        status="FINISHED",
        score={
            "winner": "HOME_TEAM",
            "duration": "REGULAR",
            "fullTime": {"home": 2, "away": 1},
        },
    )
    second_leg = _match(
        102,
        stage="LAST_16",
        home_id=20,
        home_name="Beta FC",
        away_id=10,
        away_name="Alpha FC",
        starts_at="2027-02-17T20:00:00Z",
        status="FINISHED",
        score={
            "winner": "HOME_TEAM",
            "duration": "PENALTY_SHOOTOUT",
            # fullTime deliberately includes later phases and must be ignored.
            "fullTime": {"home": 7, "away": 5},
            "regularTime": {"home": 1, "away": 0},
            "extraTime": {"home": 0, "away": 0},
            "penalties": {"home": 6, "away": 5},
        },
    )
    league_match = _match(
        1,
        stage="LEAGUE_STAGE",
        home_id=10,
        home_name="Alpha FC",
        away_id=30,
        away_name="Gamma FC",
        starts_at="2026-09-10T20:00:00Z",
    )

    batch = parse_football_data_matches(
        _payload([second_leg, league_match, first_leg]),
        season_start_year=2026,
    )
    snapshot = group_football_data_knockout(batch)

    assert batch.ignored_match_count == 1
    assert batch.conflicts == ()
    assert len(snapshot.two_legged_ties) == 1
    tie = snapshot.two_legged_ties[0]
    assert tie.round_key == ROUND_OF_16
    assert tie.first_leg.external_match_id == "101"
    assert tie.second_leg.external_match_id == "102"
    assert tie.second_leg.score is not None
    assert (tie.second_leg.score.regular_home, tie.second_leg.score.regular_away) == (
        1,
        0,
    )
    assert (
        tie.second_leg.score.extra_time_home,
        tie.second_leg.score.extra_time_away,
    ) == (
        0,
        0,
    )
    assert (tie.second_leg.score.penalty_home, tie.second_leg.score.penalty_away) == (
        6,
        5,
    )


def test_parser_keeps_incomplete_pair_pending_and_parses_final() -> None:
    playoff_leg = _match(
        201,
        stage="PLAYOFFS",
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-01-20T20:00:00Z",
    )
    final = _match(
        999,
        stage="FINAL",
        home_id=30,
        home_name="Gamma FC",
        away_id=40,
        away_name="Delta FC",
        starts_at="2027-05-29T19:00:00Z",
    )

    snapshot = group_football_data_knockout(
        parse_football_data_matches(
            _payload([playoff_leg, final]),
            season_start_year=2026,
        )
    )

    assert snapshot.two_legged_ties == ()
    assert len(snapshot.pending_matches) == 1
    assert snapshot.pending_matches[0].round_key == ROUND_KNOCKOUT_PLAYOFFS
    assert snapshot.final is not None
    assert snapshot.final.round_key == ROUND_FINAL


def test_parser_ignores_qualification_playoff_and_requires_last_updated() -> None:
    qualification_playoff = _match(
        250,
        stage="PLAYOFF_ROUND_1",
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2026-08-20T20:00:00Z",
    )
    missing_last_updated = _match(
        251,
        stage="PLAYOFFS",
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-02-10T20:00:00Z",
    )
    missing_last_updated.pop("lastUpdated")

    batch = parse_football_data_matches(
        _payload([qualification_playoff, missing_last_updated]),
        season_start_year=2026,
    )

    assert batch.matches == ()
    assert batch.ignored_match_count == 1
    assert len(batch.conflicts) == 1
    assert batch.conflicts[0].code == "invalid_last_updated"
    assert batch.conflicts[0].external_match_id == "251"


def test_invalid_finished_knockout_score_becomes_conflict() -> None:
    invalid = _match(
        301,
        stage="QUARTER_FINALS",
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-04-01T20:00:00Z",
        status="FINISHED",
        score={
            "winner": "HOME_TEAM",
            "duration": "EXTRA_TIME",
            # Missing regularTime: it is unsafe to infer 90 minutes from fullTime.
            "fullTime": {"home": 2, "away": 1},
            "extraTime": {"home": 1, "away": 0},
        },
    )

    batch = parse_football_data_matches(
        _payload([invalid]),
        season_start_year=2026,
    )

    assert batch.matches == ()
    assert len(batch.conflicts) == 1
    assert batch.conflicts[0].code == "invalid_finished_score"
    assert batch.conflicts[0].external_match_id == "301"


def test_finished_score_with_inconsistent_winner_becomes_conflict() -> None:
    invalid = _match(
        302,
        stage="QUARTER_FINALS",
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-04-01T20:00:00Z",
        status="FINISHED",
        score={
            "winner": "AWAY_TEAM",
            "duration": "REGULAR",
            "fullTime": {"home": 2, "away": 1},
        },
    )

    batch = parse_football_data_matches(
        _payload([invalid]),
        season_start_year=2026,
    )

    assert batch.matches == ()
    assert len(batch.conflicts) == 1
    assert batch.conflicts[0].code == "invalid_finished_score"


def test_parser_rejects_ambiguous_leg_orientation_and_duplicate_ids() -> None:
    first = _match(
        401,
        stage="SEMI_FINALS",
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-05-01T20:00:00Z",
    )
    same_orientation = _match(
        402,
        stage="SEMI_FINALS",
        home_id=10,
        home_name="Alpha FC",
        away_id=20,
        away_name="Beta FC",
        starts_at="2027-05-08T20:00:00Z",
    )
    duplicate = _match(
        401,
        stage="FINAL",
        home_id=30,
        home_name="Gamma FC",
        away_id=40,
        away_name="Delta FC",
        starts_at="2027-05-29T19:00:00Z",
    )

    snapshot = group_football_data_knockout(
        parse_football_data_matches(
            _payload([first, same_orientation, duplicate]),
            season_start_year=2026,
        )
    )

    assert snapshot.two_legged_ties == ()
    assert {conflict.code for conflict in snapshot.conflicts} == {
        "duplicate_external_match",
    }
    # The duplicate ID removes match 401. Match 402 remains safely pending.
    assert [item.external_match_id for item in snapshot.pending_matches] == ["402"]


def test_parser_rejects_team_repeated_across_pairs_in_same_round() -> None:
    matches = [
        _match(
            501,
            stage="SEMI_FINALS",
            home_id=10,
            home_name="Alpha FC",
            away_id=20,
            away_name="Beta FC",
            starts_at="2027-05-01T18:00:00Z",
        ),
        _match(
            502,
            stage="SEMI_FINALS",
            home_id=20,
            home_name="Beta FC",
            away_id=10,
            away_name="Alpha FC",
            starts_at="2027-05-08T18:00:00Z",
        ),
        _match(
            503,
            stage="SEMI_FINALS",
            home_id=10,
            home_name="Alpha FC",
            away_id=30,
            away_name="Gamma FC",
            starts_at="2027-05-02T18:00:00Z",
        ),
        _match(
            504,
            stage="SEMI_FINALS",
            home_id=30,
            home_name="Gamma FC",
            away_id=10,
            away_name="Alpha FC",
            starts_at="2027-05-09T18:00:00Z",
        ),
    ]

    snapshot = group_football_data_knockout(
        parse_football_data_matches(_payload(matches), season_start_year=2026)
    )

    assert snapshot.two_legged_ties == ()
    assert {conflict.code for conflict in snapshot.conflicts} == {
        "team_repeated_in_round"
    }


def test_parser_rejects_round_with_more_pairs_than_bracket_capacity() -> None:
    matches: list[dict[str, object]] = []
    for position in range(5):
        first_team_id = 100 + position * 2
        second_team_id = first_team_id + 1
        matches.extend(
            [
                _match(
                    600 + position * 2,
                    stage="QUARTER_FINALS",
                    home_id=first_team_id,
                    home_name=f"Team {first_team_id}",
                    away_id=second_team_id,
                    away_name=f"Team {second_team_id}",
                    starts_at=f"2027-04-{position + 1:02d}T18:00:00Z",
                ),
                _match(
                    601 + position * 2,
                    stage="QUARTER_FINALS",
                    home_id=second_team_id,
                    home_name=f"Team {second_team_id}",
                    away_id=first_team_id,
                    away_name=f"Team {first_team_id}",
                    starts_at=f"2027-04-{position + 8:02d}T18:00:00Z",
                ),
            ]
        )

    snapshot = group_football_data_knockout(
        parse_football_data_matches(_payload(matches), season_start_year=2026)
    )

    assert snapshot.two_legged_ties == ()
    assert {conflict.code for conflict in snapshot.conflicts} == {
        "round_capacity_exceeded"
    }


def test_top_level_competition_mismatch_is_fatal() -> None:
    payload = _payload([])
    payload["competition"] = {"id": 2021, "code": "PL"}

    with pytest.raises(FootballDataResponseError, match="different competition"):
        parse_football_data_matches(payload, season_start_year=2026)


def test_client_uses_expected_endpoint_token_and_injectable_transport() -> None:
    captured: dict[str, object] = {}

    async def fetch_json(url: str, headers, timeout: float) -> object:
        captured.update(url=url, headers=dict(headers), timeout=timeout)
        return _payload([])

    client = FootballDataClient(
        token="secret",
        season_start_year=2026,
        timeout_seconds=7.5,
        fetch_json=fetch_json,
    )

    snapshot = asyncio.run(client.fetch_champions_league_knockout())

    assert captured["url"] == (
        "https://api.football-data.org/v4/competitions/CL/matches?season=2026"
    )
    assert captured["headers"]["X-Auth-Token"] == "secret"
    assert captured["timeout"] == 7.5
    assert snapshot.external_event_id == "CL:2026"


def test_client_rejects_cross_season_configuration() -> None:
    with pytest.raises(ValueError, match="fixed 2026/27 template"):
        FootballDataClient(token="secret", season_start_year=2027)

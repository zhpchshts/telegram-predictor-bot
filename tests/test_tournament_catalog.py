from __future__ import annotations

import re
from pathlib import Path

from app.database import create_connection, initialize_database
from app.tournament_catalog import (
    CHAMPIONS_LEAGUE_BRACKET_NODE_COUNT,
    CHAMPIONS_LEAGUE_KNOCKOUT_ROUND_CAPACITIES,
    CHAMPIONS_LEAGUE_KNOCKOUT_ROUNDS,
    CHAMPIONS_LEAGUE_ROUNDS,
    CONTEST_TEMPLATE_OPTIONS,
    CREATABLE_TEMPLATE_KEYS,
    SUPPORTED_TEMPLATE_KEYS,
    TOURNAMENT_TEMPLATES,
    TOURNAMENT_TEMPLATES_BY_KEY,
)


def _check_values(connection, table: str, column: str) -> set[str]:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    assert row is not None
    match = re.search(
        rf"\b{column}\b\s+[^,]*?\b{column}\b\s+IN\s*\((.*?)\)",
        str(row["sql"]),
        flags=re.DOTALL,
    )
    assert match is not None
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_template_catalog_has_one_consistent_key_set() -> None:
    expected_keys = {template.key for template in TOURNAMENT_TEMPLATES}

    assert set(TOURNAMENT_TEMPLATES_BY_KEY) == expected_keys
    assert SUPPORTED_TEMPLATE_KEYS == expected_keys
    assert CREATABLE_TEMPLATE_KEYS <= SUPPORTED_TEMPLATE_KEYS
    assert {option["key"] for option in CONTEST_TEMPLATE_OPTIONS} == expected_keys
    assert len(CONTEST_TEMPLATE_OPTIONS) == len(expected_keys)


def test_champions_league_round_views_are_derived_from_round_catalog() -> None:
    assert CHAMPIONS_LEAGUE_BRACKET_NODE_COUNT == 23
    assert CHAMPIONS_LEAGUE_KNOCKOUT_ROUND_CAPACITIES == {
        round_definition.key: round_definition.node_count
        for round_definition in CHAMPIONS_LEAGUE_ROUNDS
    }
    assert CHAMPIONS_LEAGUE_KNOCKOUT_ROUNDS == {
        round_definition.key: (
            round_definition.name,
            round_definition.stage_position,
            round_definition.stage_type,
        )
        for round_definition in CHAMPIONS_LEAGUE_ROUNDS
    }


def test_database_template_checks_match_catalog(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)

    with create_connection(database_path) as connection:
        assert _check_values(connection, "contests", "template_key") == set(
            SUPPORTED_TEMPLATE_KEYS
        )
        assert _check_values(connection, "shared_tournaments", "template_key") == set(
            SUPPORTED_TEMPLATE_KEYS
        )
        assert _check_values(connection, "stages", "stage_key") == {
            round_definition.key for round_definition in CHAMPIONS_LEAGUE_ROUNDS
        }

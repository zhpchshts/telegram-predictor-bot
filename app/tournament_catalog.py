"""Canonical immutable metadata for supported tournament templates.

This module intentionally contains data only. Contest commands, shared-tournament
commands, bracket persistence, scoring, and HTTP presentation remain in their
domain modules. Keeping the metadata dependency-free gives those modules one
place to agree on template keys, scoring defaults, and Champions League rounds.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


SwissStageSelectionMode = Literal["exact", "up_to_limits"]
RoundKey = Literal[
    "playoff",
    "round_of_16",
    "quarterfinal",
    "semifinal",
    "final",
]
NodeFormat = Literal["two_legged", "single"]
StageType = Literal["knockout", "final"]

WORLD_CUP_2026_TEMPLATE_KEY = "world_cup_2026"
THE_INTERNATIONAL_2026_TEMPLATE_KEY = "the_international_2026"
CHAMPIONS_LEAGUE_2026_27_TEMPLATE_KEY = "champions_league_2026_27"

WORLD_CUP_2026_COMPETITION_NAME = "Чемпионат мира"
WORLD_CUP_2026_SEASON = "2026"
WORLD_CUP_2026_COMPETITION_TYPE = "world_cup"

THE_INTERNATIONAL_2026_COMPETITION_NAME = "The International"
THE_INTERNATIONAL_2026_SEASON = "2026"
THE_INTERNATIONAL_2026_COMPETITION_TYPE = "the_international"

CHAMPIONS_LEAGUE_2026_27_COMPETITION_NAME = "Лига чемпионов"
CHAMPIONS_LEAGUE_2026_27_SEASON = "2026/27"
CHAMPIONS_LEAGUE_2026_27_COMPETITION_TYPE = "champions_league"

DEFAULT_EXACT_SCORE_POINTS = 3
DEFAULT_GOAL_DIFFERENCE_POINTS = 2
DEFAULT_OUTCOME_POINTS = 1
DEFAULT_ADVANCING_TEAM_POINTS = 1

DEFAULT_SWISS_SELECTION_MODE: SwissStageSelectionMode = "exact"
DEFAULT_SWISS_DIRECT_CORRECT_POINTS = 2
DEFAULT_SWISS_ELIMINATION_CORRECT_POINTS = 2
DEFAULT_SWISS_CROSS_CATEGORY_POINTS = 1

CHAMPIONS_LEAGUE_2026_27_DIRECT_COUNT = 8
CHAMPIONS_LEAGUE_2026_27_ELIMINATED_COUNT = 12
CHAMPIONS_LEAGUE_2026_27_SWISS_SELECTION_MODE: SwissStageSelectionMode = "up_to_limits"
CHAMPIONS_LEAGUE_2026_27_DIRECT_CORRECT_POINTS = 2
CHAMPIONS_LEAGUE_2026_27_ELIMINATION_CORRECT_POINTS = 1
CHAMPIONS_LEAGUE_2026_27_CROSS_CATEGORY_POINTS = 0


@dataclass(frozen=True, slots=True)
class TournamentTemplate:
    key: str
    label: str
    slug_prefix: str
    competition_name: str
    competition_season: str
    competition_type: str
    exact_score_points: int
    goal_difference_points: int
    outcome_points: int
    advancing_team_points: int
    champion_prediction_points: int
    swiss_direct_qualifier_count: int
    swiss_elimination_qualifier_count: int
    swiss_selection_mode: SwissStageSelectionMode
    swiss_direct_correct_points: int
    swiss_elimination_correct_points: int
    swiss_cross_category_points: int


WORLD_CUP_2026_TEMPLATE = TournamentTemplate(
    key=WORLD_CUP_2026_TEMPLATE_KEY,
    label="Чемпионат мира 2026",
    slug_prefix="world-cup-2026",
    competition_name=WORLD_CUP_2026_COMPETITION_NAME,
    competition_season=WORLD_CUP_2026_SEASON,
    competition_type=WORLD_CUP_2026_COMPETITION_TYPE,
    exact_score_points=DEFAULT_EXACT_SCORE_POINTS,
    goal_difference_points=DEFAULT_GOAL_DIFFERENCE_POINTS,
    outcome_points=DEFAULT_OUTCOME_POINTS,
    advancing_team_points=DEFAULT_ADVANCING_TEAM_POINTS,
    champion_prediction_points=5,
    swiss_direct_qualifier_count=3,
    swiss_elimination_qualifier_count=5,
    swiss_selection_mode=DEFAULT_SWISS_SELECTION_MODE,
    swiss_direct_correct_points=DEFAULT_SWISS_DIRECT_CORRECT_POINTS,
    swiss_elimination_correct_points=DEFAULT_SWISS_ELIMINATION_CORRECT_POINTS,
    swiss_cross_category_points=DEFAULT_SWISS_CROSS_CATEGORY_POINTS,
)

THE_INTERNATIONAL_2026_TEMPLATE = TournamentTemplate(
    key=THE_INTERNATIONAL_2026_TEMPLATE_KEY,
    label="The International 2026",
    slug_prefix="the-international-2026",
    competition_name=THE_INTERNATIONAL_2026_COMPETITION_NAME,
    competition_season=THE_INTERNATIONAL_2026_SEASON,
    competition_type=THE_INTERNATIONAL_2026_COMPETITION_TYPE,
    exact_score_points=2,
    goal_difference_points=0,
    outcome_points=1,
    advancing_team_points=0,
    champion_prediction_points=4,
    swiss_direct_qualifier_count=3,
    swiss_elimination_qualifier_count=5,
    swiss_selection_mode=DEFAULT_SWISS_SELECTION_MODE,
    swiss_direct_correct_points=DEFAULT_SWISS_DIRECT_CORRECT_POINTS,
    swiss_elimination_correct_points=DEFAULT_SWISS_ELIMINATION_CORRECT_POINTS,
    swiss_cross_category_points=DEFAULT_SWISS_CROSS_CATEGORY_POINTS,
)

CHAMPIONS_LEAGUE_2026_27_TEMPLATE = TournamentTemplate(
    key=CHAMPIONS_LEAGUE_2026_27_TEMPLATE_KEY,
    label="Лига чемпионов 2026/27",
    slug_prefix="champions-league-2026-27",
    competition_name=CHAMPIONS_LEAGUE_2026_27_COMPETITION_NAME,
    competition_season=CHAMPIONS_LEAGUE_2026_27_SEASON,
    competition_type=CHAMPIONS_LEAGUE_2026_27_COMPETITION_TYPE,
    exact_score_points=DEFAULT_EXACT_SCORE_POINTS,
    goal_difference_points=DEFAULT_GOAL_DIFFERENCE_POINTS,
    outcome_points=DEFAULT_OUTCOME_POINTS,
    advancing_team_points=DEFAULT_ADVANCING_TEAM_POINTS,
    champion_prediction_points=5,
    swiss_direct_qualifier_count=CHAMPIONS_LEAGUE_2026_27_DIRECT_COUNT,
    swiss_elimination_qualifier_count=CHAMPIONS_LEAGUE_2026_27_ELIMINATED_COUNT,
    swiss_selection_mode=CHAMPIONS_LEAGUE_2026_27_SWISS_SELECTION_MODE,
    swiss_direct_correct_points=CHAMPIONS_LEAGUE_2026_27_DIRECT_CORRECT_POINTS,
    swiss_elimination_correct_points=(
        CHAMPIONS_LEAGUE_2026_27_ELIMINATION_CORRECT_POINTS
    ),
    swiss_cross_category_points=CHAMPIONS_LEAGUE_2026_27_CROSS_CATEGORY_POINTS,
)

TOURNAMENT_TEMPLATES: tuple[TournamentTemplate, ...] = (
    WORLD_CUP_2026_TEMPLATE,
    THE_INTERNATIONAL_2026_TEMPLATE,
    CHAMPIONS_LEAGUE_2026_27_TEMPLATE,
)
TOURNAMENT_TEMPLATES_BY_KEY: Mapping[str, TournamentTemplate] = MappingProxyType(
    {template.key: template for template in TOURNAMENT_TEMPLATES}
)
SUPPORTED_TEMPLATE_KEYS = frozenset(TOURNAMENT_TEMPLATES_BY_KEY)

# Completed seasonal templates remain readable, but only the current season is
# available when creating a new contest.
CREATABLE_TEMPLATE_KEYS: frozenset[str] = frozenset(
    {CHAMPIONS_LEAGUE_2026_27_TEMPLATE_KEY}
)
CONTEST_TEMPLATE_OPTIONS: tuple[dict[str, str], ...] = tuple(
    {"key": template.key, "label": template.label} for template in TOURNAMENT_TEMPLATES
)


@dataclass(frozen=True, slots=True)
class ChampionsLeagueRound:
    key: RoundKey
    name: str
    stage_position: int
    node_format: NodeFormat
    node_count: int
    stage_type: StageType


CHAMPIONS_LEAGUE_ROUNDS: tuple[ChampionsLeagueRound, ...] = (
    ChampionsLeagueRound("playoff", "Стыковые матчи", 10, "two_legged", 8, "knockout"),
    ChampionsLeagueRound("round_of_16", "1/8 финала", 20, "two_legged", 8, "knockout"),
    ChampionsLeagueRound("quarterfinal", "1/4 финала", 30, "two_legged", 4, "knockout"),
    ChampionsLeagueRound("semifinal", "1/2 финала", 40, "two_legged", 2, "knockout"),
    ChampionsLeagueRound("final", "Финал", 50, "single", 1, "final"),
)
CHAMPIONS_LEAGUE_KNOCKOUT_ROUNDS: Mapping[str, tuple[str, int, str]] = MappingProxyType(
    {
        round_definition.key: (
            round_definition.name,
            round_definition.stage_position,
            round_definition.stage_type,
        )
        for round_definition in CHAMPIONS_LEAGUE_ROUNDS
    }
)
CHAMPIONS_LEAGUE_KNOCKOUT_ROUND_CAPACITIES: Mapping[str, int] = MappingProxyType(
    {
        round_definition.key: round_definition.node_count
        for round_definition in CHAMPIONS_LEAGUE_ROUNDS
    }
)
CHAMPIONS_LEAGUE_BRACKET_NODE_COUNT = sum(
    round_definition.node_count for round_definition in CHAMPIONS_LEAGUE_ROUNDS
)

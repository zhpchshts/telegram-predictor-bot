"""Transport request contracts for the Telegram Mini App API.

The module has no database or service dependencies. Keeping request validation
here makes the HTTP boundary discoverable without mixing it with endpoint
orchestration and response presentation in ``tma_api``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


SQLITE_SIGNED_64_MIN = -(2**63)
SQLITE_SIGNED_64_MAX = 2**63 - 1


def _reject_boolean_integer(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Boolean values are not valid integers.")
    return value


SqliteInteger = Annotated[
    int,
    BeforeValidator(_reject_boolean_integer),
    Field(ge=SQLITE_SIGNED_64_MIN, le=SQLITE_SIGNED_64_MAX),
]


class CreateContestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    template_key: str
    shared_tournament_id: SqliteInteger | None = None


class CreateMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home_team_id: SqliteInteger
    away_team_id: SqliteInteger
    starts_at_utc: str
    best_of: Literal[3, 5] | None = None
    round_key: (
        Literal["playoff", "round_of_16", "quarterfinal", "semifinal", "final"] | None
    ) = None


class CreateTwoLeggedTieRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_team_id: SqliteInteger
    second_team_id: SqliteInteger
    first_leg_starts_at_utc: str
    second_leg_starts_at_utc: str
    round_key: Literal["playoff", "round_of_16", "quarterfinal", "semifinal"] | None = (
        None
    )


class CreateSharedTwoLeggedTieRequest(CreateTwoLeggedTieRequest):
    bracket_position: SqliteInteger | None = Field(default=None, gt=0)


class SaveTournamentTeamsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_names: list[str]


class UpdateMatchStartRequest(BaseModel):
    starts_at_utc: str


class SaveMatchPredictionRequest(BaseModel):
    predicted_home_score: SqliteInteger = Field(
        description=(
            "Для футбольного матча — прогноз счёта хозяев строго после 90 минут."
        )
    )
    predicted_away_score: SqliteInteger = Field(
        description=(
            "Для футбольного матча — прогноз счёта гостей строго после 90 минут."
        )
    )
    predicted_advancing_team_id: SqliteInteger | None = Field(
        default=None,
        description=(
            "Победитель или прошедшая команда. Для футбольного матча хранится "
            "отдельно от счёта после 90 минут."
        ),
    )


class SaveMatchResultRequest(BaseModel):
    home_score: SqliteInteger = Field(
        description="Для футбольного матча — счёт хозяев строго после 90 минут."
    )
    away_score: SqliteInteger = Field(
        description="Для футбольного матча — счёт гостей строго после 90 минут."
    )
    advancing_team_id: SqliteInteger | None = Field(
        default=None,
        description=(
            "Победитель или прошедшая команда. Для футбольного матча хранится "
            "отдельно от счёта после 90 минут."
        ),
    )


class SaveTwoLeggedTiePredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predicted_advancing_team_id: SqliteInteger


class SaveTwoLeggedTieResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    advancing_team_id: SqliteInteger | None = None
    second_leg_extra_time_home_score: SqliteInteger | None = None
    second_leg_extra_time_away_score: SqliteInteger | None = None
    second_leg_home_penalty_score: SqliteInteger | None = None
    second_leg_away_penalty_score: SqliteInteger | None = None


class SaveChampionPredictionSettingsRequest(BaseModel):
    enabled: bool
    deadline_at: str | None = None
    points: SqliteInteger


class SaveMatchPredictionPublicationSettingsRequest(BaseModel):
    enabled: bool


class SavePredictionReminderSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool
    lead_time_minutes: Literal[60, 180, 360, 1440]


class SaveNotificationPreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mention_in_prediction_reminders: bool


class SaveChampionPredictionRequest(BaseModel):
    predicted_team_id: SqliteInteger


class SaveContestChampionRequest(BaseModel):
    champion_team_id: SqliteInteger


class SaveSwissStagePredictionSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    deadline_at: str | None = None
    direct_qualifier_count: SqliteInteger = 3
    elimination_qualifier_count: SqliteInteger = 5


class SaveSwissStageSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direct_team_ids: list[SqliteInteger]
    elimination_team_ids: list[SqliteInteger]


class ResolveRoleTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str


class SaveChatSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_button_text: str


class CreateSharedTournamentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    template_key: str


class SaveSharedTournamentTeamsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_names: list[str]
    expected_version: SqliteInteger


class SharedTournamentVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: SqliteInteger


class SaveSharedFixtureSyncRequest(SharedTournamentVersionRequest):
    enabled: bool


class SaveSharedChampionSettingsRequest(SaveChampionPredictionSettingsRequest):
    expected_version: SqliteInteger


class SaveSharedChampionResultRequest(SaveContestChampionRequest):
    expected_version: SqliteInteger


class SaveSharedSwissSettingsRequest(SaveSwissStagePredictionSettingsRequest):
    expected_version: SqliteInteger


class SaveSharedSwissResultRequest(SaveSwissStageSelectionRequest):
    expected_version: SqliteInteger


class CreateSharedMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home_team_id: SqliteInteger
    away_team_id: SqliteInteger
    starts_at_utc: str
    best_of: Literal[3, 5] | None = None
    round_key: (
        Literal["playoff", "round_of_16", "quarterfinal", "semifinal", "final"] | None
    ) = None
    bracket_position: SqliteInteger | None = Field(default=None, gt=0)


class UpdateSharedMatchStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at_utc: str
    expected_version: SqliteInteger


class SaveSharedMatchResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home_score: SqliteInteger = Field(
        description="Для футбольного матча — счёт хозяев строго после 90 минут."
    )
    away_score: SqliteInteger = Field(
        description="Для футбольного матча — счёт гостей строго после 90 минут."
    )
    advancing_team_id: SqliteInteger | None = Field(
        default=None,
        description=(
            "Победитель или прошедшая команда. Для футбольного матча хранится "
            "отдельно от счёта после 90 минут."
        ),
    )
    expected_version: SqliteInteger


class SaveSharedTwoLeggedTieResultRequest(SaveTwoLeggedTieResultRequest):
    expected_version: SqliteInteger

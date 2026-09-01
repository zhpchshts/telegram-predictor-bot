from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.access_control import (
    AccessDecision,
    AccessVerificationStatus,
    TelegramAdministratorsClient,
    TelegramAdministratorsSnapshot,
    determine_access,
)
from app.audit_read_service import (
    AuditCursorInvalidError,
    AuditDataIntegrityError,
    read_audit_events,
)
from app.audit_service import (
    AuditActor,
    AuditActorRole,
    AuditEntityType,
    AuditEventType,
)
from app.config import load_settings
from app.champions_league_bracket import (
    CHAMPIONS_LEAGUE_ROUNDS,
    ChampionsLeagueBracketConflictError,
    ChampionsLeagueBracketError,
    configure_bracket_node,
    configure_external_source,
    ensure_champions_league_bracket,
    get_champions_league_bracket,
    get_external_source,
    list_fixture_imports,
    mark_bracket_node_materialized,
    reconcile_downstream_bracket_nodes,
)
from app.champions_league_sync import (
    ChampionsLeagueSyncUnavailableError,
    synchronize_champions_league_tournament_once,
)
from app.chat_settings_service import get_chat_settings, save_chat_settings
from app.contest_service import (
    ChampionPredictionSettingsLockedError,
    ChampionUnavailableError,
    ContestCompletedError,
    ContestCompletionUnavailableError,
    ContestCreationConflictError,
    ContestNotFoundError,
    MatchCreationConflictError,
    MatchNotFoundError,
    MatchResultUnavailableError,
    MatchUpdateUnavailableError,
    PredictionUnavailableError,
    SwissStagePredictionSettingsLockedError,
    SwissStageResultUnavailableError,
    TournamentTeamsLockedError,
    TwoLeggedTieCreationConflictError,
    TwoLeggedTieNotFoundError,
    TwoLeggedTiePredictionUnavailableError,
    TwoLeggedTieResultUnavailableError,
    SharedTournamentManagedError,
    complete_contest,
    create_champions_league_2026_27_contest,
    create_match,
    create_two_legged_tie,
    create_the_international_2026_contest,
    create_world_cup_2026_contest,
    delete_contest,
    delete_match,
    delete_two_legged_tie,
    get_active_contests,
    get_completed_contests,
    get_contest_details,
    save_champion_prediction,
    save_champion_prediction_settings,
    save_contest_champion,
    save_match_prediction,
    save_match_prediction_publication_settings,
    save_match_result,
    save_two_legged_tie_prediction,
    save_two_legged_tie_result,
    save_swiss_stage_prediction,
    save_swiss_stage_prediction_settings,
    save_swiss_stage_result,
    save_tournament_teams,
    update_match_start,
)
from app.database import database_connection
from app.football_data_provider import FOOTBALL_DATA_SOURCE, FootballDataClient
from app.shared_tournament_service import (
    SharedMatchConflictError,
    SharedMatchNotFoundError,
    SharedMatchResultUnavailableError,
    SharedMatchUpdateUnavailableError,
    SharedTournamentCompletionUnavailableError,
    SharedTournamentConflictError,
    SharedTournamentLockedError,
    SharedTournamentNotFoundError,
    SharedTournamentResultUnavailableError,
    SharedTournamentSettingsLockedError,
    SharedTwoLeggedTieConflictError,
    SharedTwoLeggedTieNotFoundError,
    SharedTwoLeggedTieResultUnavailableError,
    archive_shared_tournament,
    create_shared_match,
    create_shared_two_legged_tie,
    create_shared_tournament,
    delete_shared_match,
    delete_shared_two_legged_tie,
    get_shared_tournament_details,
    list_shared_tournaments,
    restore_shared_tournament,
    save_shared_match_result,
    save_shared_two_legged_tie_result,
    save_shared_champion_result,
    save_shared_champion_settings,
    save_shared_swiss_result,
    save_shared_swiss_settings,
    save_shared_tournament_teams,
    update_shared_match_start,
)
from app.tma_context import TmaContext, TmaContextError, build_tma_context
from app.tma_entrypoint import create_tma_launch_keyboard
from app.supermoderator_service import (
    ActiveSupermoderatorAssignment,
    SupermoderatorAssignmentNotFoundError,
    assign_supermoderator_with_status,
    get_active_supermoderator_assignment,
    list_active_supermoderator_assignments,
    revoke_supermoderator,
)
from app.telegram_username_resolver import (
    TelegramUsernameResolver,
    UsernameFloodWaitError,
    UsernameInvalidError,
    UsernameNotFoundError,
    UsernameResolutionNotConfiguredError,
    UsernameResolutionUnavailableError,
    UsernameTargetNotSupportedError,
)
from app.prediction_reminders import (
    NoOpenPredictionRemindersError,
    PredictionReminderMessageTooLongError,
    TelegramPredictionReminderClient,
    publish_prediction_reminders,
)
from app.leaderboard_publications import (
    IntermediateLeaderboardUnavailableError,
    queue_intermediate_leaderboard_publication,
)
from app.user_service import (
    ChatActor,
    LocalUser,
    get_or_create_telegram_user,
    get_user_by_telegram_id,
    upsert_chat_actor,
    upsert_telegram_user,
)


TMA_INIT_DATA_HEADER = "X-Telegram-Init-Data"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
SQLITE_SIGNED_64_MIN = -(2**63)
SQLITE_SIGNED_64_MAX = 2**63 - 1
CONTEST_TEMPLATE_OPTIONS = (
    {
        "key": "world_cup_2026",
        "label": "Чемпионат мира 2026",
    },
    {
        "key": "the_international_2026",
        "label": "The International 2026",
    },
    {
        "key": "champions_league_2026_27",
        "label": "Лига чемпионов 2026/27",
    },
)
SUPPORTED_TEMPLATE_KEYS = frozenset(
    option["key"] for option in CONTEST_TEMPLATE_OPTIONS
)
# Completed seasonal templates remain supported for historical data. Only the
# current Champions League season is available for creating new contests.
CREATABLE_TEMPLATE_KEYS: frozenset[str] = frozenset({"champions_league_2026_27"})


def _reject_boolean_integer(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Boolean values are not valid integers.")
    return value


SqliteInteger = Annotated[
    int,
    BeforeValidator(_reject_boolean_integer),
    Field(ge=SQLITE_SIGNED_64_MIN, le=SQLITE_SIGNED_64_MAX),
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _attach_shared_fixture_to_champions_league_bracket(
    *,
    database_path: Path,
    shared_tournament_id: int,
    round_key: str | None,
    bracket_position: int | None,
    first_team_id: int,
    second_team_id: int,
    first_leg_starts_at_utc: str,
    second_leg_starts_at_utc: str | None,
    shared_tie_id: int | None = None,
    shared_match_id: int | None = None,
) -> None:
    if round_key is None or bracket_position is None:
        return
    bracket = ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
    )
    node = next(
        (
            candidate
            for candidate in bracket.nodes
            if candidate.round_key == round_key
            and candidate.bracket_position == bracket_position
        ),
        None,
    )
    if node is None:
        raise ChampionsLeagueBracketError("Позиция сетки не найдена.")
    configured = configure_bracket_node(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        round_key=node.round_key,
        bracket_position=node.bracket_position,
        first_source_node_id=node.first_source_node_id,
        second_source_node_id=node.second_source_node_id,
        resolved_first_team_id=first_team_id,
        resolved_second_team_id=second_team_id,
        first_leg_starts_at_utc=first_leg_starts_at_utc,
        second_leg_starts_at_utc=second_leg_starts_at_utc,
        expected_version=node.version,
    )
    if configured.action == "conflict":
        raise ChampionsLeagueBracketConflictError(
            configured.node.sync_error or "Позиция сетки уже изменена другой операцией."
        )
    materialized = mark_bracket_node_materialized(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        node_id=configured.node.id,
        expected_version=configured.node.version,
        shared_tie_id=shared_tie_id,
        shared_match_id=shared_match_id,
    )
    if (
        materialized.materialized_shared_tie_id != shared_tie_id
        or materialized.materialized_shared_match_id != shared_match_id
    ):
        raise ChampionsLeagueBracketConflictError(
            materialized.sync_error or "Позиция сетки уже занята другой фикстурой."
        )


def _select_shared_fixture_bracket_position(
    *,
    database_path: Path,
    shared_tournament_id: int,
    round_key: str,
    requested_position: int | None,
    node_format: str,
    first_team_id: int,
    second_team_id: int,
) -> int:
    bracket = ensure_champions_league_bracket(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
    )
    candidates = [
        node
        for node in bracket.nodes
        if node.round_key == round_key and node.node_format == node_format
    ]
    if not candidates:
        raise ChampionsLeagueBracketError(
            "Выбранный раунд не соответствует формату противостояния."
        )

    with database_connection(database_path) as connection:
        if node_format == "two_legged":
            rows = connection.execute(
                """
                SELECT bracket_position
                FROM shared_two_legged_ties
                WHERE shared_tournament_id = ? AND round_key = ?
                """,
                (shared_tournament_id, round_key),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT bracket_position
                FROM shared_matches
                WHERE shared_tournament_id = ? AND round_key = ?
                  AND shared_tie_id IS NULL
                """,
                (shared_tournament_id, round_key),
            ).fetchall()
    occupied_positions = {
        int(row["bracket_position"])
        for row in rows
        if row["bracket_position"] is not None
    }

    available = [
        node
        for node in candidates
        if node.bracket_position not in occupied_positions
        and node.materialized_shared_tie_id is None
        and node.materialized_shared_match_id is None
    ]
    if requested_position is not None:
        selected = next(
            (
                node
                for node in candidates
                if node.bracket_position == requested_position
            ),
            None,
        )
        if selected is None:
            raise ChampionsLeagueBracketError(
                "Такой позиции нет в канонической сетке выбранного раунда."
            )
        if selected not in available:
            raise ChampionsLeagueBracketConflictError("Позиция в сетке уже занята.")
        return selected.bracket_position

    requested_teams = {first_team_id, second_team_id}
    matching = [
        node
        for node in available
        if None not in {node.resolved_first_team_id, node.resolved_second_team_id}
        and {node.resolved_first_team_id, node.resolved_second_team_id}
        == requested_teams
    ]
    selected = next(iter(matching or available), None)
    if selected is None:
        raise ChampionsLeagueBracketConflictError(
            "Все позиции выбранного раунда уже заняты."
        )
    return selected.bracket_position


def _reconcile_shared_fixture_downstream(
    *,
    database_path: Path,
    shared_tournament_id: int,
    round_key: str | None,
    shared_tie_id: int | None = None,
    shared_match_id: int | None = None,
) -> dict[str, object] | None:
    if round_key is None:
        return None
    try:
        bracket = get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=shared_tournament_id,
        )
    except ChampionsLeagueBracketError:
        return None
    source_node = next(
        (
            node
            for node in bracket.nodes
            if (
                shared_tie_id is not None
                and node.materialized_shared_tie_id == shared_tie_id
            )
            or (
                shared_match_id is not None
                and node.materialized_shared_match_id == shared_match_id
            )
        ),
        None,
    )
    if source_node is None:
        return None
    try:
        reconciliations = reconcile_downstream_bracket_nodes(
            database_path=database_path,
            shared_tournament_id=shared_tournament_id,
            source_node_id=source_node.id,
        )
    except ChampionsLeagueBracketError as error:
        return {"state": "needs_attention", "error": str(error), "nodes": []}
    serialized = [
        {
            "round_key": reconciliation.node.round_key,
            "position": reconciliation.node.bracket_position,
            "action": reconciliation.action,
            "state": reconciliation.node.sync_status,
            "error": reconciliation.node.sync_error,
        }
        for reconciliation in reconciliations
    ]
    return {
        "state": (
            "needs_attention"
            if any(item["action"] == "conflict" for item in serialized)
            else "updated"
        ),
        "nodes": serialized,
    }


def _rollback_unattached_shared_match(
    *, database_path: Path, shared_tournament_id: int, match, management
) -> None:
    delete_shared_match(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        shared_match_id=match.id,
        expected_version=match.version,
        actor_telegram_user_id=management.user.telegram_user_id,
        actor_first_name=management.user.first_name,
        actor_last_name=management.user.last_name,
        actor_username=management.user.username,
        now_utc=_utc_now(),
    )


def _rollback_unattached_shared_tie(
    *, database_path: Path, shared_tournament_id: int, tie, management
) -> None:
    delete_shared_two_legged_tie(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        shared_tie_id=tie.id,
        expected_version=tie.version,
        actor_telegram_user_id=management.user.telegram_user_id,
        actor_first_name=management.user.first_name,
        actor_last_name=management.user.last_name,
        actor_username=management.user.username,
        now_utc=_utc_now(),
    )


def _serialize_creatable_template_options() -> list[dict[str, str]]:
    return [
        {"key": option["key"], "label": option["label"]}
        for option in CONTEST_TEMPLATE_OPTIONS
        if option["key"] in CREATABLE_TEMPLATE_KEYS
    ]


def _require_creatable_template(template_key: str) -> None:
    if template_key not in SUPPORTED_TEMPLATE_KEYS:
        raise _application_http_error(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="template_unknown",
            message="Неизвестный шаблон турнира.",
        )
    if template_key not in CREATABLE_TEMPLATE_KEYS:
        raise _application_http_error(
            status_code=status.HTTP_409_CONFLICT,
            code="template_unavailable",
            message="Выбранный шаблон больше недоступен для создания.",
        )


def _audit_actor(context: TmaContext, access: AccessDecision) -> AuditActor:
    if access.role is None:
        raise RuntimeError("Allowed administrative action has no effective role.")
    return AuditActor(
        telegram_chat_id=context.chat.telegram_chat_id,
        telegram_user_id=context.user.telegram_user_id,
        role=AuditActorRole(access.role.value),
    )


router = APIRouter(
    prefix="/api/tma",
    tags=["tma"],
)


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


class TelegramUserIdInvalidError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RoleManagementContext:
    context: TmaContext
    access: AccessDecision
    administrators: TelegramAdministratorsSnapshot
    actor: ChatActor


@dataclass(frozen=True, slots=True)
class ContestManagementContext:
    context: TmaContext
    access: AccessDecision

    @property
    def user(self):
        return self.context.user

    @property
    def chat(self):
        return self.context.chat


@dataclass(frozen=True, slots=True)
class SharedTournamentManagementContext:
    context: TmaContext

    @property
    def user(self):
        return self.context.user


def get_telegram_administrators_client(
    request: Request,
) -> TelegramAdministratorsClient:
    try:
        return request.app.state.telegram_bot
    except AttributeError as error:
        raise RuntimeError(
            "Telegram bot is unavailable outside application lifespan."
        ) from error


def get_telegram_prediction_reminder_client(
    request: Request,
) -> TelegramPredictionReminderClient:
    try:
        return request.app.state.telegram_bot
    except AttributeError as error:
        raise RuntimeError(
            "Telegram bot is unavailable outside application lifespan."
        ) from error


def get_telegram_username_resolver(request: Request) -> TelegramUsernameResolver:
    try:
        return request.app.state.telegram_username_resolver
    except AttributeError as error:
        raise RuntimeError(
            "Telegram username resolver is unavailable outside application lifespan."
        ) from error


async def _authorize_contest_management(
    telegram_client: Annotated[
        TelegramAdministratorsClient,
        Depends(get_telegram_administrators_client),
    ],
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> ContestManagementContext:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    access = await determine_access(
        database_path=settings.database_path,
        telegram_chat_id=context.chat.telegram_chat_id,
        telegram_user_id=context.user.telegram_user_id,
        telegram_client=telegram_client,
        telegram_timeout_seconds=settings.telegram_admin_check_timeout_seconds,
    )
    if access.can_manage_contests:
        return ContestManagementContext(context=context, access=access)
    if access.verification_status is AccessVerificationStatus.UNAVAILABLE:
        raise _application_http_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="contest_management_verification_unavailable",
            message=(
                "Не удалось проверить права администратора Telegram. "
                "Управление конкурсами временно недоступно."
            ),
        )
    raise _application_http_error(
        status_code=status.HTTP_403_FORBIDDEN,
        code="contest_management_forbidden",
        message=(
            "Управлять конкурсами могут только администраторы чата и супермодераторы."
        ),
    )


async def _authorize_shared_tournament_management(
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> SharedTournamentManagementContext:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    if context.user.telegram_user_id not in settings.shared_tournament_admin_ids:
        raise _application_http_error(
            status_code=status.HTTP_403_FORBIDDEN,
            code="shared_tournament_management_forbidden",
            message="Управление общими турнирами недоступно.",
        )
    return SharedTournamentManagementContext(context=context)


async def _authorize_role_management(
    telegram_client: Annotated[
        TelegramAdministratorsClient,
        Depends(get_telegram_administrators_client),
    ],
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> RoleManagementContext:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    access = await determine_access(
        database_path=settings.database_path,
        telegram_chat_id=context.chat.telegram_chat_id,
        telegram_user_id=context.user.telegram_user_id,
        telegram_client=telegram_client,
        telegram_timeout_seconds=settings.telegram_admin_check_timeout_seconds,
    )
    if not access.can_manage_roles:
        if access.verification_status is AccessVerificationStatus.UNAVAILABLE:
            raise _application_http_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="telegram_admin_verification_unavailable",
                message=(
                    "Не удалось подтвердить права администратора Telegram. "
                    "Попробуйте ещё раз позже."
                ),
            )
        raise _application_http_error(
            status_code=status.HTTP_403_FORBIDDEN,
            code="telegram_admin_required",
            message="Управлять супермодераторами может только администратор Telegram.",
        )

    administrators = access.administrators
    if administrators is None:
        raise RuntimeError("Verified role management access has no administrator data.")
    actor = upsert_chat_actor(
        database_path=settings.database_path,
        telegram_chat_id=context.chat.telegram_chat_id,
        chat_title=context.chat.title,
        telegram_user_id=context.user.telegram_user_id,
        username=context.user.username,
        first_name=context.user.first_name,
        last_name=context.user.last_name,
    )
    return RoleManagementContext(
        context=context,
        access=access,
        administrators=administrators,
        actor=actor,
    )


@router.get("/bootstrap")
async def get_tma_bootstrap(
    telegram_client: Annotated[
        TelegramAdministratorsClient,
        Depends(get_telegram_administrators_client),
    ],
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> dict[str, object]:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    active_contests = get_active_contests(
        database_path=settings.database_path,
        telegram_chat_id=context.chat.telegram_chat_id,
    )
    completed_contests = get_completed_contests(
        database_path=settings.database_path,
        telegram_chat_id=context.chat.telegram_chat_id,
    )
    access = await determine_access(
        database_path=settings.database_path,
        telegram_chat_id=context.chat.telegram_chat_id,
        telegram_user_id=context.user.telegram_user_id,
        telegram_client=telegram_client,
        telegram_timeout_seconds=settings.telegram_admin_check_timeout_seconds,
    )
    return {
        "context": _serialize_context(context),
        "access": _serialize_access(access),
        "active_contests": [
            _serialize_active_contest(contest) for contest in active_contests
        ],
        "completed_contests": [
            _serialize_active_contest(contest) for contest in completed_contests
        ],
    }


@router.get("/management/contests")
async def get_tma_management_contests(
    management: Annotated[
        ContestManagementContext,
        Depends(_authorize_contest_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    active_contests = get_active_contests(
        database_path=settings.database_path,
        telegram_chat_id=management.chat.telegram_chat_id,
    )
    completed_contests = get_completed_contests(
        database_path=settings.database_path,
        telegram_chat_id=management.chat.telegram_chat_id,
    )
    chat_settings = get_chat_settings(
        database_path=settings.database_path,
        telegram_chat_id=management.chat.telegram_chat_id,
    )
    return {
        "contests": [
            _serialize_management_contest(
                contest,
                status_value="active",
            )
            for contest in active_contests
        ]
        + [
            _serialize_management_contest(
                contest,
                status_value="completed",
            )
            for contest in completed_contests
        ],
        "capabilities": {
            "can_create_contests": management.access.can_manage_contests,
            "can_manage_roles": management.access.can_manage_roles,
            "can_read_audit": management.access.can_manage_contests,
            "can_manage_chat_settings": management.access.can_manage_contests,
            "can_manage_shared_tournaments": (
                management.user.telegram_user_id in settings.shared_tournament_admin_ids
            ),
        },
        "shared_tournaments": [
            _serialize_shared_tournament_summary(tournament)
            for tournament in list_shared_tournaments(
                database_path=settings.database_path
            )
            if not tournament.is_archived
        ],
        "contest_templates": _serialize_creatable_template_options(),
        "chat_settings": {
            "app_button_text": chat_settings.app_button_text,
        },
    }


@router.put("/management/chat-settings")
async def update_tma_chat_settings(
    payload: SaveChatSettingsRequest,
    management: Annotated[
        ContestManagementContext,
        Depends(_authorize_contest_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    upsert_chat_actor(
        database_path=settings.database_path,
        telegram_chat_id=management.chat.telegram_chat_id,
        chat_title=management.chat.title,
        telegram_user_id=management.user.telegram_user_id,
        username=management.user.username,
        first_name=management.user.first_name,
        last_name=management.user.last_name,
    )
    try:
        chat_settings = save_chat_settings(
            database_path=settings.database_path,
            telegram_chat_id=management.chat.telegram_chat_id,
            app_button_text=payload.app_button_text,
            actor=_audit_actor(management.context, management.access),
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return {
        "chat_settings": {
            "app_button_text": chat_settings.app_button_text,
        }
    }


@router.get("/management/contests/{contest_id}")
async def get_tma_management_contest(
    contest_id: SqliteInteger,
    management: Annotated[
        ContestManagementContext,
        Depends(_authorize_contest_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=management.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=management.user.telegram_user_id,
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    return {
        "contest": _serialize_contest_details(
            contest, database_path=settings.database_path
        )
    }


@router.get("/access/supermoderators")
async def get_tma_supermoderators(
    management: Annotated[RoleManagementContext, Depends(_authorize_role_management)],
) -> dict[str, object]:
    settings = load_settings()
    assignments = list_active_supermoderator_assignments(
        database_path=settings.database_path,
        chat_id=management.actor.chat_id,
    )
    assignments.sort(
        key=lambda item: (
            not management.administrators.contains(item.user.telegram_user_id),
            _user_display_name(item.user).casefold(),
            item.user.telegram_user_id,
        )
    )
    return {
        "assignments": [
            _serialize_active_assignment(item, management.administrators)
            for item in assignments
        ]
    }


@router.get("/audit-events")
async def get_tma_audit_events(
    management: Annotated[
        ContestManagementContext,
        Depends(_authorize_contest_management),
    ],
    contest_id: Annotated[
        int | None,
        Query(gt=0, le=SQLITE_SIGNED_64_MAX),
    ] = None,
    event_type: AuditEventType | None = None,
    entity_type: AuditEntityType | None = None,
    actor_user_id: Annotated[
        int | None,
        Query(gt=0, le=SQLITE_SIGNED_64_MAX),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
) -> dict[str, object]:
    settings = load_settings()
    try:
        page = read_audit_events(
            database_path=settings.database_path,
            telegram_chat_id=management.chat.telegram_chat_id,
            contest_id=contest_id,
            event_type=event_type,
            entity_type=entity_type,
            actor_user_id=actor_user_id,
            cursor=cursor,
            limit=limit,
        )
    except AuditCursorInvalidError as error:
        raise _application_http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="audit_cursor_invalid",
            message="Курсор истории действий некорректен.",
        ) from error
    except AuditDataIntegrityError as error:
        raise _application_http_error(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="audit_data_invalid",
            message=(
                "Не удалось прочитать одну из записей истории действий. "
                "Попробуйте ещё раз позже."
            ),
        ) from error
    return {
        "events": page.events,
        "next_cursor": page.next_cursor,
        "filter_options": {
            "contests": page.contest_options,
            "actors": page.actor_options,
        },
    }


@router.post("/access/users/resolve")
async def resolve_tma_role_target(
    payload: ResolveRoleTargetRequest,
    username_resolver: Annotated[
        TelegramUsernameResolver,
        Depends(get_telegram_username_resolver),
    ],
    management: Annotated[RoleManagementContext, Depends(_authorize_role_management)],
) -> dict[str, object]:
    target = payload.target.strip()
    if not target:
        raise _application_http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="role_target_invalid",
            message="Укажите положительный Telegram ID или точный username.",
        )
    settings = load_settings()
    try:
        telegram_user_id = _parse_telegram_user_id_target(target)
    except TelegramUserIdInvalidError as error:
        raise _application_http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="telegram_user_id_invalid",
            message="Telegram ID должен быть положительным целым числом.",
        ) from error

    if telegram_user_id is not None:
        user = get_or_create_telegram_user(
            database_path=settings.database_path,
            telegram_user_id=telegram_user_id,
        )
    else:
        try:
            resolved_user = await username_resolver.resolve_username(target)
        except UsernameInvalidError as error:
            raise _application_http_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="username_invalid",
                message="Некорректный формат Telegram username.",
            ) from error
        except UsernameNotFoundError as error:
            raise _application_http_error(
                status_code=status.HTTP_404_NOT_FOUND,
                code="username_not_found",
                message="Пользователь с таким username не найден.",
            ) from error
        except UsernameTargetNotSupportedError as error:
            raise _application_http_error(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="username_target_not_supported",
                message=(
                    "Супермодератором можно назначить только обычного "
                    "пользователя Telegram."
                ),
            ) from error
        except UsernameFloodWaitError as error:
            raise _application_http_error(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="telegram_flood_wait",
                message="Telegram временно ограничил поиск. Попробуйте ещё раз позже.",
                headers={"Retry-After": str(error.retry_after)},
            ) from error
        except UsernameResolutionNotConfiguredError as error:
            raise _application_http_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="username_resolution_not_configured",
                message=(
                    "Поиск по username не настроен. "
                    "Укажите Telegram ID или обратитесь к администратору."
                ),
            ) from error
        except UsernameResolutionUnavailableError as error:
            raise _application_http_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="username_resolution_unavailable",
                message=(
                    "Не удалось найти пользователя в Telegram. "
                    "Попробуйте ещё раз позже."
                ),
            ) from error
        user = upsert_telegram_user(
            database_path=settings.database_path,
            telegram_user_id=resolved_user.telegram_user_id,
            username=resolved_user.username,
            first_name=resolved_user.first_name,
            last_name=resolved_user.last_name,
        )
    assignment = get_active_supermoderator_assignment(
        database_path=settings.database_path,
        chat_id=management.actor.chat_id,
        user_id=user.id,
    )
    return {
        "user": _serialize_user(user),
        "has_active_assignment": assignment is not None,
    }


@router.put("/access/supermoderators/{telegram_user_id}")
async def assign_tma_supermoderator(
    telegram_user_id: SqliteInteger,
    management: Annotated[RoleManagementContext, Depends(_authorize_role_management)],
) -> dict[str, object]:
    settings = load_settings()
    if telegram_user_id <= 0:
        raise _application_http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="telegram_user_id_invalid",
            message="Telegram ID должен быть положительным целым числом.",
        )
    user = get_or_create_telegram_user(
        database_path=settings.database_path,
        telegram_user_id=telegram_user_id,
    )
    result = assign_supermoderator_with_status(
        database_path=settings.database_path,
        chat_id=management.actor.chat_id,
        user_id=user.id,
        assigned_by_user_id=management.actor.actor_user_id,
        audit_actor=_audit_actor(management.context, management.access),
    )
    return {
        "created": result.was_created,
        "assignment": {
            **_serialize_assignment(result.assignment),
            "user": _serialize_user(user),
        },
    }


@router.delete("/access/supermoderators/{telegram_user_id}")
async def revoke_tma_supermoderator(
    telegram_user_id: SqliteInteger,
    management: Annotated[RoleManagementContext, Depends(_authorize_role_management)],
) -> dict[str, object]:
    settings = load_settings()
    if telegram_user_id <= 0:
        raise _application_http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="telegram_user_id_invalid",
            message="Telegram ID должен быть положительным целым числом.",
        )
    user = get_user_by_telegram_id(
        database_path=settings.database_path,
        telegram_user_id=telegram_user_id,
    )
    if user is None:
        raise _application_http_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="active_assignment_not_found",
            message="Активное назначение не найдено.",
        )
    try:
        assignment = revoke_supermoderator(
            database_path=settings.database_path,
            chat_id=management.actor.chat_id,
            user_id=user.id,
            revoked_by_user_id=management.actor.actor_user_id,
            audit_actor=_audit_actor(management.context, management.access),
        )
    except SupermoderatorAssignmentNotFoundError as error:
        raise _application_http_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="active_assignment_not_found",
            message="Активное назначение не найдено.",
        ) from error
    return {"assignment": _serialize_assignment(assignment)}


@router.post("/contests")
async def create_tma_contest(
    payload: CreateContestRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER),
    ] = None,
) -> JSONResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не передан ключ идемпотентности создания конкурса.",
        )

    _require_creatable_template(payload.template_key)
    settings = load_settings()
    try:
        if payload.template_key == "world_cup_2026":
            create_contest = create_world_cup_2026_contest
        elif payload.template_key == "the_international_2026":
            create_contest = create_the_international_2026_contest
        elif payload.template_key == "champions_league_2026_27":
            create_contest = create_champions_league_2026_27_contest
        else:  # pragma: no cover - guarded by _require_creatable_template
            raise RuntimeError("Unsupported creatable contest template.")
        result = create_contest(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            chat_title=context.chat.title,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            contest_name=payload.name,
            idempotency_key=idempotency_key,
            audit_actor=_audit_actor(context.context, context.access),
            shared_tournament_id=payload.shared_tournament_id,
        )
    except (ContestCreationConflictError, SharedTournamentConflictError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except SharedTournamentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (ValueError, SharedTournamentLockedError) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    response_status = (
        status.HTTP_201_CREATED if result.was_created else status.HTTP_200_OK
    )
    return JSONResponse(
        status_code=response_status,
        content={
            "contest": _serialize_active_contest(result.contest),
            "was_created": result.was_created,
        },
    )


@router.get("/shared-tournaments")
async def get_tma_shared_tournaments(
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    _ = management
    settings = load_settings()
    return {
        "shared_tournaments": [
            _serialize_shared_tournament_summary(tournament)
            for tournament in list_shared_tournaments(
                database_path=settings.database_path
            )
        ],
        "contest_templates": _serialize_creatable_template_options(),
    }


@router.post("/shared-tournaments", status_code=status.HTTP_201_CREATED)
async def create_tma_shared_tournament(
    payload: CreateSharedTournamentRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    _require_creatable_template(payload.template_key)
    settings = load_settings()
    try:
        details = create_shared_tournament(
            database_path=settings.database_path,
            name=payload.name,
            template_key=payload.template_key,
            actor_telegram_user_id=management.user.telegram_user_id,
        )
    except SharedTournamentConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    if details.tournament.template_key == "champions_league_2026_27":
        ensure_champions_league_bracket(
            database_path=settings.database_path,
            shared_tournament_id=details.tournament.id,
        )
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.get("/shared-tournaments/{shared_tournament_id}")
async def get_tma_shared_tournament(
    shared_tournament_id: SqliteInteger,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    _ = management
    settings = load_settings()
    try:
        details = get_shared_tournament_details(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.put("/shared-tournaments/{shared_tournament_id}/fixture-sync")
async def save_tma_shared_fixture_sync(
    shared_tournament_id: SqliteInteger,
    payload: SaveSharedFixtureSyncRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    _ = management
    settings = load_settings()
    try:
        ensure_champions_league_bracket(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
        )
        source = get_external_source(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            source=FOOTBALL_DATA_SOURCE,
        )
        configure_external_source(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            source=FOOTBALL_DATA_SOURCE,
            external_event_id=f"CL:{settings.champions_league_sync_season}",
            sync_enabled=payload.enabled,
            expected_version=source.version if source is not None else None,
            expected_tournament_version=payload.expected_version,
            now_utc=_utc_now(),
        )
        details = get_shared_tournament_details(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    except (ChampionsLeagueBracketError, SharedTournamentConflictError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.post("/shared-tournaments/{shared_tournament_id}/fixture-sync/run")
async def run_tma_shared_fixture_sync(
    shared_tournament_id: SqliteInteger,
    payload: SharedTournamentVersionRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    _ = management
    settings = load_settings()
    try:
        details_before = get_shared_tournament_details(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    if details_before.tournament.version != payload.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Общий турнир уже изменился; перечитайте данные и повторите действие.",
        )
    if details_before.tournament.template_key != "champions_league_2026_27":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Автоматическая сетка доступна только для Лиги чемпионов.",
        )
    if settings.football_data_api_token is None:
        raise _application_http_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="fixture_sync_token_unavailable",
            message="На сервере не настроен токен football-data.org.",
        )

    client = FootballDataClient(
        token=settings.football_data_api_token,
        season_start_year=settings.champions_league_sync_season,
    )
    try:
        sync_result = await synchronize_champions_league_tournament_once(
            database_path=settings.database_path,
            client=client,
            shared_tournament_id=shared_tournament_id,
            season_start_year=settings.champions_league_sync_season,
            now_utc=_utc_now(),
        )
    except ChampionsLeagueSyncUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    details = get_shared_tournament_details(
        database_path=settings.database_path,
        shared_tournament_id=shared_tournament_id,
    )
    response = {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings),
        "sync_result": {
            "fixtures_fetched": sync_result.fetched,
            "ties_created": sync_result.created_tie_count,
            "matches_created": sync_result.created_match_count,
            "matches_updated": sync_result.updated_start_count,
            "results_saved": sync_result.saved_result_count,
            "conflicts": (
                sync_result.conflict_count + sync_result.provider_conflict_count
            ),
        },
    }
    if sync_result.failed:
        raise _application_http_error(
            status_code=status.HTTP_502_BAD_GATEWAY,
            code="fixture_sync_failed",
            message=(
                sync_result.provider_error
                or "Не удалось синхронизировать расписание и результаты."
            ),
        )
    return response


@router.post("/shared-tournaments/{shared_tournament_id}/archive")
async def archive_tma_shared_tournament(
    shared_tournament_id: SqliteInteger,
    payload: SharedTournamentVersionRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        details = archive_shared_tournament(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            now_utc=_utc_now(),
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (
        SharedTournamentCompletionUnavailableError,
        SharedTournamentConflictError,
        SharedTournamentLockedError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.post("/shared-tournaments/{shared_tournament_id}/restore")
async def restore_tma_shared_tournament(
    shared_tournament_id: SqliteInteger,
    payload: SharedTournamentVersionRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        details = restore_shared_tournament(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except SharedTournamentConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.put("/shared-tournaments/{shared_tournament_id}/teams")
async def save_tma_shared_tournament_teams(
    shared_tournament_id: SqliteInteger,
    payload: SaveSharedTournamentTeamsRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        details = save_shared_tournament_teams(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            team_names=payload.team_names,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    except (SharedTournamentConflictError, SharedTournamentLockedError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.put("/shared-tournaments/{shared_tournament_id}/champion-prediction/settings")
async def save_tma_shared_champion_settings(
    shared_tournament_id: SqliteInteger,
    payload: SaveSharedChampionSettingsRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        details = save_shared_champion_settings(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            enabled=payload.enabled,
            deadline_at=payload.deadline_at,
            points=payload.points,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            actor_first_name=management.user.first_name,
            actor_last_name=management.user.last_name,
            actor_username=management.user.username,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (
        SharedTournamentConflictError,
        SharedTournamentLockedError,
        SharedTournamentSettingsLockedError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.put("/shared-tournaments/{shared_tournament_id}/champion")
async def save_tma_shared_champion_result(
    shared_tournament_id: SqliteInteger,
    payload: SaveSharedChampionResultRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        details = save_shared_champion_result(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            champion_team_id=payload.champion_team_id,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            actor_first_name=management.user.first_name,
            actor_last_name=management.user.last_name,
            actor_username=management.user.username,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (
        SharedTournamentConflictError,
        SharedTournamentLockedError,
        SharedTournamentResultUnavailableError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.put("/shared-tournaments/{shared_tournament_id}/swiss-stage/settings")
async def save_tma_shared_swiss_settings(
    shared_tournament_id: SqliteInteger,
    payload: SaveSharedSwissSettingsRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        details = save_shared_swiss_settings(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            enabled=payload.enabled,
            deadline_at=payload.deadline_at,
            direct_qualifier_count=payload.direct_qualifier_count,
            elimination_qualifier_count=payload.elimination_qualifier_count,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            actor_first_name=management.user.first_name,
            actor_last_name=management.user.last_name,
            actor_username=management.user.username,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (
        SharedTournamentConflictError,
        SharedTournamentLockedError,
        SharedTournamentSettingsLockedError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.put("/shared-tournaments/{shared_tournament_id}/swiss-stage/result")
async def save_tma_shared_swiss_result(
    shared_tournament_id: SqliteInteger,
    payload: SaveSharedSwissResultRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        details = save_shared_swiss_result(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            direct_team_ids=payload.direct_team_ids,
            elimination_team_ids=payload.elimination_team_ids,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            actor_first_name=management.user.first_name,
            actor_last_name=management.user.last_name,
            actor_username=management.user.username,
        )
    except SharedTournamentNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (
        SharedTournamentConflictError,
        SharedTournamentLockedError,
        SharedTournamentResultUnavailableError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "shared_tournament": _serialize_shared_tournament_for_api(details, settings)
    }


@router.post(
    "/shared-tournaments/{shared_tournament_id}/matches",
    status_code=status.HTTP_201_CREATED,
)
async def create_tma_shared_match(
    shared_tournament_id: SqliteInteger,
    payload: CreateSharedMatchRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    if payload.bracket_position is not None and payload.round_key is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Для позиции сетки укажите раунд.",
        )
    try:
        bracket_position = payload.bracket_position
        if payload.round_key is not None:
            bracket_position = _select_shared_fixture_bracket_position(
                database_path=settings.database_path,
                shared_tournament_id=shared_tournament_id,
                round_key=payload.round_key,
                requested_position=payload.bracket_position,
                node_format="single",
                first_team_id=payload.home_team_id,
                second_team_id=payload.away_team_id,
            )
        match = create_shared_match(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            home_team_id=payload.home_team_id,
            away_team_id=payload.away_team_id,
            starts_at_utc=payload.starts_at_utc,
            best_of=payload.best_of,
            round_key=payload.round_key,
            bracket_position=bracket_position,
            actor_telegram_user_id=management.user.telegram_user_id,
        )
    except (SharedTournamentNotFoundError, SharedMatchNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    except (
        ChampionsLeagueBracketError,
        SharedTournamentLockedError,
        SharedMatchConflictError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    if match.round_key is not None:
        try:
            _attach_shared_fixture_to_champions_league_bracket(
                database_path=settings.database_path,
                shared_tournament_id=shared_tournament_id,
                round_key=match.round_key,
                bracket_position=match.bracket_position,
                first_team_id=match.home_team.id,
                second_team_id=match.away_team.id,
                first_leg_starts_at_utc=match.starts_at_utc,
                second_leg_starts_at_utc=None,
                shared_match_id=match.id,
            )
        except (
            ChampionsLeagueBracketError,
            ChampionsLeagueBracketConflictError,
        ) as error:
            try:
                _rollback_unattached_shared_match(
                    database_path=settings.database_path,
                    shared_tournament_id=shared_tournament_id,
                    match=match,
                    management=management,
                )
            except Exception as cleanup_error:  # pragma: no cover - defensive fail-safe
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        "Матч создан, но не удалось безопасно привязать или "
                        "откатить позицию сетки. Требуется ручная проверка."
                    ),
                ) from cleanup_error
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
    return {"match": _serialize_shared_match(match)}


@router.post(
    "/shared-tournaments/{shared_tournament_id}/two-legged-ties",
    status_code=status.HTTP_201_CREATED,
)
async def create_tma_shared_two_legged_tie(
    shared_tournament_id: SqliteInteger,
    payload: CreateSharedTwoLeggedTieRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    if payload.bracket_position is not None and payload.round_key is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Для позиции сетки укажите раунд.",
        )
    try:
        bracket_position = payload.bracket_position
        if payload.round_key is not None:
            bracket_position = _select_shared_fixture_bracket_position(
                database_path=settings.database_path,
                shared_tournament_id=shared_tournament_id,
                round_key=payload.round_key,
                requested_position=payload.bracket_position,
                node_format="two_legged",
                first_team_id=payload.first_team_id,
                second_team_id=payload.second_team_id,
            )
        tie = create_shared_two_legged_tie(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            first_team_id=payload.first_team_id,
            second_team_id=payload.second_team_id,
            first_leg_starts_at_utc=payload.first_leg_starts_at_utc,
            second_leg_starts_at_utc=payload.second_leg_starts_at_utc,
            actor_telegram_user_id=management.user.telegram_user_id,
            now_utc=_utc_now(),
            round_key=payload.round_key,
            bracket_position=bracket_position,
        )
    except (
        SharedTournamentNotFoundError,
        SharedTwoLeggedTieNotFoundError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ChampionsLeagueBracketError,
        SharedTournamentLockedError,
        SharedTwoLeggedTieConflictError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    if tie.round_key is not None:
        try:
            _attach_shared_fixture_to_champions_league_bracket(
                database_path=settings.database_path,
                shared_tournament_id=shared_tournament_id,
                round_key=tie.round_key,
                bracket_position=tie.bracket_position,
                first_team_id=tie.first_team.id,
                second_team_id=tie.second_team.id,
                first_leg_starts_at_utc=tie.first_leg.starts_at_utc,
                second_leg_starts_at_utc=tie.second_leg.starts_at_utc,
                shared_tie_id=tie.id,
            )
        except (
            ChampionsLeagueBracketError,
            ChampionsLeagueBracketConflictError,
        ) as error:
            try:
                _rollback_unattached_shared_tie(
                    database_path=settings.database_path,
                    shared_tournament_id=shared_tournament_id,
                    tie=tie,
                    management=management,
                )
            except Exception as cleanup_error:  # pragma: no cover - defensive fail-safe
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        "Пара создана, но не удалось безопасно привязать или "
                        "откатить позицию сетки. Требуется ручная проверка."
                    ),
                ) from cleanup_error
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
    return {"two_legged_tie": _serialize_shared_two_legged_tie(tie)}


@router.put("/shared-tournaments/{shared_tournament_id}/matches/{shared_match_id}")
async def update_tma_shared_match_start(
    shared_tournament_id: SqliteInteger,
    shared_match_id: SqliteInteger,
    payload: UpdateSharedMatchStartRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        match = update_shared_match_start(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
            starts_at_utc=payload.starts_at_utc,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
        )
    except (SharedTournamentNotFoundError, SharedMatchNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    except (
        SharedTournamentLockedError,
        SharedMatchConflictError,
        SharedMatchUpdateUnavailableError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    return {"match": _serialize_shared_match(match)}


@router.put(
    "/shared-tournaments/{shared_tournament_id}/matches/{shared_match_id}/result"
)
async def save_tma_shared_match_result(
    shared_tournament_id: SqliteInteger,
    shared_match_id: SqliteInteger,
    payload: SaveSharedMatchResultRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        match = save_shared_match_result(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
            home_score=payload.home_score,
            away_score=payload.away_score,
            advancing_team_id=payload.advancing_team_id,
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            actor_first_name=management.user.first_name,
            actor_last_name=management.user.last_name,
            actor_username=management.user.username,
            now_utc=_utc_now(),
        )
    except (SharedTournamentNotFoundError, SharedMatchNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    except (
        SharedTournamentLockedError,
        SharedMatchConflictError,
        SharedMatchResultUnavailableError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    reconciliation = _reconcile_shared_fixture_downstream(
        database_path=settings.database_path,
        shared_tournament_id=shared_tournament_id,
        round_key=match.round_key,
        shared_tie_id=match.shared_tie_id,
        shared_match_id=(match.id if match.shared_tie_id is None else None),
    )
    return {
        "match": _serialize_shared_match(match),
        **(
            {"bracket_reconciliation": reconciliation}
            if reconciliation is not None
            else {}
        ),
    }


@router.put(
    "/shared-tournaments/{shared_tournament_id}/two-legged-ties/{shared_tie_id}/result"
)
async def save_tma_shared_two_legged_tie_result(
    shared_tournament_id: SqliteInteger,
    shared_tie_id: SqliteInteger,
    payload: SaveSharedTwoLeggedTieResultRequest,
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        tie = save_shared_two_legged_tie_result(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            shared_tie_id=shared_tie_id,
            advancing_team_id=payload.advancing_team_id,
            second_leg_extra_time_home_score=(payload.second_leg_extra_time_home_score),
            second_leg_extra_time_away_score=(payload.second_leg_extra_time_away_score),
            second_leg_home_penalty_score=(payload.second_leg_home_penalty_score),
            second_leg_away_penalty_score=(payload.second_leg_away_penalty_score),
            expected_version=payload.expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            actor_first_name=management.user.first_name,
            actor_last_name=management.user.last_name,
            actor_username=management.user.username,
            now_utc=_utc_now(),
        )
    except (
        SharedTournamentNotFoundError,
        SharedTwoLeggedTieNotFoundError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        SharedTournamentLockedError,
        SharedTwoLeggedTieConflictError,
        SharedTwoLeggedTieResultUnavailableError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    reconciliation = _reconcile_shared_fixture_downstream(
        database_path=settings.database_path,
        shared_tournament_id=shared_tournament_id,
        round_key=tie.round_key,
        shared_tie_id=tie.id,
    )
    return {
        "two_legged_tie": _serialize_shared_two_legged_tie(tie),
        **(
            {"bracket_reconciliation": reconciliation}
            if reconciliation is not None
            else {}
        ),
    }


@router.delete("/shared-tournaments/{shared_tournament_id}/matches/{shared_match_id}")
async def delete_tma_shared_match(
    shared_tournament_id: SqliteInteger,
    shared_match_id: SqliteInteger,
    expected_version: Annotated[int, Query(gt=0, le=SQLITE_SIGNED_64_MAX)],
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        result = delete_shared_match(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            shared_match_id=shared_match_id,
            expected_version=expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            actor_first_name=management.user.first_name,
            actor_last_name=management.user.last_name,
            actor_username=management.user.username,
        )
    except (SharedTournamentNotFoundError, SharedMatchNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error
    except (
        SharedTournamentLockedError,
        SharedMatchConflictError,
        SharedMatchUpdateUnavailableError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    return {
        "deleted": True,
        "linked_contest_count": result.linked_contest_count,
        "deleted_prediction_count": result.deleted_prediction_count,
    }


@router.delete(
    "/shared-tournaments/{shared_tournament_id}/two-legged-ties/{shared_tie_id}"
)
async def delete_tma_shared_two_legged_tie(
    shared_tournament_id: SqliteInteger,
    shared_tie_id: SqliteInteger,
    expected_version: Annotated[int, Query(gt=0, le=SQLITE_SIGNED_64_MAX)],
    management: Annotated[
        SharedTournamentManagementContext,
        Depends(_authorize_shared_tournament_management),
    ],
) -> dict[str, object]:
    settings = load_settings()
    try:
        result = delete_shared_two_legged_tie(
            database_path=settings.database_path,
            shared_tournament_id=shared_tournament_id,
            shared_tie_id=shared_tie_id,
            expected_version=expected_version,
            actor_telegram_user_id=management.user.telegram_user_id,
            actor_first_name=management.user.first_name,
            actor_last_name=management.user.last_name,
            actor_username=management.user.username,
            now_utc=_utc_now(),
        )
    except (
        SharedTournamentNotFoundError,
        SharedTwoLeggedTieNotFoundError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        SharedTournamentLockedError,
        SharedTwoLeggedTieConflictError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return {
        "deleted": True,
        "linked_contest_count": result.linked_contest_count,
        "deleted_match_prediction_count": result.deleted_match_prediction_count,
        "deleted_advancing_prediction_count": (
            result.deleted_advancing_prediction_count
        ),
    }


@router.delete(
    "/contests/{contest_id}/two-legged-ties/{tie_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_tma_two_legged_tie(
    contest_id: SqliteInteger,
    tie_id: SqliteInteger,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> Response:
    settings = load_settings()
    try:
        delete_two_legged_tie(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            tie_id=tie_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            audit_actor=_audit_actor(context.context, context.access),
        )
    except (ContestNotFoundError, TwoLeggedTieNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        MatchUpdateUnavailableError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/contests/{contest_id}/matches/{match_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_tma_match(
    contest_id: SqliteInteger,
    match_id: SqliteInteger,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> Response:
    settings = load_settings()

    try:
        delete_match(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            match_id=match_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            audit_actor=_audit_actor(context.context, context.access),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except MatchNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        MatchUpdateUnavailableError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/contests/{contest_id}/matches/{match_id}")
async def update_tma_match_start(
    contest_id: SqliteInteger,
    match_id: SqliteInteger,
    payload: UpdateMatchStartRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        match = update_match_start(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            match_id=match_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            starts_at_utc=payload.starts_at_utc,
            audit_actor=_audit_actor(context.context, context.access),
        )
    except (ContestNotFoundError, MatchNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        MatchUpdateUnavailableError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(content={"match": _serialize_match(match)})


@router.get("/contests/{contest_id}")
async def get_tma_contest(
    contest_id: SqliteInteger,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> dict[str, object]:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    try:
        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    return {
        "contest": _serialize_contest_details(
            contest, database_path=settings.database_path
        )
    }


@router.post("/contests/{contest_id}/complete")
async def complete_tma_contest(
    contest_id: SqliteInteger,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    now_utc = _utc_now()

    try:
        complete_contest(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            now_utc=now_utc,
            audit_actor=_audit_actor(context.context, context.access),
        )

        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            now_utc=now_utc,
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        ContestCompletionUnavailableError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={
            "contest": _serialize_contest_details(
                contest, database_path=settings.database_path
            )
        },
    )


@router.delete(
    "/contests/{contest_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_tma_contest(
    contest_id: SqliteInteger,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> Response:
    settings = load_settings()

    try:
        delete_contest(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            audit_actor=_audit_actor(context.context, context.access),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except ContestCompletedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/contests/{contest_id}/teams")
async def save_tma_tournament_teams(
    contest_id: SqliteInteger,
    payload: SaveTournamentTeamsRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        tournament_teams = save_tournament_teams(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            team_names=payload.team_names,
            audit_actor=_audit_actor(context.context, context.access),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        TournamentTeamsLockedError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={
            "tournament_teams": _serialize_tournament_teams(tournament_teams),
        },
    )


@router.post("/contests/{contest_id}/matches")
async def create_tma_match(
    contest_id: SqliteInteger,
    payload: CreateMatchRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER),
    ] = None,
) -> JSONResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не передан ключ идемпотентности создания матча.",
        )

    settings = load_settings()
    now_utc = _utc_now()
    try:
        result = create_match(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            home_team_id=payload.home_team_id,
            away_team_id=payload.away_team_id,
            starts_at_utc=payload.starts_at_utc,
            best_of=payload.best_of,
            round_key=payload.round_key,
            idempotency_key=idempotency_key,
            audit_actor=_audit_actor(context.context, context.access),
        )
        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            now_utc=now_utc,
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        MatchCreationConflictError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    response_status = (
        status.HTTP_201_CREATED if result.was_created else status.HTTP_200_OK
    )
    match = next(
        (match for match in contest.matches if match.id == result.match.id), None
    )
    if match is None:
        raise RuntimeError("Не удалось повторно прочитать созданный матч.")
    return JSONResponse(
        status_code=response_status,
        content={
            "match": _serialize_match(match),
            "was_created": result.was_created,
        },
    )


@router.post("/contests/{contest_id}/two-legged-ties")
async def create_tma_two_legged_tie(
    contest_id: SqliteInteger,
    payload: CreateTwoLeggedTieRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER),
    ] = None,
) -> JSONResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не передан ключ идемпотентности создания противостояния.",
        )

    settings = load_settings()
    try:
        result = create_two_legged_tie(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            first_team_id=payload.first_team_id,
            second_team_id=payload.second_team_id,
            first_leg_starts_at_utc=payload.first_leg_starts_at_utc,
            second_leg_starts_at_utc=payload.second_leg_starts_at_utc,
            idempotency_key=idempotency_key,
            audit_actor=_audit_actor(context.context, context.access),
            now_utc=_utc_now(),
            round_key=payload.round_key,
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        SharedTournamentManagedError,
        TwoLeggedTieCreationConflictError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        status_code=(
            status.HTTP_201_CREATED if result.was_created else status.HTTP_200_OK
        ),
        content={
            "two_legged_tie": _serialize_two_legged_tie(result.tie),
            "was_created": result.was_created,
        },
    )


@router.put("/contests/{contest_id}/matches/{match_id}/prediction")
async def save_tma_match_prediction(
    contest_id: SqliteInteger,
    match_id: SqliteInteger,
    payload: SaveMatchPredictionRequest,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> JSONResponse:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    try:
        result = save_match_prediction(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            match_id=match_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            predicted_home_score=payload.predicted_home_score,
            predicted_away_score=payload.predicted_away_score,
            predicted_advancing_team_id=payload.predicted_advancing_team_id,
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except MatchNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (ContestCompletedError, PredictionUnavailableError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    response_status = (
        status.HTTP_201_CREATED if result.was_created else status.HTTP_200_OK
    )
    return JSONResponse(
        status_code=response_status,
        content={
            "prediction": _serialize_prediction(result.prediction),
            "was_created": result.was_created,
        },
    )


@router.put("/contests/{contest_id}/matches/{match_id}/result")
async def save_tma_match_result(
    contest_id: SqliteInteger,
    match_id: SqliteInteger,
    payload: SaveMatchResultRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        result = save_match_result(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            match_id=match_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            home_score=payload.home_score,
            away_score=payload.away_score,
            advancing_team_id=payload.advancing_team_id,
            audit_actor=_audit_actor(context.context, context.access),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except MatchNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        MatchResultUnavailableError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    response_status = (
        status.HTTP_201_CREATED if result.was_created else status.HTTP_200_OK
    )
    return JSONResponse(
        status_code=response_status,
        content={
            "result": _serialize_result(result.result),
            "was_created": result.was_created,
        },
    )


@router.put("/contests/{contest_id}/two-legged-ties/{tie_id}/prediction")
async def save_tma_two_legged_tie_prediction(
    contest_id: SqliteInteger,
    tie_id: SqliteInteger,
    payload: SaveTwoLeggedTiePredictionRequest,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> JSONResponse:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    try:
        result = save_two_legged_tie_prediction(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            tie_id=tie_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            predicted_advancing_team_id=payload.predicted_advancing_team_id,
            now_utc=_utc_now(),
        )
    except (ContestNotFoundError, TwoLeggedTieNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        TwoLeggedTiePredictionUnavailableError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        status_code=(
            status.HTTP_201_CREATED if result.was_created else status.HTTP_200_OK
        ),
        content={
            "prediction": {
                "advancing_team_id": result.prediction.advancing_team_id,
            },
            "was_created": result.was_created,
        },
    )


@router.put("/contests/{contest_id}/two-legged-ties/{tie_id}/result")
async def save_tma_two_legged_tie_result(
    contest_id: SqliteInteger,
    tie_id: SqliteInteger,
    payload: SaveTwoLeggedTieResultRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        result = save_two_legged_tie_result(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            tie_id=tie_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            advancing_team_id=payload.advancing_team_id,
            second_leg_extra_time_home_score=(payload.second_leg_extra_time_home_score),
            second_leg_extra_time_away_score=(payload.second_leg_extra_time_away_score),
            second_leg_home_penalty_score=(payload.second_leg_home_penalty_score),
            second_leg_away_penalty_score=(payload.second_leg_away_penalty_score),
            audit_actor=_audit_actor(context.context, context.access),
            now_utc=_utc_now(),
        )
    except (ContestNotFoundError, TwoLeggedTieNotFoundError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        SharedTournamentManagedError,
        TwoLeggedTieResultUnavailableError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        status_code=(
            status.HTTP_201_CREATED if result.was_created else status.HTTP_200_OK
        ),
        content={
            "result": _serialize_two_legged_tie_result(result.result),
            "was_created": result.was_created,
        },
    )


@router.put("/contests/{contest_id}/match-prediction-publication/settings")
async def save_tma_match_prediction_publication_settings(
    contest_id: SqliteInteger,
    payload: SaveMatchPredictionPublicationSettingsRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        save_match_prediction_publication_settings(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            enabled=payload.enabled,
            audit_actor=_audit_actor(context.context, context.access),
        )
        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except ContestCompletedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={
            "match_prediction_publication": (
                _serialize_match_prediction_publication(
                    contest.match_prediction_publication
                )
            ),
        },
    )


@router.post("/contests/{contest_id}/prediction-reminders/publish")
async def publish_tma_prediction_reminders(
    contest_id: SqliteInteger,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
    telegram_client: Annotated[
        TelegramPredictionReminderClient,
        Depends(get_telegram_prediction_reminder_client),
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        message = await asyncio.wait_for(
            publish_prediction_reminders(
                bot=telegram_client,
                database_path=settings.database_path,
                telegram_chat_id=context.chat.telegram_chat_id,
                contest_id=contest_id,
                reply_markup=create_tma_launch_keyboard(
                    database_path=settings.database_path,
                    telegram_chat_id=context.chat.telegram_chat_id,
                    chat_type=context.chat.chat_type,
                    chat_title=context.chat.title,
                    bot_username=settings.bot_username,
                    bot_token=settings.bot_token,
                ),
                now_utc=_utc_now(),
            ),
            timeout=30.0,
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        NoOpenPredictionRemindersError,
        PredictionReminderMessageTooLongError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except (TelegramAPIError, TimeoutError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Не удалось опубликовать напоминания. Попробуйте ещё раз.",
        ) from error

    return JSONResponse(
        content={
            "published": True,
            "reminder_count": message.reminder_count,
            "match_count": message.match_count,
        },
    )


@router.post("/contests/{contest_id}/leaderboard-publications")
async def publish_tma_intermediate_leaderboard(
    contest_id: SqliteInteger,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER),
    ] = None,
) -> JSONResponse:
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не передан ключ идемпотентности публикации рейтинга.",
        )

    settings = load_settings()
    try:
        result = queue_intermediate_leaderboard_publication(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            idempotency_key=idempotency_key,
            audit_actor=_audit_actor(context.context, context.access),
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except IntermediateLeaderboardUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "queued": True,
            "request_id": result.request_id,
            "was_created": result.was_created,
        },
    )


@router.put("/contests/{contest_id}/champion-prediction/settings")
async def save_tma_champion_prediction_settings(
    contest_id: SqliteInteger,
    payload: SaveChampionPredictionSettingsRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        save_champion_prediction_settings(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            enabled=payload.enabled,
            deadline_at=payload.deadline_at,
            points=payload.points,
            audit_actor=_audit_actor(context.context, context.access),
            now_utc=_utc_now(),
        )
        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ChampionPredictionSettingsLockedError,
        ContestCompletedError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={
            "champion_prediction": _serialize_champion_prediction(
                contest.champion_prediction
            ),
        },
    )


@router.put("/contests/{contest_id}/swiss-stage-prediction/settings")
async def save_tma_swiss_stage_prediction_settings(
    contest_id: SqliteInteger,
    payload: SaveSwissStagePredictionSettingsRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        save_swiss_stage_prediction_settings(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            enabled=payload.enabled,
            deadline_at=payload.deadline_at,
            direct_qualifier_count=payload.direct_qualifier_count,
            elimination_qualifier_count=payload.elimination_qualifier_count,
            audit_actor=_audit_actor(context.context, context.access),
            now_utc=_utc_now(),
        )
        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        SwissStagePredictionSettingsLockedError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={
            "swiss_stage_prediction": _serialize_swiss_stage_prediction(
                contest.swiss_stage_prediction
            )
        },
    )


@router.put("/contests/{contest_id}/swiss-stage-prediction")
async def save_tma_swiss_stage_prediction(
    contest_id: SqliteInteger,
    payload: SaveSwissStageSelectionRequest,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> JSONResponse:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    try:
        save_swiss_stage_prediction(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            direct_team_ids=payload.direct_team_ids,
            elimination_team_ids=payload.elimination_team_ids,
            now_utc=_utc_now(),
        )
        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (ContestCompletedError, PredictionUnavailableError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={
            "swiss_stage_prediction": _serialize_swiss_stage_prediction(
                contest.swiss_stage_prediction
            )
        },
    )


@router.put("/contests/{contest_id}/swiss-stage-result")
async def save_tma_swiss_stage_result(
    contest_id: SqliteInteger,
    payload: SaveSwissStageSelectionRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        save_swiss_stage_result(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            direct_team_ids=payload.direct_team_ids,
            elimination_team_ids=payload.elimination_team_ids,
            audit_actor=_audit_actor(context.context, context.access),
            now_utc=_utc_now(),
        )
        contest = get_contest_details(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ContestCompletedError,
        SwissStageResultUnavailableError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={
            "swiss_stage_prediction": _serialize_swiss_stage_prediction(
                contest.swiss_stage_prediction
            )
        },
    )


@router.put("/contests/{contest_id}/champion-prediction")
async def save_tma_champion_prediction(
    contest_id: SqliteInteger,
    payload: SaveChampionPredictionRequest,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> JSONResponse:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()
    try:
        prediction = save_champion_prediction(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            predicted_team_id=payload.predicted_team_id,
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (ContestCompletedError, PredictionUnavailableError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={"prediction": _serialize_team_summary(prediction)},
    )


@router.put("/contests/{contest_id}/champion")
async def save_tma_contest_champion(
    contest_id: SqliteInteger,
    payload: SaveContestChampionRequest,
    context: Annotated[
        ContestManagementContext, Depends(_authorize_contest_management)
    ],
) -> JSONResponse:
    settings = load_settings()
    try:
        champion = save_contest_champion(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            champion_team_id=payload.champion_team_id,
            audit_actor=_audit_actor(context.context, context.access),
            now_utc=_utc_now(),
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        ChampionUnavailableError,
        ContestCompletedError,
        SharedTournamentManagedError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error

    return JSONResponse(
        content={"champion": _serialize_team_summary(champion)},
    )


def _get_verified_tma_context(
    *,
    x_telegram_init_data: str | None,
) -> TmaContext:
    if not x_telegram_init_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Telegram init data is required.",
        )

    settings = load_settings()
    try:
        return build_tma_context(
            init_data=x_telegram_init_data,
            bot_token=settings.bot_token,
            database_path=settings.database_path,
        )
    except TmaContextError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error


def _application_http_error(
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
        headers=headers,
    )


def _parse_telegram_user_id_target(target: str) -> int | None:
    if target.isdecimal():
        significant_digits = target.lstrip("0")
        if not significant_digits or len(significant_digits) > 19:
            raise TelegramUserIdInvalidError(
                "Telegram user id must be a positive integer."
            )
        telegram_user_id = int(significant_digits)
        if telegram_user_id <= 0 or telegram_user_id > SQLITE_SIGNED_64_MAX:
            raise TelegramUserIdInvalidError(
                "Telegram user id must be a positive integer."
            )
        return telegram_user_id
    if target.startswith(("+", "-")) or target[:1].isdigit():
        raise TelegramUserIdInvalidError("Telegram user id must be a positive integer.")
    return None


def _serialize_context(context: TmaContext) -> dict[str, object]:
    return {
        "user": {
            "id": context.user.telegram_user_id,
            "first_name": context.user.first_name,
            "last_name": context.user.last_name,
            "username": context.user.username,
        },
        "chat": {
            "id": context.chat.telegram_chat_id,
            "type": context.chat.chat_type,
            "title": context.chat.title,
        },
    }


def _serialize_access(access: AccessDecision) -> dict[str, object]:
    return {
        "verification_status": access.verification_status.value,
        "role": access.role.value if access.role is not None else None,
        "can_manage_contests": access.can_manage_contests,
        "can_manage_roles": access.can_manage_roles,
    }


def _serialize_user(user: LocalUser) -> dict[str, object]:
    return {
        "id": user.id,
        "telegram_user_id": user.telegram_user_id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


def _serialize_assignment(assignment) -> dict[str, object]:
    return {
        "id": assignment.id,
        "assigned_at": assignment.assigned_at,
        "revoked_at": assignment.revoked_at,
    }


def _serialize_active_assignment(
    item: ActiveSupermoderatorAssignment,
    administrators: TelegramAdministratorsSnapshot,
) -> dict[str, object]:
    is_telegram_admin = administrators.contains(item.user.telegram_user_id)
    return {
        **_serialize_assignment(item.assignment),
        "user": _serialize_user(item.user),
        "assigned_by": _serialize_user(item.assigned_by),
        "is_telegram_admin": is_telegram_admin,
    }


def _user_display_name(user: LocalUser) -> str:
    return " ".join(part for part in (user.first_name, user.last_name) if part)


def _serialize_active_contest(contest) -> dict[str, object]:
    return {
        "id": contest.id,
        "name": contest.name,
        "slug": contest.slug,
        "template_key": contest.template_key,
        "created_at": contest.created_at,
    }


def _serialize_shared_tournament_summary(tournament) -> dict[str, object]:
    return {
        "id": tournament.id,
        "name": tournament.name,
        "template_key": tournament.template_key,
        "is_archived": tournament.is_archived,
        "version": tournament.version,
        "linked_contest_count": tournament.linked_contest_count,
        "match_count": tournament.match_count,
    }


def _serialize_shared_tournament_for_api(details, settings) -> dict[str, object]:
    return _serialize_shared_tournament_details(
        details,
        database_path=settings.database_path,
        football_data_token_configured=(settings.football_data_api_token is not None),
        sync_interval_minutes=settings.champions_league_sync_interval_minutes,
    )


def _serialize_shared_match(match) -> dict[str, object]:
    result = {
        "id": match.id,
        "home_team": {"id": match.home_team.id, "name": match.home_team.name},
        "away_team": {"id": match.away_team.id, "name": match.away_team.name},
        "starts_at_utc": match.starts_at_utc,
        "best_of": match.best_of,
        "status": match.status,
        "result": (
            {
                "home_score": match.home_score,
                "away_score": match.away_score,
                "advancing_team_id": match.advancing_team_id,
            }
            if match.home_score is not None
            else None
        ),
        "version": match.version,
        "linked_contest_count": match.linked_contest_count,
        "prediction_count": match.prediction_count,
    }
    if match.round_key is not None:
        result.update(
            {
                "round_key": match.round_key,
                "round_name": match.round_name,
                "round_position": match.round_position,
                "bracket_position": match.bracket_position,
            }
        )
    if match.shared_tie_id is not None:
        result.update(
            {
                "shared_tie_id": match.shared_tie_id,
                "leg_number": match.leg_number,
                "is_two_legged": True,
            }
        )
    return result


def _serialize_shared_tournament_details(
    details,
    *,
    database_path: Path | None = None,
    football_data_token_configured: bool = False,
    sync_interval_minutes: int = 10,
) -> dict[str, object]:
    swiss_stage_prediction = {
        "is_enabled": details.swiss_stage_prediction.is_enabled,
        "deadline_at": details.swiss_stage_prediction.deadline_at,
        "direct_qualifier_count": (
            details.swiss_stage_prediction.direct_qualifier_count
        ),
        "elimination_qualifier_count": (
            details.swiss_stage_prediction.elimination_qualifier_count
        ),
        "selection_mode": details.swiss_stage_prediction.selection_mode,
        "direct_correct_points": (details.swiss_stage_prediction.direct_correct_points),
        "elimination_correct_points": (
            details.swiss_stage_prediction.elimination_correct_points
        ),
        "cross_category_points": (details.swiss_stage_prediction.cross_category_points),
        "maximum_points": details.swiss_stage_prediction.maximum_points,
        "direct_qualifier_team_ids": list(
            details.swiss_stage_prediction.direct_qualifier_team_ids
        ),
        "elimination_qualifier_team_ids": list(
            details.swiss_stage_prediction.elimination_qualifier_team_ids
        ),
        "settings_locked": details.swiss_stage_prediction.settings_locked,
    }
    if details.tournament.template_key == "champions_league_2026_27":
        swiss_stage_prediction["playoff_team_ids"] = list(
            details.swiss_stage_prediction.playoff_team_ids
        )
    result = {
        **_serialize_shared_tournament_summary(details.tournament),
        "teams": [{"id": team.id, "name": team.name} for team in details.teams],
        "matches": [_serialize_shared_match(match) for match in details.matches],
        "champion_prediction": {
            "is_enabled": details.champion_prediction.is_enabled,
            "deadline_at": details.champion_prediction.deadline_at,
            "points": details.champion_prediction.points,
            "actual_champion": (
                {
                    "id": details.champion_prediction.actual_champion.id,
                    "name": details.champion_prediction.actual_champion.name,
                }
                if details.champion_prediction.actual_champion is not None
                else None
            ),
        },
        "swiss_stage_prediction": swiss_stage_prediction,
    }
    if details.two_legged_ties:
        result["two_legged_ties"] = [
            _serialize_shared_two_legged_tie(tie) for tie in details.two_legged_ties
        ]
    if (
        database_path is not None
        and details.tournament.template_key == "champions_league_2026_27"
    ):
        playoff_bracket = _serialize_champions_league_playoff_bracket(
            database_path=database_path,
            shared_tournament_id=details.tournament.id,
            teams=details.teams,
            matches=details.matches,
            two_legged_ties=details.two_legged_ties,
        )
        if playoff_bracket is not None:
            result["playoff_bracket"] = playoff_bracket
        result["fixture_sync"] = _serialize_champions_league_fixture_sync(
            database_path=database_path,
            shared_tournament_id=details.tournament.id,
            token_configured=football_data_token_configured,
            interval_minutes=sync_interval_minutes,
        )
    return result


def _serialize_shared_two_legged_tie(tie) -> dict[str, object]:
    result = {
        "id": tie.id,
        "first_team": {"id": tie.first_team.id, "name": tie.first_team.name},
        "second_team": {"id": tie.second_team.id, "name": tie.second_team.name},
        "first_leg": _serialize_shared_match(tie.first_leg),
        "second_leg": _serialize_shared_match(tie.second_leg),
        "aggregate_first_team_score": tie.aggregate_first_team_score,
        "aggregate_second_team_score": tie.aggregate_second_team_score,
        "advancing_team_id": tie.advancing_team_id,
        "resolution_method": tie.resolution_method,
        "second_leg_extra_time_home_score": (tie.second_leg_extra_time_home_score),
        "second_leg_extra_time_away_score": (tie.second_leg_extra_time_away_score),
        "second_leg_home_penalty_score": tie.second_leg_home_penalty_score,
        "second_leg_away_penalty_score": tie.second_leg_away_penalty_score,
        "version": tie.version,
        "linked_contest_count": tie.linked_contest_count,
        "prediction_count": tie.prediction_count,
    }
    if tie.round_key is not None:
        result.update(
            {
                "round_key": tie.round_key,
                "round_name": tie.round_name,
                "round_position": tie.round_position,
                "bracket_position": tie.bracket_position,
            }
        )
    return result


def _serialize_independent_champions_league_playoff_bracket(
    *, matches, two_legged_ties
) -> dict[str, object]:
    matches_by_id = {int(match.id): match for match in matches}
    matches_by_position = {
        (str(match.round_key), int(match.bracket_position)): match
        for match in matches
        if getattr(match, "round_key", None) is not None
        and getattr(match, "bracket_position", None) is not None
        and not bool(getattr(match, "is_two_legged", False))
    }
    ties_by_position = {
        (str(tie.round_key), int(tie.bracket_position)): tie
        for tie in two_legged_ties
        if getattr(tie, "round_key", None) is not None
        and getattr(tie, "bracket_position", None) is not None
    }
    serialized_rounds: list[dict[str, object]] = []
    node_states: set[str] = set()
    for round_definition in CHAMPIONS_LEAGUE_ROUNDS:
        nodes: list[dict[str, object]] = []
        for bracket_position in range(1, round_definition.node_count + 1):
            position_key = (round_definition.key, bracket_position)
            entity: dict[str, object] | None = None
            first_team = None
            second_team = None
            state = "awaiting_teams"
            if round_definition.node_format == "two_legged":
                tie = ties_by_position.get(position_key)
                if tie is not None:
                    entity = {"type": "two_legged_tie", "id": int(tie.id)}
                    first_team = {
                        "id": int(tie.first_team_id),
                        "name": str(tie.first_team_name),
                    }
                    second_team = {
                        "id": int(tie.second_team_id),
                        "name": str(tie.second_team_name),
                    }
                    if tie.result is not None:
                        state = "finished"
                    else:
                        legs = [
                            matches_by_id.get(tie.first_leg_match_id),
                            matches_by_id.get(tie.second_leg_match_id),
                        ]
                        statuses = {str(leg.status) for leg in legs if leg is not None}
                        if "cancelled" in statuses:
                            state = "cancelled"
                        elif statuses & {"started", "finished"}:
                            state = "in_progress"
                        else:
                            state = "scheduled"
            else:
                match = matches_by_position.get(position_key)
                if match is not None:
                    entity = {"type": "match", "id": int(match.id)}
                    first_team = {
                        "id": int(match.home_team_id),
                        "name": str(match.home_team_name),
                    }
                    second_team = {
                        "id": int(match.away_team_id),
                        "name": str(match.away_team_name),
                    }
                    state = _playoff_match_state(str(match.status))
            node_states.add(state)
            nodes.append(
                {
                    "id": -(round_definition.stage_position * 100 + bracket_position),
                    "position": bracket_position,
                    "first_slot": {
                        "team": first_team,
                        "source_node_id": None,
                        "label": None,
                    },
                    "second_slot": {
                        "team": second_team,
                        "source_node_id": None,
                        "label": None,
                    },
                    "entity": entity,
                    "advances_to": None,
                    "state": state,
                    "sync_error": None,
                }
            )
        serialized_rounds.append(
            {
                "key": round_definition.key,
                "name": round_definition.name,
                "position": round_definition.stage_position,
                "format": round_definition.node_format,
                "nodes": nodes,
            }
        )
    if node_states == {"finished"}:
        bracket_state = "finished"
    elif node_states & {"scheduled", "in_progress", "finished"}:
        bracket_state = "ready"
    else:
        bracket_state = "pending"
    return {
        "state": bracket_state,
        "mode": "manual",
        "rounds": serialized_rounds,
    }


def _serialize_champions_league_playoff_bracket(
    *,
    database_path: Path,
    shared_tournament_id: int,
    teams,
    matches,
    two_legged_ties,
    contest_id: int | None = None,
) -> dict[str, object] | None:
    try:
        bracket = get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=shared_tournament_id,
        )
    except ChampionsLeagueBracketError:
        return None
    if not bracket.nodes:
        return None

    team_names = {int(team.id): str(team.name) for team in teams}
    node_by_id = {node.id: node for node in bracket.nodes}
    downstream_by_id: dict[int, tuple[int, str]] = {}
    for downstream in bracket.nodes:
        if downstream.first_source_node_id is not None:
            downstream_by_id[downstream.first_source_node_id] = (
                downstream.id,
                "first",
            )
        if downstream.second_source_node_id is not None:
            downstream_by_id[downstream.second_source_node_id] = (
                downstream.id,
                "second",
            )

    tie_id_map: dict[int, int] = {}
    match_id_map: dict[int, int] = {}
    if contest_id is not None:
        with database_connection(database_path) as connection:
            tie_id_map = {
                int(row["shared_tie_id"]): int(row["tie_id"])
                for row in connection.execute(
                    """
                    SELECT shared_tie_id, tie_id
                    FROM shared_tie_links
                    WHERE contest_id = ?
                    """,
                    (contest_id,),
                ).fetchall()
            }
            match_id_map = {
                int(row["shared_match_id"]): int(row["match_id"])
                for row in connection.execute(
                    """
                    SELECT shared_match_id, match_id
                    FROM shared_match_links
                    WHERE contest_id = ?
                    """,
                    (contest_id,),
                ).fetchall()
            }

    matches_by_id = {int(match.id): match for match in matches}
    ties_by_id = {int(tie.id): tie for tie in two_legged_ties}
    matches_by_position = {
        (str(match.round_key), int(match.bracket_position)): match
        for match in matches
        if getattr(match, "round_key", None) is not None
        and getattr(match, "bracket_position", None) is not None
        and getattr(match, "shared_tie_id", None) is None
        and not bool(getattr(match, "is_two_legged", False))
    }
    ties_by_position = {
        (str(tie.round_key), int(tie.bracket_position)): tie
        for tie in two_legged_ties
        if getattr(tie, "round_key", None) is not None
        and getattr(tie, "bracket_position", None) is not None
    }

    def slot_payload(
        *, team_id: int | None, source_node_id: int | None
    ) -> dict[str, object]:
        team = (
            {
                "id": team_id,
                "name": team_names.get(team_id, f"Команда #{team_id}"),
            }
            if team_id is not None
            else None
        )
        label = None
        if team is None and source_node_id is not None:
            source_node = node_by_id.get(source_node_id)
            if source_node is not None:
                label = (
                    f"Победитель: {source_node.round_name}, "
                    f"пара {source_node.bracket_position}"
                )
            else:
                label = "Победитель предыдущей пары"
        return {
            "team": team,
            "source_node_id": source_node_id,
            "label": label,
        }

    serialized_nodes: dict[int, dict[str, object]] = {}
    for node in bracket.nodes:
        entity: dict[str, object] | None = None
        materialized_entity = None
        if node.materialized_shared_tie_id is not None:
            entity_id = (
                tie_id_map.get(node.materialized_shared_tie_id)
                if contest_id is not None
                else node.materialized_shared_tie_id
            )
            if entity_id is not None:
                entity = {"type": "two_legged_tie", "id": entity_id}
                materialized_entity = ties_by_id.get(entity_id)
        elif node.materialized_shared_match_id is not None:
            entity_id = (
                match_id_map.get(node.materialized_shared_match_id)
                if contest_id is not None
                else node.materialized_shared_match_id
            )
            if entity_id is not None:
                entity = {"type": "match", "id": entity_id}
                materialized_entity = matches_by_id.get(entity_id)

        position_key = (node.round_key, node.bracket_position)
        if materialized_entity is None and node.node_format == "two_legged":
            materialized_entity = ties_by_position.get(position_key)
            if materialized_entity is not None:
                entity = {
                    "type": "two_legged_tie",
                    "id": int(materialized_entity.id),
                }
        elif materialized_entity is None:
            materialized_entity = matches_by_position.get(position_key)
            if materialized_entity is not None:
                entity = {"type": "match", "id": int(materialized_entity.id)}

        effective_first_team_id = node.resolved_first_team_id
        effective_second_team_id = node.resolved_second_team_id
        if materialized_entity is not None and node.node_format == "two_legged":
            first_team = getattr(materialized_entity, "first_team", None)
            second_team = getattr(materialized_entity, "second_team", None)
            effective_first_team_id = (
                effective_first_team_id
                or getattr(first_team, "id", None)
                or getattr(materialized_entity, "first_team_id", None)
            )
            effective_second_team_id = (
                effective_second_team_id
                or getattr(second_team, "id", None)
                or getattr(materialized_entity, "second_team_id", None)
            )
        elif materialized_entity is not None:
            home_team = getattr(materialized_entity, "home_team", None)
            away_team = getattr(materialized_entity, "away_team", None)
            effective_first_team_id = (
                effective_first_team_id
                or getattr(home_team, "id", None)
                or getattr(materialized_entity, "home_team_id", None)
            )
            effective_second_team_id = (
                effective_second_team_id
                or getattr(away_team, "id", None)
                or getattr(materialized_entity, "away_team_id", None)
            )

        schedule_ready = node.first_leg_starts_at_utc is not None and (
            node.node_format == "single" or node.second_leg_starts_at_utc is not None
        )
        if materialized_entity is not None and node.node_format == "single":
            schedule_ready = bool(getattr(materialized_entity, "starts_at_utc", None))
        elif materialized_entity is not None:
            first_leg = getattr(materialized_entity, "first_leg", None)
            second_leg = getattr(materialized_entity, "second_leg", None)
            if first_leg is None:
                first_leg = matches_by_id.get(
                    getattr(materialized_entity, "first_leg_match_id", None)
                )
            if second_leg is None:
                second_leg = matches_by_id.get(
                    getattr(materialized_entity, "second_leg_match_id", None)
                )
            schedule_ready = bool(
                getattr(first_leg, "starts_at_utc", None)
                and getattr(second_leg, "starts_at_utc", None)
            )

        node_state = _playoff_bracket_node_state(
            node=node,
            materialized_entity=materialized_entity,
            matches_by_id=matches_by_id,
            teams_ready=(
                effective_first_team_id is not None
                and effective_second_team_id is not None
            ),
            schedule_ready=schedule_ready,
            entity_is_mapped=(
                entity is not None
                or (
                    node.materialized_shared_tie_id is None
                    and node.materialized_shared_match_id is None
                )
            ),
        )
        downstream = downstream_by_id.get(node.id)
        serialized_nodes[node.id] = {
            "id": node.id,
            "position": node.bracket_position,
            "first_slot": slot_payload(
                team_id=effective_first_team_id,
                source_node_id=node.first_source_node_id,
            ),
            "second_slot": slot_payload(
                team_id=effective_second_team_id,
                source_node_id=node.second_source_node_id,
            ),
            "entity": entity,
            "advances_to": (
                {"node_id": downstream[0], "slot": downstream[1]}
                if downstream is not None
                else None
            ),
            "state": node_state,
            "sync_error": node.sync_error,
        }

    serialized_rounds = [
        {
            "key": round_definition.key,
            "name": round_definition.name,
            "position": round_definition.stage_position,
            "format": round_definition.node_format,
            "nodes": [
                serialized_nodes[node.id]
                for node in bracket.nodes
                if node.round_key == round_definition.key
            ],
        }
        for round_definition in CHAMPIONS_LEAGUE_ROUNDS
    ]
    node_states = {
        str(node_payload["state"]) for node_payload in serialized_nodes.values()
    }
    if "conflict" in node_states or "needs_attention" in node_states:
        bracket_state = "needs_attention"
    elif node_states == {"finished"}:
        bracket_state = "finished"
    elif node_states & {"scheduled", "in_progress", "finished"}:
        bracket_state = "ready"
    else:
        bracket_state = "pending"
    return {"state": bracket_state, "rounds": serialized_rounds}


def _playoff_bracket_node_state(
    *,
    node,
    materialized_entity,
    matches_by_id,
    teams_ready: bool,
    schedule_ready: bool,
    entity_is_mapped: bool,
) -> str:
    if node.sync_status == "conflict":
        return "conflict"
    if not teams_ready:
        return "awaiting_teams"
    if not schedule_ready:
        return "awaiting_schedule"
    if node.materialized_shared_tie_id is None and (
        node.materialized_shared_match_id is None
    ):
        return "ready"
    if not entity_is_mapped or materialized_entity is None:
        return "needs_attention"
    if node.node_format == "single":
        return _playoff_match_state(str(materialized_entity.status))

    tie_result = getattr(materialized_entity, "result", None)
    if (
        tie_result is not None
        or getattr(materialized_entity, "advancing_team_id", None) is not None
    ):
        return "finished"
    first_leg = getattr(materialized_entity, "first_leg", None)
    second_leg = getattr(materialized_entity, "second_leg", None)
    if first_leg is None:
        first_leg_id = getattr(materialized_entity, "first_leg_match_id", None)
        first_leg = matches_by_id.get(first_leg_id)
    if second_leg is None:
        second_leg_id = getattr(materialized_entity, "second_leg_match_id", None)
        second_leg = matches_by_id.get(second_leg_id)
    legs = (first_leg, second_leg)
    statuses = {
        str(getattr(leg, "status", "scheduled")) for leg in legs if leg is not None
    }
    if "cancelled" in statuses:
        return "cancelled"
    if statuses & {"started", "finished"}:
        return "in_progress"
    return "scheduled"


def _playoff_match_state(match_status: str) -> str:
    return {
        "scheduled": "scheduled",
        "started": "in_progress",
        "finished": "finished",
        "cancelled": "cancelled",
    }.get(match_status, "needs_attention")


def _serialize_champions_league_fixture_sync(
    *,
    database_path: Path,
    shared_tournament_id: int,
    token_configured: bool,
    interval_minutes: int,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    source = get_external_source(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        source=FOOTBALL_DATA_SOURCE,
    )
    fixture_imports = list_fixture_imports(
        database_path=database_path,
        shared_tournament_id=shared_tournament_id,
        source=FOOTBALL_DATA_SOURCE,
    )
    try:
        bracket = get_champions_league_bracket(
            database_path=database_path,
            shared_tournament_id=shared_tournament_id,
        )
    except ChampionsLeagueBracketError:
        bracket = None
    node_conflicts = (
        sum(node.sync_status == "conflict" for node in bracket.nodes)
        if bracket is not None
        else 0
    )
    import_conflicts = sum(
        item.import_status in {"conflict", "tombstoned"} for item in fixture_imports
    )
    with database_connection(database_path) as connection:
        saved_result_count = int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT fixture.shared_match_id)
                FROM shared_fixture_imports AS fixture
                JOIN shared_matches AS match ON match.id = fixture.shared_match_id
                WHERE fixture.shared_tournament_id = ?
                  AND fixture.source = ?
                  AND fixture.import_status = 'imported'
                  AND match.status = 'finished'
                """,
                (shared_tournament_id, FOOTBALL_DATA_SOURCE),
            ).fetchone()[0]
        )
    conflicts = node_conflicts + import_conflicts
    conflict_details: list[dict[str, object]] = []
    if bracket is not None:
        conflict_details.extend(
            {
                "kind": "bracket_node",
                "round_key": node.round_key,
                "position": node.bracket_position,
                "status": node.sync_status,
                "message": node.sync_error or "Узел сетки требует ручной проверки.",
            }
            for node in bracket.nodes
            if node.sync_status == "conflict"
        )
    conflict_details.extend(
        {
            "kind": "fixture",
            "round_key": item.round_key,
            "position": item.bracket_position,
            "fixture_id": item.external_fixture_id,
            "leg_number": item.leg_number,
            "status": item.import_status,
            "message": (
                item.last_error
                or (
                    "Импорт удалён вручную и не будет создан повторно."
                    if item.import_status == "tombstoned"
                    else "Фикстура требует ручной проверки."
                )
            ),
        }
        for item in fixture_imports
        if item.import_status in {"conflict", "tombstoned"}
    )
    enabled = source.sync_enabled if source is not None else False
    resolved_now = (now_utc or _utc_now()).astimezone(timezone.utc)
    last_attempt = _parse_optional_utc_datetime(
        source.last_attempt_at if source is not None else None
    )
    last_success = _parse_optional_utc_datetime(
        source.last_success_at if source is not None else None
    )
    enabled_at = _parse_optional_utc_datetime(
        source.enabled_at if source is not None else None
    )
    next_attempt = None
    if enabled:
        next_attempt = (last_attempt or enabled_at or resolved_now) + timedelta(
            minutes=interval_minutes
        )
    if not enabled:
        state = "disabled"
    elif conflicts:
        state = "needs_attention"
    elif source is not None and source.last_error is not None:
        state = "error"
    elif last_success is None:
        state = "idle"
    elif resolved_now - last_success > timedelta(minutes=interval_minutes * 2):
        state = "stale"
    else:
        state = "ok"

    return {
        "source": FOOTBALL_DATA_SOURCE,
        "state": state,
        "last_attempt_at": source.last_attempt_at if source is not None else None,
        "last_success_at": source.last_success_at if source is not None else None,
        "next_attempt_at": (
            next_attempt.isoformat().replace("+00:00", "Z")
            if next_attempt is not None
            else None
        ),
        "last_error": source.last_error if source is not None else None,
        "conflict_details": conflict_details[:50],
        "conflict_detail_count": len(conflict_details),
        "stats": {
            "fixtures_seen": len(fixture_imports),
            "matches_created": len(
                {
                    item.shared_match_id
                    for item in fixture_imports
                    if item.shared_match_id is not None
                }
            ),
            "matches_updated": 0,
            "ties_created": len(
                {
                    item.shared_tie_id
                    for item in fixture_imports
                    if item.shared_tie_id is not None
                }
            ),
            "results_saved": saved_result_count,
            "conflicts": conflicts,
        },
        "enabled": enabled,
        "token_configured": token_configured,
        "version": source.version if source is not None else None,
    }


def _parse_optional_utc_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _serialize_management_contest(
    contest,
    *,
    status_value: str,
) -> dict[str, object]:
    return {
        "id": contest.id,
        "name": contest.name,
        "status": status_value,
    }


def _serialize_contest_details(
    contest, *, database_path: Path | None = None
) -> dict[str, object]:
    result = {
        "id": contest.id,
        "name": contest.name,
        "slug": contest.slug,
        "template_key": contest.template_key,
        "created_at": contest.created_at,
        "is_active": contest.is_active,
        "shared_tournament": (
            {
                "id": contest.shared_tournament_id,
                "name": contest.shared_tournament_name,
            }
            if contest.shared_tournament_id is not None
            else None
        ),
        "tournament_teams": _serialize_tournament_teams(contest.tournament_teams),
        "match_prediction_publication": _serialize_match_prediction_publication(
            contest.match_prediction_publication
        ),
        "champion_prediction": _serialize_champion_prediction(
            contest.champion_prediction
        ),
        "leaderboard": [
            _serialize_leaderboard_entry(entry) for entry in contest.leaderboard
        ],
        "matches": [_serialize_match(match) for match in contest.matches],
    }
    if contest.two_legged_ties:
        result["two_legged_ties"] = [
            _serialize_two_legged_tie(tie) for tie in contest.two_legged_ties
        ]
    if (
        contest.swiss_stage_prediction.is_enabled
        or contest.swiss_stage_prediction.deadline_at is not None
        or contest.swiss_stage_prediction.candidates
        or contest.swiss_stage_prediction.settings_locked
        or contest.swiss_stage_prediction.direct_qualifier_count != 3
        or contest.swiss_stage_prediction.elimination_qualifier_count != 5
        or contest.swiss_stage_prediction.selection_mode != "exact"
        or contest.swiss_stage_prediction.direct_correct_points != 2
        or contest.swiss_stage_prediction.elimination_correct_points != 2
        or contest.swiss_stage_prediction.cross_category_points != 1
    ):
        result["swiss_stage_prediction"] = _serialize_swiss_stage_prediction(
            contest.swiss_stage_prediction
        )
    if contest.template_key == "champions_league_2026_27":
        if database_path is not None and contest.shared_tournament_id is not None:
            playoff_bracket = _serialize_champions_league_playoff_bracket(
                database_path=database_path,
                shared_tournament_id=contest.shared_tournament_id,
                contest_id=contest.id,
                teams=contest.tournament_teams.teams,
                matches=contest.matches,
                two_legged_ties=contest.two_legged_ties,
            )
            if playoff_bracket is not None:
                result["playoff_bracket"] = playoff_bracket
        elif contest.shared_tournament_id is None:
            result["playoff_bracket"] = (
                _serialize_independent_champions_league_playoff_bracket(
                    matches=contest.matches,
                    two_legged_ties=contest.two_legged_ties,
                )
            )
    return result


def _serialize_match_prediction_publication(
    match_prediction_publication,
) -> dict[str, object]:
    return {
        "is_enabled": match_prediction_publication.is_enabled,
    }


def _serialize_champion_prediction(champion_prediction) -> dict[str, object]:
    return {
        "is_enabled": champion_prediction.is_enabled,
        "deadline_at": champion_prediction.deadline_at,
        "points": champion_prediction.points,
        "candidates": [
            _serialize_team_summary(team) for team in champion_prediction.candidates
        ],
        "prediction": (
            _serialize_team_summary(champion_prediction.prediction)
            if champion_prediction.prediction is not None
            else None
        ),
        "actual_champion": (
            _serialize_team_summary(champion_prediction.actual_champion)
            if champion_prediction.actual_champion is not None
            else None
        ),
        "is_open": champion_prediction.is_open,
        "is_tournament_completed": champion_prediction.is_tournament_completed,
        "awarded_points": champion_prediction.awarded_points,
    }


def _serialize_team_summary(team) -> dict[str, object]:
    return {
        "id": team.id,
        "name": team.name,
    }


def _serialize_tournament_teams(tournament_teams) -> dict[str, object]:
    return {
        "teams": [_serialize_team_summary(team) for team in tournament_teams.teams],
        "is_locked": tournament_teams.is_locked,
    }


def _serialize_swiss_stage_selection(selection) -> dict[str, object]:
    result = {
        "direct_teams": [
            _serialize_team_summary(team) for team in selection.direct_teams
        ],
        "elimination_teams": [
            _serialize_team_summary(team) for team in selection.elimination_teams
        ],
        "is_complete": selection.is_complete,
    }
    if selection.playoff_teams:
        result["playoff_teams"] = [
            _serialize_team_summary(team) for team in selection.playoff_teams
        ]
    return result


def _serialize_swiss_stage_award(award) -> dict[str, object]:
    return {
        "team": _serialize_team_summary(award.team),
        "predicted_category": award.predicted_category,
        "actual_category": award.actual_category,
        "points": award.points,
    }


def _serialize_swiss_stage_prediction(swiss_stage_prediction) -> dict[str, object]:
    return {
        "is_enabled": swiss_stage_prediction.is_enabled,
        "deadline_at": swiss_stage_prediction.deadline_at,
        "direct_qualifier_count": (swiss_stage_prediction.direct_qualifier_count),
        "elimination_qualifier_count": (
            swiss_stage_prediction.elimination_qualifier_count
        ),
        "selection_mode": swiss_stage_prediction.selection_mode,
        "direct_correct_points": swiss_stage_prediction.direct_correct_points,
        "elimination_correct_points": (
            swiss_stage_prediction.elimination_correct_points
        ),
        "cross_category_points": swiss_stage_prediction.cross_category_points,
        "maximum_points": swiss_stage_prediction.maximum_points,
        "candidates": [
            _serialize_team_summary(team) for team in swiss_stage_prediction.candidates
        ],
        "prediction": (
            _serialize_swiss_stage_selection(swiss_stage_prediction.prediction)
            if swiss_stage_prediction.prediction is not None
            else None
        ),
        "actual_result": (
            _serialize_swiss_stage_selection(swiss_stage_prediction.actual_result)
            if swiss_stage_prediction.actual_result is not None
            else None
        ),
        "is_open": swiss_stage_prediction.is_open,
        "settings_locked": swiss_stage_prediction.settings_locked,
        "awarded_points": swiss_stage_prediction.awarded_points,
        "awards": [
            _serialize_swiss_stage_award(award)
            for award in swiss_stage_prediction.awards
        ],
        "score_breakdown": (
            _serialize_swiss_stage_score_breakdown(
                swiss_stage_prediction.score_breakdown
            )
            if swiss_stage_prediction.score_breakdown is not None
            else None
        ),
    }


def _serialize_swiss_stage_score_breakdown(score_breakdown) -> dict[str, int]:
    return {
        "correct_direct_count": score_breakdown.correct_direct_count,
        "direct_points": score_breakdown.direct_points,
        "correct_elimination_count": score_breakdown.correct_elimination_count,
        "elimination_points": score_breakdown.elimination_points,
        "total_points": score_breakdown.total_points,
    }


def _serialize_leaderboard_entry(entry) -> dict[str, object]:
    result = {
        "place": entry.place,
        "participant_name": entry.participant_name,
        "participant_username": entry.participant_username,
        "total_points": entry.total_points,
        "match_predictions_count": entry.match_predictions_count,
        "champion_prediction_count": entry.champion_prediction_count,
        "calculated_predictions_count": entry.calculated_predictions_count,
        "prediction_history": [
            _serialize_match(match) for match in entry.prediction_history
        ],
        "champion_prediction_history": (
            _serialize_leaderboard_champion_prediction_history(
                entry.champion_prediction_history,
            )
            if entry.champion_prediction_history is not None
            else None
        ),
    }
    if (
        entry.swiss_stage_prediction_count
        or entry.swiss_stage_prediction_history is not None
    ):
        result["swiss_stage_prediction_count"] = entry.swiss_stage_prediction_count
        result["swiss_stage_prediction_history"] = (
            _serialize_leaderboard_swiss_stage_prediction_history(
                entry.swiss_stage_prediction_history,
            )
            if entry.swiss_stage_prediction_history is not None
            else None
        )
    if entry.two_legged_tie_predictions_count:
        result["two_legged_tie_predictions_count"] = (
            entry.two_legged_tie_predictions_count
        )
    return result


def _serialize_leaderboard_champion_prediction_history(history) -> dict[str, object]:
    return {
        "prediction": _serialize_team_summary(history.prediction),
        "actual_champion": (
            _serialize_team_summary(history.actual_champion)
            if history.actual_champion is not None
            else None
        ),
        "awarded_points": history.awarded_points,
    }


def _serialize_leaderboard_swiss_stage_prediction_history(
    history,
) -> dict[str, object]:
    return {
        "prediction": _serialize_swiss_stage_selection(history.prediction),
        "actual_result": (
            _serialize_swiss_stage_selection(history.actual_result)
            if history.actual_result is not None
            else None
        ),
        "awarded_points": history.awarded_points,
        "awards": [_serialize_swiss_stage_award(award) for award in history.awards],
        "score_breakdown": (
            _serialize_swiss_stage_score_breakdown(history.score_breakdown)
            if history.score_breakdown is not None
            else None
        ),
    }


def _serialize_match(match) -> dict[str, object]:
    result = {
        "id": match.id,
        "tie_id": match.tie_id,
        **({"best_of": match.best_of} if match.best_of is not None else {}),
        "home_team_id": match.home_team_id,
        "home_team_name": match.home_team_name,
        "away_team_id": match.away_team_id,
        "away_team_name": match.away_team_name,
        "starts_at_utc": match.starts_at_utc,
        "status": match.status,
        "result": (
            _serialize_result(match.result) if match.result is not None else None
        ),
        "prediction": (
            _serialize_prediction(match.prediction)
            if match.prediction is not None
            else None
        ),
        "prediction_score": (
            _serialize_prediction_score(match.prediction_score)
            if match.prediction_score is not None
            else None
        ),
    }
    if match.is_two_legged:
        result.update({"is_two_legged": True, "leg_number": match.leg_number})
    if match.round_key is not None:
        result.update(
            {
                "round_key": match.round_key,
                "round_name": match.round_name,
                "round_position": match.round_position,
                "bracket_position": match.bracket_position,
            }
        )
    return result


def _serialize_two_legged_tie(tie) -> dict[str, object]:
    result = {
        "id": tie.id,
        "name": tie.name,
        "first_team": {"id": tie.first_team_id, "name": tie.first_team_name},
        "second_team": {"id": tie.second_team_id, "name": tie.second_team_name},
        "first_leg_match_id": tie.first_leg_match_id,
        "second_leg_match_id": tie.second_leg_match_id,
        "prediction_deadline_at": tie.prediction_deadline_at,
        "is_prediction_open": tie.is_prediction_open,
        "prediction": (
            {"advancing_team_id": tie.prediction.advancing_team_id}
            if tie.prediction is not None
            else None
        ),
        "result": (
            _serialize_two_legged_tie_result(tie.result)
            if tie.result is not None
            else None
        ),
        "awarded_points": tie.awarded_points,
    }
    if tie.round_key is not None:
        result.update(
            {
                "round_key": tie.round_key,
                "round_name": tie.round_name,
                "round_position": tie.round_position,
                "bracket_position": tie.bracket_position,
            }
        )
    return result


def _serialize_two_legged_tie_result(result) -> dict[str, object]:
    return {
        "aggregate_first_team_score": result.aggregate_first_team_score,
        "aggregate_second_team_score": result.aggregate_second_team_score,
        "advancing_team_id": result.advancing_team_id,
        "resolution_method": result.resolution_method,
        "second_leg_extra_time_home_score": (result.second_leg_extra_time_home_score),
        "second_leg_extra_time_away_score": (result.second_leg_extra_time_away_score),
        "second_leg_home_penalty_score": result.second_leg_home_penalty_score,
        "second_leg_away_penalty_score": result.second_leg_away_penalty_score,
    }


def _serialize_result(result) -> dict[str, int | None]:
    return {
        "home_score": result.home_score,
        "away_score": result.away_score,
        "advancing_team_id": result.advancing_team_id,
    }


def _serialize_prediction(prediction) -> dict[str, int | None]:
    return {
        "home_score": prediction.home_score,
        "away_score": prediction.away_score,
        "advancing_team_id": prediction.advancing_team_id,
    }


def _serialize_prediction_score(score) -> dict[str, object]:
    return {
        "total_points": score.total_points,
        "awards": [
            {
                "type": award.score_type,
                "points": award.points,
            }
            for award in score.awards
        ],
    }

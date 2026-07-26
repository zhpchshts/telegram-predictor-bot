from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, BeforeValidator, Field

from app.access_control import (
    AccessDecision,
    AccessRole,
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
    PredictionUnavailableError,
    complete_contest,
    create_match,
    create_world_cup_2026_contest,
    delete_contest,
    delete_match,
    get_active_contests,
    get_completed_contests,
    get_contest_details,
    save_champion_prediction,
    save_champion_prediction_settings,
    save_contest_champion,
    save_match_prediction,
    save_match_prediction_publication_settings,
    save_match_result,
)
from app.tma_context import TmaContext, TmaContextError, build_tma_context
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
    name: str


class CreateMatchRequest(BaseModel):
    home_team_name: str
    away_team_name: str
    starts_at_utc: str


class SaveMatchPredictionRequest(BaseModel):
    predicted_home_score: SqliteInteger
    predicted_away_score: SqliteInteger
    predicted_advancing_team_id: SqliteInteger


class SaveMatchResultRequest(BaseModel):
    home_score: SqliteInteger
    away_score: SqliteInteger
    advancing_team_id: SqliteInteger


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


class ResolveRoleTargetRequest(BaseModel):
    target: str | None = None
    username: str | None = None


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


def get_telegram_administrators_client(
    request: Request,
) -> TelegramAdministratorsClient:
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
        enforcement_enabled=settings.role_enforcement_enabled,
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
        enforcement_enabled=settings.role_enforcement_enabled,
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
        enforcement_enabled=settings.role_enforcement_enabled,
    )
    return {
        "context": _serialize_context(context),
        "access": _serialize_access(access),
        "can_access_management": access.can_manage_contests,
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
    effective_role = (
        management.access.role.value
        if management.access.role is not None
        else AccessRole.PARTICIPANT.value
    )
    return {
        "contests": [
            _serialize_management_contest(
                contest,
                status_value="active",
                effective_role=effective_role,
            )
            for contest in active_contests
        ]
        + [
            _serialize_management_contest(
                contest,
                status_value="completed",
                effective_role=effective_role,
            )
            for contest in completed_contests
        ],
        "capabilities": {
            "can_create_contests": management.access.can_manage_contests,
            "can_manage_roles": management.access.can_manage_roles,
            "can_read_audit": management.access.can_manage_contests,
        },
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
    return {"contest": _serialize_contest_details(contest)}


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
    target = payload.target if payload.target is not None else payload.username
    if target is None:
        raise _application_http_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="role_target_invalid",
            message="Укажите положительный Telegram ID или точный username.",
        )
    target = target.strip()
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
        selection_type = "telegram_user_id"
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
        selection_type = "username"
    assignment = get_active_supermoderator_assignment(
        database_path=settings.database_path,
        chat_id=management.actor.chat_id,
        user_id=user.id,
    )
    return {
        "user": _serialize_user(user),
        "selection_type": selection_type,
        "has_active_assignment": assignment is not None,
        "effective_role": _effective_role(
            user=user,
            administrators=management.administrators,
            has_active_assignment=assignment is not None,
        ),
        "is_telegram_admin": management.administrators.contains(user.telegram_user_id),
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

    settings = load_settings()
    try:
        result = create_world_cup_2026_contest(
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
        )
    except ContestCreationConflictError as error:
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
            "contest": _serialize_active_contest(result.contest),
            "was_created": result.was_created,
        },
    )


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
    except ContestCompletedError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return Response(status_code=status.HTTP_204_NO_CONTENT)


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

    return {"contest": _serialize_contest_details(contest)}


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
        content={"contest": _serialize_contest_details(contest)},
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
            home_team_name=payload.home_team_name,
            away_team_name=payload.away_team_name,
            starts_at_utc=payload.starts_at_utc,
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
    except (ContestCompletedError, MatchCreationConflictError) as error:
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
    except (ContestCompletedError, MatchResultUnavailableError) as error:
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
    except (ChampionUnavailableError, ContestCompletedError) as error:
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
        "enforcement_enabled": access.enforcement_enabled,
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
        "effective_role": ("telegram_admin" if is_telegram_admin else "supermoderator"),
        "is_telegram_admin": is_telegram_admin,
    }


def _effective_role(
    *,
    user: LocalUser,
    administrators: TelegramAdministratorsSnapshot,
    has_active_assignment: bool,
) -> str:
    if administrators.contains(user.telegram_user_id):
        return "telegram_admin"
    if has_active_assignment:
        return "supermoderator"
    return "participant"


def _user_display_name(user: LocalUser) -> str:
    return " ".join(part for part in (user.first_name, user.last_name) if part)


def _serialize_active_contest(contest) -> dict[str, object]:
    return {
        "id": contest.id,
        "name": contest.name,
        "slug": contest.slug,
        "created_at": contest.created_at,
    }


def _serialize_management_contest(
    contest,
    *,
    status_value: str,
    effective_role: str,
) -> dict[str, object]:
    return {
        "id": contest.id,
        "name": contest.name,
        "status": status_value,
        "effective_role": effective_role,
    }


def _serialize_contest_details(contest) -> dict[str, object]:
    return {
        "id": contest.id,
        "name": contest.name,
        "slug": contest.slug,
        "created_at": contest.created_at,
        "is_active": contest.is_active,
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


def _serialize_leaderboard_entry(entry) -> dict[str, object]:
    return {
        "place": entry.place,
        "participant_name": entry.participant_name,
        "total_points": entry.total_points,
        "match_predictions_count": entry.match_predictions_count,
        "champion_prediction_count": entry.champion_prediction_count,
        "total_matches_count": entry.total_matches_count,
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


def _serialize_match(match) -> dict[str, object]:
    return {
        "id": match.id,
        "tie_id": match.tie_id,
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


def _serialize_result(result) -> dict[str, int]:
    return {
        "home_score": result.home_score,
        "away_score": result.away_score,
        "advancing_team_id": result.advancing_team_id,
    }


def _serialize_prediction(prediction) -> dict[str, int]:
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

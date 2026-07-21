from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.access_control import (
    AccessDecision,
    TelegramAdministratorsClient,
    determine_access,
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


TMA_INIT_DATA_HEADER = "X-Telegram-Init-Data"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    predicted_home_score: int
    predicted_away_score: int
    predicted_advancing_team_id: int


class SaveMatchResultRequest(BaseModel):
    home_score: int
    away_score: int
    advancing_team_id: int


class SaveChampionPredictionSettingsRequest(BaseModel):
    enabled: bool
    deadline_at: str | None = None
    points: int


class SaveMatchPredictionPublicationSettingsRequest(BaseModel):
    enabled: bool


class SaveChampionPredictionRequest(BaseModel):
    predicted_team_id: int


class SaveContestChampionRequest(BaseModel):
    champion_team_id: int


def get_telegram_administrators_client(
    request: Request,
) -> TelegramAdministratorsClient:
    try:
        return request.app.state.telegram_bot
    except AttributeError as error:
        raise RuntimeError(
            "Telegram bot is unavailable outside application lifespan."
        ) from error


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
        "active_contests": [
            _serialize_active_contest(contest) for contest in active_contests
        ],
        "completed_contests": [
            _serialize_active_contest(contest) for contest in completed_contests
        ],
    }


@router.post("/contests")
async def create_tma_contest(
    payload: CreateContestRequest,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER),
    ] = None,
) -> JSONResponse:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
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
    contest_id: int,
    match_id: int,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> Response:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
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
    contest_id: int,
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
    contest_id: int,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> JSONResponse:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )

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
    contest_id: int,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> Response:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
    settings = load_settings()

    try:
        delete_contest(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
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
    contest_id: int,
    payload: CreateMatchRequest,
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER),
    ] = None,
) -> JSONResponse:
    context = _get_verified_tma_context(
        x_telegram_init_data=x_telegram_init_data,
    )
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
    contest_id: int,
    match_id: int,
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
    contest_id: int,
    match_id: int,
    payload: SaveMatchResultRequest,
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
    contest_id: int,
    payload: SaveMatchPredictionPublicationSettingsRequest,
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
        save_match_prediction_publication_settings(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            enabled=payload.enabled,
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
    contest_id: int,
    payload: SaveChampionPredictionSettingsRequest,
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
    contest_id: int,
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
    contest_id: int,
    payload: SaveContestChampionRequest,
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
        champion = save_contest_champion(
            database_path=settings.database_path,
            telegram_chat_id=context.chat.telegram_chat_id,
            contest_id=contest_id,
            telegram_user_id=context.user.telegram_user_id,
            first_name=context.user.first_name,
            last_name=context.user.last_name,
            username=context.user.username,
            champion_team_id=payload.champion_team_id,
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


def _serialize_active_contest(contest) -> dict[str, object]:
    return {
        "id": contest.id,
        "name": contest.name,
        "slug": contest.slug,
        "created_at": contest.created_at,
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

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import load_settings
from app.contest_service import (
    ContestCreationConflictError,
    ContestNotFoundError,
    MatchCreationConflictError,
    create_match,
    create_world_cup_2026_contest,
    get_active_contests,
    get_contest_details,
)
from app.tma_context import TmaContext, TmaContextError, build_tma_context


TMA_INIT_DATA_HEADER = "X-Telegram-Init-Data"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

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


@router.get("/bootstrap")
async def get_tma_bootstrap(
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

    return {
        "context": _serialize_context(context),
        "active_contests": [
            _serialize_active_contest(contest) for contest in active_contests
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
        )
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    return {"contest": _serialize_contest_details(contest)}


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
    except ContestNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except MatchCreationConflictError as error:
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
            "match": _serialize_match(result.match),
            "was_created": result.was_created,
        },
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
        "matches": [_serialize_match(match) for match in contest.matches],
    }


def _serialize_match(match) -> dict[str, object]:
    return {
        "id": match.id,
        "home_team_name": match.home_team_name,
        "away_team_name": match.away_team_name,
        "starts_at_utc": match.starts_at_utc,
        "status": match.status,
    }

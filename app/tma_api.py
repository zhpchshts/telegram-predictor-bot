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
    MatchNotFoundError,
    MatchResultUnavailableError,
    PredictionUnavailableError,
    create_match,
    create_world_cup_2026_contest,
    get_active_contests,
    get_contest_details,
    save_match_prediction,
    save_match_result,
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


class SaveMatchPredictionRequest(BaseModel):
    predicted_home_score: int
    predicted_away_score: int
    predicted_advancing_team_id: int


class SaveMatchResultRequest(BaseModel):
    home_score: int
    away_score: int
    advancing_team_id: int


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
            telegram_user_id=context.user.telegram_user_id,
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
    except PredictionUnavailableError as error:
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
    except MatchResultUnavailableError as error:
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
        "leaderboard": [
            _serialize_leaderboard_entry(entry) for entry in contest.leaderboard
        ],
        "matches": [_serialize_match(match) for match in contest.matches],
    }


def _serialize_leaderboard_entry(entry) -> dict[str, object]:
    return {
        "place": entry.place,
        "participant_name": entry.participant_name,
        "total_points": entry.total_points,
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

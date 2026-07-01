from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException

from app.config import load_settings
from app.contest_service import get_active_contests
from app.tma_context import TmaContextError, build_tma_context

TMA_INIT_DATA_HEADER = "X-Telegram-Init-Data"

router = APIRouter(
    prefix="/api/tma",
    tags=["tma"],
)


@router.get("/bootstrap")
async def get_tma_bootstrap(
    x_telegram_init_data: Annotated[
        str | None,
        Header(alias=TMA_INIT_DATA_HEADER),
    ] = None,
) -> dict[str, object]:
    if not x_telegram_init_data:
        raise HTTPException(
            status_code=401,
            detail="Telegram init data is required.",
        )

    settings = load_settings()

    try:
        context = build_tma_context(
            init_data=x_telegram_init_data,
            bot_token=settings.bot_token,
        )
    except TmaContextError as error:
        raise HTTPException(
            status_code=401,
            detail=str(error),
        ) from error

    active_contests = get_active_contests(
        database_path=settings.database_path,
        telegram_chat_id=context.chat.telegram_chat_id,
    )

    return {
        "context": {
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
        },
        "active_contests": [
            {
                "id": contest.id,
                "name": contest.name,
                "slug": contest.slug,
                "created_at": contest.created_at,
            }
            for contest in active_contests
        ],
    }

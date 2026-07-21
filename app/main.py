from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from aiogram import Bot
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app.bot import create_dispatcher
from app.config import load_settings
from app.database import initialize_database
from app.match_prediction_publications import (
    run_match_prediction_publication_worker,
)
from app.match_lifecycle import run_match_lifecycle_worker
from app.publication_outbox import restore_legacy_champion_result_reconciliations
from app.publication_worker import run_contest_publication_worker
from app.tma_api import router as tma_api_router

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMA_DIRECTORY = PROJECT_ROOT / "tma"


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = load_settings()
        initialize_database(settings.database_path)
        restore_legacy_champion_result_reconciliations(
            database_path=settings.database_path,
        )

        bot = Bot(token=settings.bot_token)
        app.state.telegram_bot = bot
        dispatcher = create_dispatcher(settings)

        polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        )
        publication_task = asyncio.create_task(
            run_match_prediction_publication_worker(
                bot=bot,
                database_path=settings.database_path,
            )
        )
        contest_publication_task = asyncio.create_task(
            run_contest_publication_worker(
                bot=bot,
                database_path=settings.database_path,
            )
        )
        match_lifecycle_task = asyncio.create_task(
            run_match_lifecycle_worker(database_path=settings.database_path)
        )

        try:
            yield
        finally:
            match_lifecycle_task.cancel()
            contest_publication_task.cancel()
            publication_task.cancel()
            polling_task.cancel()

            with suppress(asyncio.CancelledError):
                await match_lifecycle_task

            with suppress(asyncio.CancelledError):
                await contest_publication_task

            with suppress(asyncio.CancelledError):
                await publication_task

            with suppress(asyncio.CancelledError):
                await polling_task

            await bot.session.close()
            del app.state.telegram_bot

    app = FastAPI(
        title="Клевер",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(tma_api_router)

    @app.middleware("http")
    async def disable_tma_cache(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        if request.url.path == "/tma" or request.url.path.startswith("/tma/"):
            response.headers["Cache-Control"] = "no-store"

        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount(
        "/tma",
        StaticFiles(directory=TMA_DIRECTORY, html=True),
        name="tma",
    )

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
    )

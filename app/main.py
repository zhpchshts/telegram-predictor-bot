from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from aiogram import Bot
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.bot import create_dispatcher
from app.config import load_settings
from app.database import initialize_database


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMA_DIRECTORY = PROJECT_ROOT / "tma"


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings = load_settings()
        initialize_database(settings.database_path)

        bot = Bot(token=settings.bot_token)
        dispatcher = create_dispatcher()

        polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        )

        try:
            yield
        finally:
            polling_task.cancel()

            with suppress(asyncio.CancelledError):
                await polling_task

            await bot.session.close()

    app = FastAPI(
        title="Telegram Predictor Bot",
        version="0.1.0",
        lifespan=lifespan,
    )

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

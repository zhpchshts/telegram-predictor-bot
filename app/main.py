from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from aiogram import Bot
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.bot import create_dispatcher
from app.config import load_settings
from app.database import database_connection, initialize_database
from app.healthcheck_notifications import run_healthcheck_notification_worker
from app.match_prediction_publications import (
    run_match_prediction_publication_worker,
)
from app.match_lifecycle import run_match_lifecycle_worker
from app.publication_worker import run_contest_publication_worker
from app.tma_api import router as tma_api_router
from app.telegram_username_resolver import (
    TelethonTelegramUsernameResolver,
    UnavailableTelegramUsernameResolver,
)
from app.ti2026_schedule_sync import run_ti2026_schedule_sync_worker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMA_DIRECTORY = PROJECT_ROOT / "tma"
logger = logging.getLogger(__name__)
EXPECTED_BACKGROUND_TASK_NAMES = (
    "telegram-polling",
    "match-prediction-publications",
    "contest-publications",
    "match-lifecycle",
    "ti2026-schedule-sync",
)


async def _cancel_background_tasks(
    tasks: tuple[asyncio.Task[None], ...],
) -> None:
    for task in tasks:
        task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(
            result,
            asyncio.CancelledError,
        ):
            logger.error(
                "Background task %s stopped with an error: %r",
                task.get_name(),
                result,
            )


def _database_is_available(database_path: Path | None) -> bool:
    if database_path is None or not database_path.is_file():
        return False

    try:
        with database_connection(database_path) as connection:
            required_table = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'contests'
                """
            ).fetchone()
            if required_table is None:
                return False
            connection.execute("SELECT 1 FROM contests LIMIT 1").fetchone()
    except Exception as error:
        logger.warning(
            "Database health check failed (%s).",
            type(error).__name__,
        )
        return False

    return True


def _background_task_statuses(
    background_tasks: dict[str, asyncio.Task[None]] | None,
) -> dict[str, str]:
    if background_tasks is None:
        return {name: "missing" for name in EXPECTED_BACKGROUND_TASK_NAMES}

    statuses: dict[str, str] = {}
    task_names = (*EXPECTED_BACKGROUND_TASK_NAMES, *sorted(background_tasks.keys()))
    for name in dict.fromkeys(task_names):
        task = background_tasks.get(name)
        if task is None:
            statuses[name] = "missing"
        elif task.done():
            statuses[name] = "stopped"
        else:
            statuses[name] = "ok"
    return statuses


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = load_settings()
        initialize_database(settings.database_path)

        bot = Bot(token=settings.bot_token)
        app.state.telegram_bot = bot
        username_resolver = UnavailableTelegramUsernameResolver()
        telegram_api_id = settings.telegram_api_id
        telegram_api_hash = settings.telegram_api_hash
        if telegram_api_id is not None and telegram_api_hash is not None:
            try:
                username_resolver = TelethonTelegramUsernameResolver(
                    api_id=telegram_api_id,
                    api_hash=telegram_api_hash,
                    bot_token=settings.bot_token,
                    session_path=settings.telegram_mtproto_session_path,
                )
                await username_resolver.start()
            except Exception as error:
                logger.warning(
                    "Telegram MTProto initialization failed (%s); username "
                    "resolution is disabled.",
                    type(error).__name__,
                )
                username_resolver = UnavailableTelegramUsernameResolver()
        elif telegram_api_id is not None or telegram_api_hash is not None:
            logger.warning(
                "Telegram MTProto configuration is incomplete; username "
                "resolution is disabled."
            )
        app.state.telegram_username_resolver = username_resolver
        dispatcher = create_dispatcher(settings)

        polling_task = asyncio.create_task(
            dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
            ),
            name="telegram-polling",
        )
        publication_task = asyncio.create_task(
            run_match_prediction_publication_worker(
                bot=bot,
                database_path=settings.database_path,
            ),
            name="match-prediction-publications",
        )
        contest_publication_task = asyncio.create_task(
            run_contest_publication_worker(
                bot=bot,
                database_path=settings.database_path,
            ),
            name="contest-publications",
        )
        match_lifecycle_task = asyncio.create_task(
            run_match_lifecycle_worker(database_path=settings.database_path),
            name="match-lifecycle",
        )
        ti2026_schedule_sync_task = asyncio.create_task(
            run_ti2026_schedule_sync_worker(database_path=settings.database_path),
            name="ti2026-schedule-sync",
        )
        healthcheck_notification_task = (
            asyncio.create_task(
                run_healthcheck_notification_worker(
                    bot=bot,
                    database_path=settings.database_path,
                    chat_id=settings.healthcheck_chat_id,
                    interval_minutes=settings.healthcheck_interval_minutes,
                ),
                name="telegram-healthcheck-notifications",
            )
            if settings.healthcheck_chat_id is not None
            else None
        )
        started_tasks = (
            polling_task,
            publication_task,
            contest_publication_task,
            match_lifecycle_task,
            ti2026_schedule_sync_task,
            *(
                (healthcheck_notification_task,)
                if healthcheck_notification_task is not None
                else ()
            ),
        )
        background_tasks = {task.get_name(): task for task in started_tasks}
        app.state.database_path = settings.database_path
        app.state.background_tasks = background_tasks

        try:
            yield
        finally:
            await _cancel_background_tasks(tuple(background_tasks.values()))

            try:
                await bot.session.close()
            finally:
                try:
                    await username_resolver.close()
                finally:
                    del app.state.telegram_bot
                    del app.state.telegram_username_resolver
                    del app.state.background_tasks
                    del app.state.database_path

    app = FastAPI(
        title="Клевер",
        description=(
            "Telegram Mini App для конкурсов прогнозов на спортивные "
            "и киберспортивные турниры в групповых чатах."
        ),
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
    async def health(request: Request) -> JSONResponse:
        database_ok = _database_is_available(
            getattr(request.app.state, "database_path", None)
        )
        task_statuses = _background_task_statuses(
            getattr(request.app.state, "background_tasks", None)
        )
        healthy = database_ok and all(
            task_status == "ok" for task_status in task_statuses.values()
        )
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={
                "status": "ok" if healthy else "unhealthy",
                "checks": {
                    "database": "ok" if database_ok else "unavailable",
                    "background_tasks": task_statuses,
                },
            },
        )

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

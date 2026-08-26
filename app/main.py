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
from app.healthcheck_notifications import send_healthcheck_notification
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
from app.telegram_api_session import TelegramApiAiohttpSession

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMA_DIRECTORY = PROJECT_ROOT / "tma"
logger = logging.getLogger(__name__)
EXPECTED_BACKGROUND_TASK_NAMES = (
    "telegram-polling",
    "match-prediction-publications",
    "contest-publications",
    "match-lifecycle",
)


def _create_telegram_bot(*, token: str, fallback_ips: tuple[str, ...]) -> Bot:
    if not fallback_ips:
        return Bot(token=token)
    return Bot(
        token=token,
        session=TelegramApiAiohttpSession(fallback_ips=fallback_ips),
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


async def _send_startup_healthcheck_notification(
    *,
    bot: Bot,
    database_path: Path,
    chat_id: int,
) -> None:
    try:
        await send_healthcheck_notification(
            bot=bot,
            database_path=database_path,
            chat_id=chat_id,
        )
    except Exception:
        logger.exception("Could not send the Telegram startup notification.")


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


def _build_health_response(request: Request) -> JSONResponse:
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


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = load_settings()
        initialize_database(settings.database_path)

        bot = _create_telegram_bot(
            token=settings.bot_token,
            fallback_ips=settings.telegram_bot_api_fallback_ips,
        )
        app.state.telegram_bot = bot
        username_resolver = UnavailableTelegramUsernameResolver()
        background_tasks: dict[str, asyncio.Task[None]] = {}
        startup_notification_task: asyncio.Task[None] | None = None
        try:
            telegram_api_id = settings.telegram_api_id
            telegram_api_hash = settings.telegram_api_hash
            if telegram_api_id is not None and telegram_api_hash is not None:
                candidate_resolver = TelethonTelegramUsernameResolver(
                    api_id=telegram_api_id,
                    api_hash=telegram_api_hash,
                    bot_token=settings.bot_token,
                    session_path=settings.telegram_mtproto_session_path,
                )
                try:
                    await candidate_resolver.start()
                except asyncio.CancelledError:
                    await candidate_resolver.close()
                    raise
                except Exception as error:
                    try:
                        await candidate_resolver.close()
                    except Exception:
                        logger.exception(
                            "Could not close Telegram MTProto after failed startup."
                        )
                    logger.warning(
                        "Telegram MTProto initialization failed (%s); username "
                        "resolution is disabled.",
                        type(error).__name__,
                    )
                else:
                    username_resolver = candidate_resolver
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
            background_tasks[polling_task.get_name()] = polling_task
            publication_task = asyncio.create_task(
                run_match_prediction_publication_worker(
                    bot=bot,
                    database_path=settings.database_path,
                ),
                name="match-prediction-publications",
            )
            background_tasks[publication_task.get_name()] = publication_task
            contest_publication_task = asyncio.create_task(
                run_contest_publication_worker(
                    bot=bot,
                    database_path=settings.database_path,
                ),
                name="contest-publications",
            )
            background_tasks[contest_publication_task.get_name()] = (
                contest_publication_task
            )
            match_lifecycle_task = asyncio.create_task(
                run_match_lifecycle_worker(database_path=settings.database_path),
                name="match-lifecycle",
            )
            background_tasks[match_lifecycle_task.get_name()] = match_lifecycle_task
            if settings.healthcheck_chat_id is not None:
                startup_notification_task = asyncio.create_task(
                    _send_startup_healthcheck_notification(
                        bot=bot,
                        database_path=settings.database_path,
                        chat_id=settings.healthcheck_chat_id,
                    ),
                    name="telegram-startup-notification",
                )
            app.state.database_path = settings.database_path
            app.state.background_tasks = background_tasks

            yield
        finally:
            await _cancel_background_tasks(
                (
                    *background_tasks.values(),
                    *(
                        (startup_notification_task,)
                        if startup_notification_task is not None
                        else ()
                    ),
                )
            )

            try:
                await bot.session.close()
            finally:
                try:
                    await username_resolver.close()
                finally:
                    for state_name in (
                        "telegram_bot",
                        "telegram_username_resolver",
                        "background_tasks",
                        "database_path",
                    ):
                        if hasattr(app.state, state_name):
                            delattr(app.state, state_name)

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
    async def apply_response_security_policy(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        path = request.url.path
        if (
            path == "/tma"
            or path.startswith("/tma/")
            or path == "/api/tma"
            or path.startswith("/api/tma/")
        ):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"

        return response

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        return _build_health_response(request)

    @app.head("/health")
    async def health_head(request: Request) -> Response:
        health_response = _build_health_response(request)
        return Response(
            status_code=health_response.status_code,
            headers=dict(health_response.headers),
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

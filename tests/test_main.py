import asyncio
from pathlib import Path
import re
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import User
from aiogram.utils.backoff import Backoff
from fastapi.testclient import TestClient
import pytest

from app import main
from app.database import initialize_database
from app.main import create_app


class _PollingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.get_me_error: Exception | None = None
        self.get_me_calls = 0
        self.get_me_started = asyncio.Event()
        self.get_me_allowed = asyncio.Event()
        self.get_me_allowed.set()
        self.get_updates_started = asyncio.Event()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            self.get_me_calls += 1
            self.get_me_started.set()
            await self.get_me_allowed.wait()
            if self.get_me_error is not None:
                raise self.get_me_error
            return User(
                id=123456789,
                is_bot=True,
                first_name="Test",
                username="test_bot",
            )
        if isinstance(method, GetUpdates):
            self.get_updates_started.set()
            await asyncio.Event().wait()
            return []
        raise AssertionError(f"Unexpected Telegram method: {type(method).__name__}")

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""


def _tma_source() -> str:
    return (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _tma_source()
    match = re.search(
        rf"^(?:async )?function {re.escape(name)}\(",
        source,
        flags=re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"Function {name!r} was not found.")
    next_match = re.search(
        r"^(?:async )?function [A-Za-z_$][\w$]*\(",
        source[match.end() :],
        flags=re.MULTILINE,
    )
    end = len(source) if next_match is None else match.end() + next_match.start()
    return source[match.start() : end]


def test_tma_index_is_available() -> None:
    client = TestClient(create_app())

    response = client.get("/tma/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "x-frame-options" not in response.headers
    assert "Клевер" in response.text


@pytest.mark.parametrize(
    ("asset_path", "content_type"),
    (
        ("/tma/app.js", "text/javascript"),
        ("/tma/styles.css", "text/css"),
    ),
)
def test_tma_static_assets_are_stored_and_revalidated(
    asset_path: str,
    content_type: str,
) -> None:
    client = TestClient(create_app())

    response = client.get(asset_path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache, must-revalidate"
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["etag"]

    not_modified_response = client.get(
        asset_path,
        headers={"If-None-Match": response.headers["etag"]},
    )

    assert not_modified_response.status_code == 304
    assert not_modified_response.content == b""
    assert not_modified_response.headers["etag"] == response.headers["etag"]
    assert not_modified_response.headers["cache-control"] == "no-cache, must-revalidate"


def test_private_tma_api_responses_are_not_cacheable() -> None:
    app = create_app()
    app.state.telegram_bot = object()
    client = TestClient(app)

    response = client.get("/api/tma/bootstrap")

    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "x-frame-options" not in response.headers


def test_health_checks_database_and_background_tasks(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    app = create_app()
    app.state.database_path = database_path
    app.state.background_tasks = {
        name: SimpleNamespace(done=lambda: False)
        for name in main.EXPECTED_BACKGROUND_TASK_NAMES
    }

    client = TestClient(app)
    response = client.get("/health")
    head_response = client.head("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "database": "ok",
            "background_tasks": {
                name: "ok" for name in main.EXPECTED_BACKGROUND_TASK_NAMES
            },
        },
    }
    assert head_response.status_code == response.status_code
    assert head_response.content == b""
    assert head_response.headers["content-length"] == response.headers["content-length"]


def test_health_reports_missing_database_and_stopped_task(tmp_path: Path) -> None:
    app = create_app()
    app.state.database_path = tmp_path / "uninitialized.db"
    app.state.telegram_polling_ready = asyncio.Event()
    app.state.background_tasks = {
        name: SimpleNamespace(done=lambda name=name: name == "telegram-polling")
        for name in main.EXPECTED_BACKGROUND_TASK_NAMES
    }

    client = TestClient(app)
    response = client.get("/health")
    head_response = client.head("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "checks": {
            "database": "unavailable",
            "background_tasks": {
                "telegram-polling": "stopped",
                "match-prediction-publications": "ok",
                "contest-publications": "ok",
                "prediction-reminders": "ok",
                "match-lifecycle": "ok",
                "champions-league-sync": "ok",
            },
        },
    }
    assert head_response.status_code == response.status_code
    assert head_response.content == b""
    assert head_response.headers["content-length"] == response.headers["content-length"]


def test_polling_recovers_after_initial_network_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise_lifespan() -> None:
        session = _PollingSession()
        session.get_me_error = TelegramNetworkError(
            method=GetMe(),
            message="Synthetic no route to host",
        )
        bot = Bot(
            token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            session=session,
        )
        dispatcher = Dispatcher()
        retry_started = asyncio.Event()
        retry_allowed = asyncio.Event()

        async def wait_before_retry(_backoff) -> None:
            retry_started.set()
            await retry_allowed.wait()

        async def idle_worker(**_kwargs) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(Backoff, "asleep", wait_before_retry)
        monkeypatch.setattr(
            main,
            "load_settings",
            lambda: SimpleNamespace(
                bot_token="dummy",
                bot_username="test_bot",
                telegram_bot_api_fallback_ips=(),
                database_path=tmp_path / "predictor.db",
                telegram_api_id=None,
                telegram_api_hash=None,
                healthcheck_chat_id=None,
            ),
        )
        monkeypatch.setattr(main, "_create_telegram_bot", lambda **_kwargs: bot)
        monkeypatch.setattr(main, "create_dispatcher", lambda _settings: dispatcher)
        for worker_name in (
            "run_match_prediction_publication_worker",
            "run_contest_publication_worker",
            "run_prediction_reminder_worker",
            "run_match_lifecycle_worker",
            "run_champions_league_sync_worker",
        ):
            monkeypatch.setattr(main, worker_name, idle_worker)

        app = create_app()
        client = TestClient(app)
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(retry_started.wait(), timeout=1)
            polling = app.state.background_tasks["telegram-polling"]
            response = client.get("/health")
            head_response = client.head("/health")

            assert response.status_code == head_response.status_code == 503
            assert response.json()["checks"]["database"] == "ok"
            assert response.json()["checks"]["background_tasks"] == {
                name: "starting" if name == "telegram-polling" else "ok"
                for name in main.EXPECTED_BACKGROUND_TASK_NAMES
            }
            assert head_response.content == b""
            assert client.get("/tma/").status_code == 200
            assert not polling.done()
            assert session.get_me_calls == 1

            session.get_me_error = None
            retry_allowed.set()
            await asyncio.wait_for(session.get_updates_started.wait(), timeout=1)

            assert session.get_me_calls == 2
            assert not polling.done()
            assert client.get("/health").status_code == 200
            assert client.head("/health").status_code == 200

            await dispatcher.stop_polling()
            await asyncio.wait_for(polling, timeout=1)
            stopped_response = client.get("/health")
            assert stopped_response.status_code == 503
            assert (
                stopped_response.json()["checks"]["background_tasks"][
                    "telegram-polling"
                ]
                == "stopped"
            )

        assert session.close_calls == 2
        assert not hasattr(app.state, "telegram_polling_ready")

    asyncio.run(exercise_lifespan())


@pytest.mark.parametrize("error_kind", ("unauthorized", "programming"))
def test_polling_does_not_retry_permanent_startup_errors(error_kind: str) -> None:
    async def exercise_polling() -> None:
        session = _PollingSession()
        error = (
            TelegramUnauthorizedError(method=GetMe(), message="Synthetic unauthorized")
            if error_kind == "unauthorized"
            else RuntimeError("Synthetic programming error")
        )
        session.get_me_error = error
        bot = Bot(
            token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            session=session,
        )
        ready = asyncio.Event()
        try:
            with pytest.raises(type(error), match="Synthetic"):
                await asyncio.wait_for(
                    main._run_telegram_polling(
                        bot=bot,
                        dispatcher=Dispatcher(),
                        ready=ready,
                    ),
                    timeout=1,
                )
            assert session.get_me_calls == 1
            assert not ready.is_set()
            assert not session.get_updates_started.is_set()
            assert session.close_calls == 0
        finally:
            await session.close()

    asyncio.run(exercise_polling())


@pytest.mark.parametrize("phase", ("request", "backoff"))
def test_polling_startup_can_be_cancelled(
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise_polling() -> None:
        session = _PollingSession()
        backoff_started = asyncio.Event()

        async def wait_before_retry(_backoff) -> None:
            backoff_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(Backoff, "asleep", wait_before_retry)
        if phase == "request":
            session.get_me_allowed.clear()
        else:
            session.get_me_error = TelegramNetworkError(
                method=GetMe(),
                message="Synthetic no route to host",
            )
        bot = Bot(
            token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            session=session,
        )
        ready = asyncio.Event()
        polling = asyncio.create_task(
            main._run_telegram_polling(
                bot=bot,
                dispatcher=Dispatcher(),
                ready=ready,
            )
        )
        try:
            waiting = session.get_me_started if phase == "request" else backoff_started
            await asyncio.wait_for(waiting.wait(), timeout=1)
            polling.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(polling, timeout=1)

            assert session.get_me_calls == 1
            assert not ready.is_set()
            assert not session.get_updates_started.is_set()
            assert session.close_calls == 0
        finally:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
            await session.close()

    asyncio.run(exercise_polling())


def test_create_telegram_bot_uses_configured_fallback_ips(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class FakeSession:
        def __init__(self, *, fallback_ips: tuple[str, ...]) -> None:
            recorded["fallback_ips"] = fallback_ips

    class FakeBot:
        def __init__(self, *, token: str, session: object) -> None:
            recorded["token"] = token
            recorded["session"] = session

    monkeypatch.setattr(main, "TelegramApiAiohttpSession", FakeSession)
    monkeypatch.setattr(main, "Bot", FakeBot)

    bot = main._create_telegram_bot(
        token="123:test",
        fallback_ips=("149.154.167.220",),
    )

    assert isinstance(bot, FakeBot)
    assert recorded["token"] == "123:test"
    assert recorded["fallback_ips"] == ("149.154.167.220",)


def test_tma_locks_champion_settings_after_actual_champion() -> None:
    card_source = _function_source("createChampionAdministrationCard")

    assert "championPrediction.actual_champion" in card_source
    assert '? createElement("p"' in card_source
    assert (
        "Настройки зафиксированы после указания фактического чемпиона." in card_source
    )
    assert ": createChampionPredictionSettingsDisclosure" in card_source
    assert "createContestChampionSection" in card_source


def test_tma_exposes_chat_level_supermoderator_management_for_admins() -> None:
    source = _tma_source()
    management_source = _function_source("createSupermoderatorManagementCard")
    assignment_start = management_source.index("async (selectedUser) =>")
    assignment_source = management_source[assignment_start:]
    put_index = assignment_source.index("await apiRequest")

    assert "capabilities.can_manage_roles === true" in source
    assert '"/api/tma/access/supermoderators"' in source
    assert '"/api/tma/access/users/resolve"' in source
    assert "Членство пользователя в Telegram-чате не проверяется." in source
    assert 'input.addEventListener("input"' in source
    assert "Telegram ID или точный username" in source
    assert "JSON.stringify({ target })" in source
    assert "const selectedUser = Object.freeze({ ...result.user });" in source
    assert "${selectedUser.telegram_user_id}" in assignment_source
    assert "getRoleTargetDisplayName(selectedUser)" in assignment_source
    assert "resolvedUser" not in assignment_source[put_index:]
    assert "input.disabled = true" in assignment_source[:put_index]
    assert "findButton.disabled = true" in assignment_source[:put_index]
    assert "finally" in assignment_source
    assert "input.disabled = false" in assignment_source
    assert "findButton.disabled = false" in assignment_source


def test_tma_explains_unavailable_telegram_admin_verification() -> None:
    management_source = _function_source("openManagement")

    assert '"Не удалось открыть управление"' in management_source
    assert "error.message" in management_source
    assert "await openContestList(nextBootstrap" in management_source
    assert "can_manage_contests: false" in management_source


def test_tma_hides_contest_management_without_access() -> None:
    source = _tma_source()
    details_source = _function_source("renderContestDetailsScreen")
    screen_source = _function_source("renderContestScreen")

    assert "return bootstrap.access?.can_manage_contests === true" in source
    assert "if (canManageContests(bootstrap))" in screen_source
    assert "createManagementNavigationCard(bootstrap)" in screen_source
    assert "createMatchFormCard" not in details_source
    assert "createContestCompletionCard" not in details_source
    assert "createContestDeletionCard" not in details_source
    assert "canManageResults: false" in details_source
    assert "createMatchPredictionSection(contest, match)" in source
    assert "createLeaderboardCard(" in details_source


def test_tma_keeps_supermoderator_role_management_separate() -> None:
    management_source = _function_source("renderManagementScreen")
    access_source = _function_source("createManagementAccessCard")
    role_screen_source = _function_source("renderSupermoderatorManagementScreen")
    participant_source = _function_source("renderContestScreen")

    assert "capabilities.can_manage_roles === true" in access_source
    assert "openSupermoderatorManagement(bootstrap)" in access_source
    assert "createSupermoderatorManagementCard" not in management_source
    assert "createSupermoderatorManagementCard()" in role_screen_source
    assert "createSupermoderatorManagementCard" not in participant_source


def test_tma_reports_rejected_management_request_without_false_success() -> None:
    request_source = _function_source("apiRequest")
    confirmation_source = _function_source("createContestConfirmationCard")
    catch_start = confirmation_source.index("} catch (error) {")
    success_source = confirmation_source[:catch_start]
    rejection_source = confirmation_source[catch_start:]
    management_start = success_source.index("if (state.managementMode === true)")
    management_end = success_source.index(
        "renderContestScreen(nextBootstrap",
        management_start,
    )
    management_success_source = success_source[management_start:management_end]

    assert "if (!response.ok)" in request_source
    assert "detail.message" in request_source
    assert "error.code = detail.code" in request_source
    assert 'formMessageType: "success"' in success_source
    assert "openContest(nextBootstrap, result.contest.id" in management_success_source
    assert "managementMode: true" in management_success_source
    assert "openManagement(nextBootstrap" not in management_success_source
    assert 'confirmationMessageType: "error"' in rejection_source
    assert "error instanceof Error" in rejection_source
    assert 'MessageType: "success"' not in rejection_source
    assert "renderContestCreationState(bootstrap" in rejection_source


def test_lifespan_cleans_up_after_a_background_task_failure(
    monkeypatch,
) -> None:
    started: list[str] = []
    cancelled: list[str] = []
    startup_steps: list[str] = []
    session_closed = False
    mtproto_started = False
    mtproto_closed = False

    async def background_task(name: str) -> None:
        started.append(name)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(name)

    class FakeDispatcher:
        async def start_polling(self, *_args, **_kwargs) -> None:
            started.append("polling")
            try:
                raise RuntimeError("synthetic polling failure")
            finally:
                cancelled.append("polling")

        def resolve_used_update_types(self) -> list[str]:
            return []

    class FakeSession:
        async def close(self) -> None:
            nonlocal session_closed
            session_closed = True

    class FakeBot:
        def __init__(self, **_kwargs) -> None:
            self.session = FakeSession()

        async def me(self) -> None:
            return None

    class FakeUsernameResolver:
        def __init__(self, **_kwargs) -> None:
            pass

        async def start(self) -> None:
            nonlocal mtproto_started
            mtproto_started = True

        async def close(self) -> None:
            nonlocal mtproto_closed
            mtproto_closed = True

    async def match_publication_worker(**_kwargs) -> None:
        await background_task("match-publication")

    async def contest_publication_worker(**_kwargs) -> None:
        startup_steps.append("contest-publication-worker")
        await background_task("contest-publication")

    async def prediction_reminder_worker(**_kwargs) -> None:
        await background_task("prediction-reminder")

    async def match_lifecycle_worker(**_kwargs) -> None:
        await background_task("match-lifecycle")

    async def champions_league_sync_worker(**_kwargs) -> None:
        await background_task("champions-league-sync")

    async def startup_healthcheck_notification(**_kwargs) -> None:
        startup_steps.append("healthcheck-notification")

    monkeypatch.setattr(
        main,
        "load_settings",
        lambda: SimpleNamespace(
            bot_token="dummy",
            bot_username="test_bot",
            telegram_bot_api_fallback_ips=(),
            database_path=Path("unused.db"),
            telegram_api_id=12345,
            telegram_api_hash="secret-hash",
            telegram_mtproto_session_path=Path("unused-session"),
            healthcheck_chat_id=100,
        ),
    )
    monkeypatch.setattr(
        main,
        "initialize_database",
        lambda _path: startup_steps.append("initialize-database"),
    )
    monkeypatch.setattr(main, "Bot", FakeBot)
    monkeypatch.setattr(
        main,
        "TelethonTelegramUsernameResolver",
        FakeUsernameResolver,
    )
    monkeypatch.setattr(main, "create_dispatcher", lambda _settings: FakeDispatcher())
    monkeypatch.setattr(
        main,
        "run_match_prediction_publication_worker",
        match_publication_worker,
    )
    monkeypatch.setattr(
        main,
        "run_contest_publication_worker",
        contest_publication_worker,
    )
    monkeypatch.setattr(
        main,
        "run_prediction_reminder_worker",
        prediction_reminder_worker,
    )
    monkeypatch.setattr(
        main,
        "run_match_lifecycle_worker",
        match_lifecycle_worker,
    )
    monkeypatch.setattr(
        main,
        "run_champions_league_sync_worker",
        champions_league_sync_worker,
    )
    monkeypatch.setattr(
        main,
        "send_healthcheck_notification",
        startup_healthcheck_notification,
    )

    app = create_app()

    async def exercise_lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.telegram_bot, FakeBot)
            assert isinstance(
                app.state.telegram_username_resolver,
                FakeUsernameResolver,
            )
            await asyncio.sleep(0)

    asyncio.run(exercise_lifespan())

    assert set(started) == {
        "polling",
        "match-publication",
        "contest-publication",
        "prediction-reminder",
        "match-lifecycle",
        "champions-league-sync",
    }
    assert set(cancelled) == set(started)
    assert session_closed is True
    assert mtproto_started is True
    assert mtproto_closed is True
    assert startup_steps[:2] == [
        "initialize-database",
        "contest-publication-worker",
    ]
    assert startup_steps.count("healthcheck-notification") == 1


def test_lifespan_cleans_up_resources_when_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_closed = False
    resolver_closed = False

    class FakeSession:
        async def close(self) -> None:
            nonlocal session_closed
            session_closed = True

    class FakeBot:
        def __init__(self, **_kwargs) -> None:
            self.session = FakeSession()

    class FakeUsernameResolver:
        def __init__(self, **_kwargs) -> None:
            pass

        async def start(self) -> None:
            return None

        async def close(self) -> None:
            nonlocal resolver_closed
            resolver_closed = True

    monkeypatch.setattr(
        main,
        "load_settings",
        lambda: SimpleNamespace(
            bot_token="dummy",
            telegram_bot_api_fallback_ips=(),
            database_path=Path("unused.db"),
            telegram_api_id=12345,
            telegram_api_hash="secret-hash",
            telegram_mtproto_session_path=Path("unused-session"),
            healthcheck_chat_id=None,
        ),
    )
    monkeypatch.setattr(main, "initialize_database", lambda _path: None)
    monkeypatch.setattr(main, "Bot", FakeBot)
    monkeypatch.setattr(
        main,
        "TelethonTelegramUsernameResolver",
        FakeUsernameResolver,
    )

    def fail_dispatcher_creation(_settings):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(main, "create_dispatcher", fail_dispatcher_creation)
    app = create_app()

    async def exercise_lifespan() -> None:
        async with app.router.lifespan_context(app):
            raise AssertionError("Application startup unexpectedly succeeded.")

    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        asyncio.run(exercise_lifespan())

    assert session_closed is True
    assert resolver_closed is True
    assert not hasattr(app.state, "telegram_bot")
    assert not hasattr(app.state, "telegram_username_resolver")

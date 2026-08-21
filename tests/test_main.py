import asyncio
from pathlib import Path
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.database import initialize_database
from app.main import create_app


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
    assert "Клевер" in response.text


def test_health_checks_database_and_background_tasks(tmp_path: Path) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    app = create_app()
    app.state.database_path = database_path
    app.state.background_tasks = {
        name: SimpleNamespace(done=lambda: False)
        for name in main.EXPECTED_BACKGROUND_TASK_NAMES
    }

    response = TestClient(app).get("/health")

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


def test_health_reports_missing_database_and_stopped_task(tmp_path: Path) -> None:
    app = create_app()
    app.state.database_path = tmp_path / "uninitialized.db"
    app.state.background_tasks = {
        name: SimpleNamespace(done=lambda name=name: name == "telegram-polling")
        for name in main.EXPECTED_BACKGROUND_TASK_NAMES
    }

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "checks": {
            "database": "unavailable",
            "background_tasks": {
                "telegram-polling": "stopped",
                "match-prediction-publications": "ok",
                "contest-publications": "ok",
                "match-lifecycle": "ok",
                "ti2026-schedule-sync": "ok",
            },
        },
    }


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
    assert "can_access_management: false" in management_source


def test_tma_hides_contest_management_without_access() -> None:
    source = _tma_source()
    details_source = _function_source("renderContestDetailsScreen")
    screen_source = _function_source("renderContestScreen")

    assert "return bootstrap.can_access_management === true" in source
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

    async def match_lifecycle_worker(**_kwargs) -> None:
        await background_task("match-lifecycle")

    async def ti2026_schedule_sync_worker(**_kwargs) -> None:
        await background_task("ti2026-schedule-sync")

    async def healthcheck_notification_worker(**_kwargs) -> None:
        await background_task("telegram-healthcheck")

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
            healthcheck_chat_id=100,
            healthcheck_interval_minutes=360,
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
        "run_match_lifecycle_worker",
        match_lifecycle_worker,
    )
    monkeypatch.setattr(
        main,
        "run_ti2026_schedule_sync_worker",
        ti2026_schedule_sync_worker,
    )
    monkeypatch.setattr(
        main,
        "run_healthcheck_notification_worker",
        healthcheck_notification_worker,
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
        "match-lifecycle",
        "ti2026-schedule-sync",
        "telegram-healthcheck",
    }
    assert set(cancelled) == set(started)
    assert session_closed is True
    assert mtproto_started is True
    assert mtproto_closed is True
    assert startup_steps[:2] == [
        "initialize-database",
        "contest-publication-worker",
    ]

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main
from app.main import create_app


def test_tma_index_is_available() -> None:
    client = TestClient(create_app())

    response = client.get("/tma/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Клевер" in response.text


def test_tma_locks_champion_settings_after_actual_champion() -> None:
    source = (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")
    card_start = source.index("function createChampionAdministrationCard")
    card_end = source.index("function createChampionPredictionCard", card_start)
    card_source = source[card_start:card_end]

    assert "championPrediction.actual_champion\n      ? createElement" in card_source
    assert (
        "Настройки зафиксированы после указания фактического чемпиона." in card_source
    )
    assert ": createChampionPredictionSettingsDisclosure" in card_source
    assert "createContestChampionSection" in card_source


def test_tma_exposes_chat_level_supermoderator_management_for_admins() -> None:
    source = (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")
    management_start = source.index("function createSupermoderatorManagementCard")
    management_end = source.index(
        "function renderSupermoderatorAssignments",
        management_start,
    )
    management_source = source[management_start:management_end]
    assignment_start = management_source.index("async (selectedUser) =>")
    assignment_source = management_source[assignment_start:]
    put_index = assignment_source.index("await apiRequest")

    assert "bootstrap.access?.can_manage_roles === true" in source
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
    source = (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")
    screen_start = source.index("function renderContestScreen")
    screen_end = source.index("function renderBootstrap", screen_start)
    screen_source = source[screen_start:screen_end]

    assert (
        'else if (bootstrap.access?.verification_status === "unavailable")'
        in screen_source
    )
    assert "cards.push(createRoleManagementUnavailableCard())" in screen_source
    assert "Не удалось проверить права администратора Telegram." in source
    assert "Доступ к управлению конкурсами определяется отдельно." in source
    assert "Просмотр и прогнозирование продолжают работать." in source
    assert (
        "cards.push(createSupermoderatorManagementCard());\n"
        "  } else if (bootstrap.access?.verification_status"
    ) in screen_source


def test_tma_hides_contest_management_without_access() -> None:
    source = (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")
    details_start = source.index("function renderContestDetailsScreen")
    details_end = source.index("async function openContest", details_start)
    details_source = source[details_start:details_end]
    screen_start = source.index("function renderContestScreen")
    screen_end = source.index("function renderBootstrap", screen_start)
    screen_source = source[screen_start:screen_end]

    assert "bootstrap.access?.enforcement_enabled !== true" in source
    assert "bootstrap.access?.can_manage_contests === true" in source
    assert "Создавать и настраивать конкурсы могут администраторы чата" in source
    assert "Когда будет создан конкурс, он появится здесь." in source
    assert "Открой конкурс, чтобы делать прогнозы и смотреть рейтинг." in source
    assert "Укажите команды и время начала матча." in source
    assert "Любой участник этого чата может добавить матч" not in source
    assert "и управлять матчами." not in source
    assert "creationCard = createContestManagementRestrictedCard()" in screen_source
    assert "creationCard = createContestManagementUnavailableCard()" in screen_source
    assert "canManage ? (resultState)" in details_source
    assert "canManage ? (deletionState)" in details_source
    assert "leadingItems: isActive && canManage" in details_source
    assert (
        '? canManage\n              ? ["Матчей пока нет.", '
        '"Добавьте первый матч ниже."]\n'
        '              : ["Матчей пока нет."]'
    ) in details_source
    assert "if (isActive && canManage)" in details_source
    assert "createMatchFormCard(bootstrap, contest, state)" in details_source
    assert "createContestCompletionCard(bootstrap, contest, state)" in details_source
    assert "createContestDeletionCard(bootstrap, contest, state)" in details_source
    assert "Результат пока не внесён." in source
    assert "createMatchPredictionSection(contest, match)" in source
    assert "createLeaderboardCard(leaderboard, contest.champion_prediction)" in source


def test_tma_keeps_supermoderator_role_management_separate() -> None:
    source = (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")
    screen_start = source.index("function renderContestScreen")
    screen_end = source.index("function renderBootstrap", screen_start)
    screen_source = source[screen_start:screen_end]

    assert "if (bootstrap.access?.can_manage_roles === true)" in screen_source
    assert "cards.push(createSupermoderatorManagementCard())" in screen_source
    assert (
        "can_manage_contests"
        not in screen_source[
            screen_source.index("if (bootstrap.access?.can_manage_roles === true)") :
        ]
    )


def test_tma_reports_rejected_management_request_without_false_success() -> None:
    source = (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")
    request_start = source.index("async function apiRequest")
    request_end = source.index("function handleError", request_start)
    request_source = source[request_start:request_end]
    confirmation_start = source.index("function createContestConfirmationCard")
    confirmation_end = source.index(
        "function createContestDetailsCard",
        confirmation_start,
    )
    confirmation_source = source[confirmation_start:confirmation_end]
    catch_start = confirmation_source.index("} catch (error) {")
    success_source = confirmation_source[:catch_start]
    rejection_source = confirmation_source[catch_start:]

    assert "if (!response.ok)" in request_source
    assert "detail.message" in request_source
    assert "error.code = detail.code" in request_source
    assert 'formMessageType: "success"' in success_source
    assert 'confirmationMessageType: "error"' in rejection_source
    assert "error instanceof Error" in rejection_source
    assert 'MessageType: "success"' not in rejection_source
    assert "renderContestScreen(bootstrap" in rejection_source


def test_lifespan_starts_and_cancels_match_lifecycle_worker(monkeypatch) -> None:
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
            await background_task("polling")

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

    monkeypatch.setattr(
        main,
        "load_settings",
        lambda: SimpleNamespace(
            bot_token="dummy",
            database_path=Path("unused.db"),
            telegram_api_id=12345,
            telegram_api_hash="secret-hash",
            telegram_mtproto_session_path=Path("unused-session"),
        ),
    )
    monkeypatch.setattr(
        main,
        "initialize_database",
        lambda _path: startup_steps.append("initialize-database"),
    )
    monkeypatch.setattr(
        main,
        "restore_legacy_champion_result_reconciliations",
        lambda **_kwargs: startup_steps.append("restore-legacy-publications"),
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
    }
    assert set(cancelled) == set(started)
    assert session_closed is True
    assert mtproto_started is True
    assert mtproto_closed is True
    assert startup_steps[:3] == [
        "initialize-database",
        "restore-legacy-publications",
        "contest-publication-worker",
    ]

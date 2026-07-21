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


def test_lifespan_starts_and_cancels_match_lifecycle_worker(monkeypatch) -> None:
    started: list[str] = []
    cancelled: list[str] = []
    startup_steps: list[str] = []
    session_closed = False

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
    assert startup_steps[:3] == [
        "initialize-database",
        "restore-legacy-publications",
        "contest-publication-worker",
    ]

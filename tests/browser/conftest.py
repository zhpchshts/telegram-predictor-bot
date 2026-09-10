"""Run the shipped Mini App in Chromium against the real API and a temporary DB.

Only the transport is controlled: requests can be paused or dropped before/after
the application processes them. No JavaScript functions or API business rules
are replaced. Installing the optional requirements enables this suite in pytest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.database import initialize_database
from app.main import create_app
from app.tma_api import get_telegram_administrators_client
from app.tma_auth import calculate_init_data_hash
from app.tma_launch import create_tma_launch_token

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Request, Route


BOT_TOKEN = "123456789:browser-test-token"
CHAT_ID = -1009876543210
USER_ID = 123


class TelegramAdministrators:
    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        assert chat_id == CHAT_ID
        return [SimpleNamespace(user=SimpleNamespace(id=USER_ID))]


def signed_init_data() -> str:
    now = int(time.time())
    fields = {
        "auth_date": str(now),
        "query_id": "browser-autosave-test",
        "user": json.dumps({"id": USER_ID, "first_name": "Участник"}),
        "chat_type": "supergroup",
        "start_param": create_tma_launch_token(
            chat_id=CHAT_ID,
            chat_type="supergroup",
            chat_title="Браузерные проверки",
            secret=BOT_TOKEN,
            now=now,
        ),
    }
    fields["hash"] = calculate_init_data_hash(fields, bot_token=BOT_TOKEN)
    return urlencode(fields)


@dataclass
class ObservedRequest:
    method: str
    path: str
    payload: dict | None


class PendingRequest:
    def __init__(
        self,
        app: BrowserApp,
        path: str,
        *,
        persist: bool,
        fail: bool,
        method: str,
    ) -> None:
        self.app = app
        self.path = path
        self.persist = persist
        self.fail = fail
        self.method = method
        self.route: Route | None = None
        self.response = None

    def wait(self) -> PendingRequest:
        deadline = time.monotonic() + 5
        while self.route is None and time.monotonic() < deadline:
            self.app.page.wait_for_timeout(10)
        assert self.route is not None, f"No {self.method} request for {self.path}"
        return self

    def release(self) -> None:
        self.wait()
        assert self.route is not None
        if self.response is None:
            self.response = self.app.forward(self.route.request)
        self.app.fulfill(self.route, self.response)
        self.route = None

    def abort(self) -> None:
        self.wait()
        assert self.route is not None
        self.route.abort("connectionfailed")
        self.route = None

    def respond(self, *, json: dict, status: int = 200) -> None:
        self.wait()
        assert self.route is not None
        self.route.fulfill(status=status, json=json)
        self.route = None


class BrowserApp:
    def __init__(self, page: Page, client: TestClient, database_path: Path) -> None:
        self.page = page
        self.client = client
        self.database_path = database_path
        self.init_data = signed_init_data()
        self.headers = {"X-Telegram-Init-Data": self.init_data}
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.requests: list[ObservedRequest] = []
        self.interceptions: list[PendingRequest] = []
        self.errors: list[str] = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.route("**/*", self.handle_route)
        self.page.add_init_script(
            "window.Telegram = {WebApp: {initData: "
            + json.dumps(self.init_data)
            + ", ready() {}, expand() {}, onEvent() {}, offEvent() {}}};"
        )
        self.page.clock.install(time=self.now)
        self.page.clock.pause_at(self.now)
        response = self.request(
            "POST",
            "/api/tma/contests",
            json={
                "name": "Проверка автосохранения",
                "template_key": "champions_league_2026_27",
            },
        )
        assert response.status_code == 201, response.text
        self.contest = response.json()["contest"]
        self.contest_path = f"/api/tma/contests/{self.contest['id']}"
        response = self.request(
            "PUT",
            f"{self.contest_path}/teams",
            json={"team_names": [f"Клуб {index:02d}" for index in range(1, 37)]},
        )
        assert response.status_code == 200, response.text
        self.contest = self.refresh_contest()
        self.teams = self.contest["tournament_teams"]["teams"]
        response = self.request(
            "POST",
            f"{self.contest_path}/matches",
            json={
                "home_team_id": self.teams[0]["id"],
                "away_team_id": self.teams[1]["id"],
                "starts_at_utc": self.iso_after(seconds=3600),
            },
        )
        assert response.status_code == 201, response.text
        self.matches = [response.json()["match"]]
        self.contest = self.refresh_contest()

    def iso_after(self, *, seconds: float) -> str:
        return (
            (self.now + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
        )

    def request(self, method: str, path: str, *, json=None):
        return self.client.request(
            method,
            path,
            headers={**self.headers, "Idempotency-Key": str(uuid4())},
            **({"json": json} if json is not None else {}),
        )

    def refresh_contest(self) -> dict:
        response = self.request("GET", self.contest_path)
        assert response.status_code == 200, response.text
        return response.json()["contest"]

    def prediction(self, match_id: int | None = None) -> dict | None:
        match_id = self.matches[0]["id"] if match_id is None else match_id
        match = next(
            m for m in self.refresh_contest()["matches"] if m["id"] == match_id
        )
        return match["prediction"]

    def forward(self, request: Request):
        url = urlsplit(request.url)
        assert url.hostname == "testserver", request.url
        path = url.path + (f"?{url.query}" if url.query else "")
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length", "accept-encoding"}
        }
        return self.client.request(
            request.method,
            path,
            headers=headers,
            content=request.post_data_buffer,
        )

    @staticmethod
    def fulfill(route: Route, response) -> None:
        route.fulfill(
            status=response.status_code,
            headers={
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-length", "content-encoding"}
            },
            body=response.content,
        )

    def handle_route(self, route: Route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        if path.startswith("/api/"):
            payload = json.loads(request.post_data) if request.post_data else None
            self.requests.append(ObservedRequest(request.method, path, payload))
        pending = next(
            (
                p
                for p in self.interceptions
                if p.path == path and p.method == request.method
            ),
            None,
        )
        if pending is None:
            self.fulfill(route, self.forward(request))
            return
        self.interceptions.remove(pending)
        pending.route = route
        if pending.persist:
            pending.response = self.forward(request)
        if pending.fail:
            route.abort("connectionfailed")

    def hold_next(
        self, path: str, *, persist: bool = False, method: str = "PUT"
    ) -> PendingRequest:
        pending = PendingRequest(self, path, persist=persist, fail=False, method=method)
        self.interceptions.append(pending)
        return pending

    def fail_next(self, path: str, *, persist: bool = False) -> PendingRequest:
        pending = PendingRequest(self, path, persist=persist, fail=True, method="PUT")
        self.interceptions.append(pending)
        return pending

    def saves(self, path: str) -> list[ObservedRequest]:
        return [r for r in self.requests if r.path == path and r.method == "PUT"]

    def open(self, tab: str = "matches") -> None:
        self.page.goto("http://testserver/tma/")
        self.page.get_by_role("button", name=self.contest["name"], exact=True).click()
        self.page.get_by_role(
            "button",
            name={"matches": "Матчи", "tournament": "Турнир", "leaderboard": "Рейтинг"}[
                tab
            ],
            exact=True,
        ).click()

    def advance(self, milliseconds: float = 500) -> None:
        self.page.clock.run_for(milliseconds)


@pytest.fixture(scope="module")
def browser_engine():
    # The synchronous driver owns an event loop. Close it before another test
    # module can call asyncio.run() (the backend suite does so directly).
    playwright = pytest.importorskip(
        "playwright.sync_api",
        reason="Install requirements-browser.txt to run Chromium autosave regressions",
    )
    with playwright.sync_playwright() as runtime:
        browser_name = os.getenv("TMA_TEST_BROWSER", "chromium")
        if browser_name not in {"chromium", "webkit"}:
            pytest.fail("TMA_TEST_BROWSER must be chromium or webkit")
        try:
            browser: Browser = getattr(runtime, browser_name).launch()
        except playwright.Error as error:
            pytest.fail(
                f"{browser_name} is unavailable. Run: python -m playwright install {browser_name}\n{error}"
            )
        yield browser
        browser.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item.browser_call_failed = report.failed


@pytest.fixture
def browser_app(browser_engine, monkeypatch, tmp_path, request):
    database_path = tmp_path / "browser-predictor.sqlite3"
    initialize_database(database_path)
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("BOT_USERNAME", "BrowserAutosaveTestBot")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    application = create_app()
    application.dependency_overrides[get_telegram_administrators_client] = (
        TelegramAdministrators
    )
    # No lifespan: polling, background workers and external Telegram calls never start.
    client = TestClient(application)
    context = browser_engine.new_context(viewport={"width": 390, "height": 844})
    context.set_default_timeout(5000)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    app = BrowserApp(context.new_page(), client, database_path)
    yield app
    if getattr(request.node, "browser_call_failed", False) or app.errors:
        artifact_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.node.name)
        artifact_dir = (
            Path(__file__).resolve().parents[2]
            / "tmp"
            / "browser-artifacts"
            / os.getenv("TMA_TEST_BROWSER", "chromium")
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        app.page.screenshot(
            path=str(artifact_dir / f"{artifact_name}.png"), full_page=True
        )
        context.tracing.stop(path=str(artifact_dir / f"{artifact_name}.zip"))
    else:
        context.tracing.stop()
    context.close()
    client.close()
    assert not app.errors, f"Uncaught browser JavaScript errors: {app.errors}"

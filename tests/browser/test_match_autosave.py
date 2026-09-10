from __future__ import annotations

from datetime import timedelta

import pytest

from app import contest_service


def match_form(app):
    return app.page.locator(".match-prediction-form").first


def score(app, home: int, away: int = 0) -> None:
    match_id = app.matches[0]["id"]
    app.page.locator(f"#match-{match_id}-home-score").fill(str(home))
    app.page.locator(f"#match-{match_id}-away-score").fill(str(away))


def status(app):
    return match_form(app).locator("[data-save-state]")


def save_path(app) -> str:
    return f"{app.contest_path}/matches/{app.matches[0]['id']}/prediction"


def assert_saved(app, home: int, away: int = 0) -> None:
    from playwright.sync_api import expect

    expect(status(app)).to_have_attribute("data-save-state", "saved")
    prediction = app.prediction()
    assert prediction is not None
    assert (prediction["home_score"], prediction["away_score"]) == (home, away)


def test_rapid_score_edits_preserve_last_value_while_response_is_delayed(browser_app):
    app = browser_app
    app.open()
    pending = app.hold_next(save_path(app), persist=True)
    score(app, 1)
    app.advance()
    pending.wait()
    score(app, 2)
    score(app, 3)
    app.advance()
    assert len(app.saves(save_path(app))) == 1
    assert app.prediction()["home_score"] == 1

    pending.release()
    assert_saved(app, 3)
    assert [r.payload["predicted_home_score"] for r in app.saves(save_path(app))] == [
        1,
        3,
    ]


@pytest.mark.parametrize("persist", [False, True], ids=["before-write", "after-write"])
def test_network_failure_retry_persists_latest_score(browser_app, persist):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    app.fail_next(save_path(app), persist=persist)
    score(app, 2, 1)
    app.advance()
    expect(status(app)).to_have_attribute("data-save-state", "error")
    assert app.prediction() is not None if persist else app.prediction() is None
    match_form(app).get_by_role("button", name="Повторить сохранение").click()
    assert_saved(app, 2, 1)
    assert len(app.saves(save_path(app))) == 2


@pytest.mark.parametrize(
    "revert_after_error", [False, True], ids=["while-pending", "after-error"]
)
def test_lost_response_then_reverting_to_old_score_really_saves_it(
    browser_app, revert_after_error
):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    score(app, 1)
    app.advance()
    assert_saved(app, 1)

    pending = app.hold_next(save_path(app), persist=True)
    score(app, 2)
    app.advance()
    pending.wait()
    assert app.prediction()["home_score"] == 2
    if not revert_after_error:
        score(app, 1)
    pending.abort()
    if revert_after_error:
        expect(status(app)).to_have_attribute("data-save-state", "error")
        score(app, 1)
    app.advance()
    assert_saved(app, 1)
    assert [r.payload["predicted_home_score"] for r in app.saves(save_path(app))] == [
        1,
        2,
        1,
    ]


def test_draw_requires_advancing_team_before_confirmation(browser_app):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    score(app, 1, 1)
    app.advance()
    expect(status(app)).to_have_attribute("data-save-state", "draft")
    expect(status(app)).to_contain_text("При ничьей")
    assert app.prediction() is None
    assert app.saves(save_path(app)) == []

    match_id = app.matches[0]["id"]
    app.page.locator(f"#match-{match_id}-prediction-advancing-away").check()
    app.advance()
    assert_saved(app, 1, 1)
    assert app.prediction()["advancing_team_id"] == app.matches[0]["away_team_id"]


def test_navigation_flushes_debounce_and_waits_before_reading_contest(browser_app):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    pending = app.hold_next(save_path(app))
    score(app, 2, 1)
    assert app.saves(save_path(app)) == []
    requests_before_navigation = len(app.requests)
    app.page.get_by_role("button", name="Рейтинг", exact=True).click()
    pending.wait()
    expect(
        app.page.get_by_role("heading", name="Открываем конкурс", exact=True)
    ).to_be_visible()
    assert not any(
        r.method == "GET" and r.path == app.contest_path
        for r in app.requests[requests_before_navigation:]
    )
    pending.release()
    expect(
        app.page.get_by_role("button", name="Рейтинг", exact=True)
    ).to_have_attribute("aria-current", "page")
    assert app.prediction()["home_score"] == 2
    app.page.get_by_role("button", name="Матчи", exact=True).click()
    assert_saved(app, 2, 1)


def test_detached_form_saves_latest_edit_without_replacing_contest_list(browser_app):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    pending = app.hold_next(save_path(app), persist=True)
    score(app, 1)
    app.advance()
    pending.wait()
    score(app, 3, 1)
    app.page.get_by_role("button", name="← Все конкурсы", exact=True).click()
    expect(
        app.page.get_by_role("heading", name="Активные конкурсы", exact=True)
    ).to_be_visible()
    pending.release()
    app.page.get_by_role("button", name=app.contest["name"], exact=True).click()
    app.page.get_by_role("button", name="Матчи", exact=True).click()
    assert_saved(app, 3, 1)
    assert [r.payload["predicted_home_score"] for r in app.saves(save_path(app))] == [
        1,
        3,
    ]


def test_navigation_keeps_draft_when_initial_save_and_automatic_retry_fail(browser_app):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    score(app, 1)
    app.advance()
    assert_saved(app, 1)
    app.fail_next(save_path(app))
    app.fail_next(save_path(app))
    score(app, 3, 1)
    app.page.get_by_role("button", name="Рейтинг", exact=True).click()
    expect(
        app.page.get_by_role("heading", name="Не удалось открыть конкурс", exact=True)
    ).to_be_visible()
    assert len(app.saves(save_path(app))) == 3
    assert app.prediction()["home_score"] == 1

    app.page.get_by_role("button", name="К списку конкурсов", exact=True).click()
    app.page.get_by_role("button", name=app.contest["name"], exact=True).click()
    app.page.get_by_role("button", name="Матчи", exact=True).click()
    assert_saved(app, 3, 1)
    assert len(app.saves(save_path(app))) == 4


def test_invalid_success_response_does_not_confirm_save(browser_app):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    pending = app.hold_next(save_path(app), persist=True)
    score(app, 2)
    app.advance()
    pending.respond(json={"prediction": {"home_score": 99, "away_score": 0}})
    expect(status(app)).to_have_attribute("data-save-state", "error")
    match_form(app).get_by_role("button", name="Повторить сохранение").click()
    assert_saved(app, 2)


def test_navigation_retries_malformed_acknowledgement_before_loading(browser_app):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    pending = app.hold_next(save_path(app), persist=True)
    score(app, 2)
    app.page.get_by_role("button", name="Рейтинг", exact=True).click()
    pending.respond(json={"prediction": None})
    expect(
        app.page.get_by_role("button", name="Рейтинг", exact=True)
    ).to_have_attribute("aria-current", "page")
    assert len(app.saves(save_path(app))) == 2
    assert app.prediction()["home_score"] == 2


@pytest.mark.parametrize("event_name", ["pagehide", "visibilitychange"])
def test_background_lifecycle_flushes_pending_score(browser_app, event_name):
    app = browser_app
    app.open()
    score(app, 2)
    assert app.saves(save_path(app)) == []
    if event_name == "visibilitychange":
        app.page.evaluate(
            "Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'}); document.dispatchEvent(new Event('visibilitychange'))"
        )
    else:
        app.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    assert_saved(app, 2)
    assert len(app.saves(save_path(app))) == 1


def test_edit_just_before_deadline_saves_without_normal_debounce(browser_app):
    app = browser_app
    app.open()
    app.page.clock.set_system_time(app.now + timedelta(seconds=3599.5))
    app.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
    score(app, 2)
    app.advance(1)
    assert_saved(app, 2)


def test_resume_after_deadline_closes_unsaved_draft_without_sending(browser_app):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    score(app, 2)
    app.page.clock.set_system_time(app.now + timedelta(seconds=3600.001))
    app.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
    expect(status(app)).to_have_attribute("data-save-state", "closed")
    expect(status(app)).to_contain_text("Последние изменения не сохранены")
    expect(match_form(app).locator("input[type=number]").first).to_be_disabled()
    app.advance()
    assert app.saves(save_path(app)) == []
    assert app.prediction() is None


def test_server_deadline_rejection_closes_form_and_stops_retry(
    browser_app, monkeypatch
):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    monkeypatch.setattr(
        contest_service, "_resolve_now_utc", lambda value: app.now + timedelta(hours=2)
    )
    score(app, 2)
    app.advance()
    expect(status(app)).to_have_attribute("data-save-state", "closed")
    expect(match_form(app).locator("input[type=number]").first).to_be_disabled()
    expect(
        match_form(app).get_by_role("button", name="Повторить сохранение")
    ).to_be_hidden()
    app.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    app.advance()
    assert len(app.saves(save_path(app))) == 1
    assert app.prediction() is None


def test_save_timeout_keeps_last_draft_and_late_response_preserves_current_view(
    browser_app,
):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    pending = app.hold_next(save_path(app), persist=True)
    score(app, 3, 1)
    app.page.get_by_role("button", name="Рейтинг", exact=True).click()
    pending.wait()
    app.advance(15_001)
    expect(
        app.page.get_by_role("heading", name="Не удалось открыть конкурс", exact=True)
    ).to_be_visible()
    expect(
        app.page.get_by_text("Не удалось дождаться сохранения прогноза", exact=False)
    ).to_be_visible()
    app.page.get_by_role("button", name="К списку конкурсов", exact=True).click()
    pending.release()
    expect(
        app.page.get_by_role("heading", name="Активные конкурсы", exact=True)
    ).to_be_visible()
    app.page.get_by_role("button", name=app.contest["name"], exact=True).click()
    app.page.get_by_role("button", name="Матчи", exact=True).click()
    assert_saved(app, 3, 1)
    assert len(app.saves(save_path(app))) == 1


def test_deleted_match_error_does_not_block_reopening_remaining_contest(browser_app):
    from playwright.sync_api import expect

    app = browser_app
    app.open()
    response = app.request(
        "DELETE", f"{app.contest_path}/matches/{app.matches[0]['id']}"
    )
    assert response.status_code == 204, response.text
    score(app, 2)
    app.page.get_by_role("button", name="Рейтинг", exact=True).click()
    expect(
        app.page.get_by_role("heading", name="Не удалось открыть конкурс", exact=True)
    ).to_be_visible()
    app.page.get_by_role("button", name="К списку конкурсов", exact=True).click()
    app.page.get_by_role("button", name=app.contest["name"], exact=True).click()
    app.page.get_by_role("button", name="Матчи", exact=True).click()
    expect(app.page.get_by_role("button", name="Матчи", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    expect(app.page.locator(".match-prediction-form")).to_have_count(0)
    assert app.refresh_contest()["matches"] == []
    assert len(app.saves(save_path(app))) == 1

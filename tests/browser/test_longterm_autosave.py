"""Exercise participant choices through the shipped TMA and real API storage."""

from __future__ import annotations

from datetime import timedelta

import pytest


def _expect(locator):
    # Keep the optional browser dependency out of normal test collection.
    from playwright.sync_api import expect

    return expect(locator)


def _enable_champion(app, *, seconds=3600):
    response = app.request(
        "PUT",
        f"{app.contest_path}/champion-prediction/settings",
        json={
            "enabled": True,
            "deadline_at": app.iso_after(seconds=seconds),
            "points": 5,
        },
    )
    assert response.status_code == 200, response.text
    return f"{app.contest_path}/champion-prediction"


def _enable_stage(app, *, seconds=3600):
    response = app.request(
        "PUT",
        f"{app.contest_path}/swiss-stage-prediction/settings",
        json={
            "enabled": True,
            "deadline_at": app.iso_after(seconds=seconds),
            "direct_qualifier_count": 8,
            "elimination_qualifier_count": 12,
        },
    )
    assert response.status_code == 200, response.text
    return f"{app.contest_path}/swiss-stage-prediction"


def _create_pair(app, *, seconds=3600):
    response = app.request(
        "POST",
        f"{app.contest_path}/two-legged-ties",
        json={
            "first_team_id": app.teams[2]["id"],
            "second_team_id": app.teams[3]["id"],
            "first_leg_starts_at_utc": app.iso_after(seconds=seconds),
            "second_leg_starts_at_utc": app.iso_after(seconds=seconds + 86400),
            "round_key": "playoff",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["two_legged_tie"]


def _prepare_choice(app, kind, *, seconds=3600):
    """Return only UI locators and IDs received from actual application services."""
    if kind == "champion":
        path = _enable_champion(app, seconds=seconds)
        app.open("tournament")
        form = app.page.locator(".champion-card .champion-choice-form")
        choices = [app.teams[0], app.teams[1]]
    elif kind == "stage":
        path = _enable_stage(app, seconds=seconds)
        app.open("tournament")
        form = app.page.locator(".swiss-stage-selection-form")
        choices = [app.teams[0]]
    else:
        tie = _create_pair(app, seconds=seconds)
        path = f"{app.contest_path}/two-legged-ties/{tie['id']}/prediction"
        app.open()
        app.page.get_by_role("tab", name=tie["round_name"], exact=True).click()
        form = app.page.locator(".two-legged-tie-prediction-form")
        choices = [tie["first_team"], tie["second_team"]]
    _expect(form).to_be_visible()
    return path, form, choices


def _choose(form, kind, choices, index):
    if kind == "champion":
        form.get_by_role("combobox", name="Чемпион турнира").select_option(
            str(choices[index]["id"])
        )
    elif kind == "stage":
        category = "Напрямую" if index == 0 else "Вылет"
        form.get_by_role(
            "button", name=f"{choices[0]['name']}: {category}", exact=True
        ).click()
    else:
        form.get_by_role("radio", name=choices[index]["name"], exact=True).check()


def _stored_choice(app, kind):
    contest = app.refresh_contest()
    if kind == "champion":
        prediction = contest["champion_prediction"]["prediction"]
        return prediction["id"] if prediction else None
    if kind == "stage":
        prediction = contest["swiss_stage_prediction"]["prediction"]
        if not prediction:
            return None
        return (
            [team["id"] for team in prediction["direct_teams"]],
            [team["id"] for team in prediction["elimination_teams"]],
        )
    prediction = contest["two_legged_ties"][0]["prediction"]
    return prediction["advancing_team_id"] if prediction else None


def _expected_choice(kind, choices, index):
    if kind == "stage":
        team_id = choices[0]["id"]
        return ([team_id], []) if index == 0 else ([], [team_id])
    return choices[index]["id"]


@pytest.mark.parametrize("kind", ["champion", "stage", "tie"])
def test_latest_choice_waits_for_previous_response_and_survives_reopen(
    browser_app, kind
):
    app = browser_app
    path, form, choices = _prepare_choice(app, kind)
    pending = app.hold_next(path, persist=True)
    _choose(form, kind, choices, 0)
    app.advance()
    pending.wait()
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 0)

    _choose(form, kind, choices, 1)
    app.advance()
    _expect(form.locator(".form-message")).not_to_have_attribute(
        "data-save-state", "saved"
    )
    assert len(app.saves(path)) == 1
    pending.release()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 1)
    assert len(app.saves(path)) == 2

    app.open("matches" if kind == "tie" else "tournament")
    if kind == "tie":
        app.page.get_by_role("tab", name="Стыковые матчи", exact=True).click()
    if kind == "champion":
        _expect(form.get_by_role("combobox", name="Чемпион турнира")).to_have_value(
            str(choices[1]["id"])
        )
    elif kind == "stage":
        _expect(
            form.get_by_role("button", name=f"{choices[0]['name']}: Вылет", exact=True)
        ).to_have_attribute("aria-pressed", "true")
    else:
        _expect(
            form.get_by_role("radio", name=choices[1]["name"], exact=True)
        ).to_be_checked()


@pytest.mark.parametrize("kind", ["champion", "stage", "tie"])
def test_reverting_choice_after_lost_response_really_restores_saved_prediction(
    browser_app, kind
):
    app = browser_app
    path, form, choices = _prepare_choice(app, kind)
    _choose(form, kind, choices, 0)
    app.advance()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")

    pending = app.hold_next(path, persist=True)
    _choose(form, kind, choices, 1)
    app.advance()
    pending.wait()
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 1)
    _choose(form, kind, choices, 0)
    pending.abort()

    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 0)
    assert len(app.saves(path)) == 3


@pytest.mark.parametrize("kind", ["champion", "stage", "tie"])
def test_retry_after_lost_response_confirms_one_persisted_prediction(browser_app, kind):
    app = browser_app
    path, form, choices = _prepare_choice(app, kind)
    app.fail_next(path, persist=True)
    _choose(form, kind, choices, 0)
    app.advance()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "error")
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 0)

    form.get_by_role("button", name="Повторить сохранение", exact=True).click()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 0)
    assert len(app.saves(path)) == 2
    counter = {
        "champion": "champion_prediction_count",
        "stage": "swiss_stage_prediction_count",
        "tie": "two_legged_tie_predictions_count",
    }[kind]
    assert app.refresh_contest()["leaderboard"][0][counter] == 1


def test_clearing_initial_stage_choice_after_lost_response_leaves_no_prediction(
    browser_app,
):
    app = browser_app
    path, form, choices = _prepare_choice(app, "stage")
    pending = app.hold_next(path, persist=True)
    _choose(form, "stage", choices, 0)
    app.advance()
    pending.wait()
    assert _stored_choice(app, "stage") == ([choices[0]["id"]], [])

    form.get_by_role("button", name=f"{choices[0]['name']}: Стыки", exact=True).click()
    pending.abort()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "empty")
    assert _stored_choice(app, "stage") is None
    assert len(app.saves(path)) == 2


def test_stage_partial_full_and_cleared_snapshots_are_persisted_automatically(
    browser_app,
):
    app = browser_app
    path = _enable_stage(app)
    app.open("tournament")
    form = app.page.locator(".swiss-stage-selection-form")
    progress = form.get_by_role("status", name="Заполнение прогноза")
    message = form.locator(".form-message")
    _expect(progress).to_have_text("В 1/8: 0/8На вылет: 0/12")
    app.advance()
    assert not app.saves(path)
    assert _stored_choice(app, "stage") is None

    first = app.teams[0]
    form.get_by_role("button", name=f"{first['name']}: Напрямую", exact=True).click()
    app.advance()
    _expect(message).to_have_text("Прогноз сохранён. Можно дополнить до 8+12.")
    partial = app.refresh_contest()["swiss_stage_prediction"]["prediction"]
    assert partial["is_complete"] is False
    assert [team["id"] for team in partial["direct_teams"]] == [first["id"]]
    assert partial["elimination_teams"] == []

    for team in app.teams[1:8]:
        form.get_by_role("button", name=f"{team['name']}: Напрямую", exact=True).click()
    for team in app.teams[8:20]:
        form.get_by_role("button", name=f"{team['name']}: Вылет", exact=True).click()
    app.advance()
    _expect(message).to_have_text("Прогноз сохранён и заполнен полностью.")
    _expect(progress).to_have_text("В 1/8: 8/8На вылет: 12/12")
    complete = app.refresh_contest()["swiss_stage_prediction"]["prediction"]
    assert complete["is_complete"] is True
    assert {team["id"] for team in complete["direct_teams"]} == {
        team["id"] for team in app.teams[:8]
    }
    assert {team["id"] for team in complete["elimination_teams"]} == {
        team["id"] for team in app.teams[8:20]
    }
    assert len(complete["playoff_teams"]) == 16
    _expect(
        form.get_by_role(
            "button", name=f"{app.teams[20]['name']}: Напрямую", exact=True
        )
    ).to_be_disabled()
    _expect(
        form.get_by_role("button", name=f"{app.teams[20]['name']}: Вылет", exact=True)
    ).to_be_disabled()

    for team in app.teams[:20]:
        form.get_by_role("button", name=f"{team['name']}: Стыки", exact=True).click()
    app.advance()
    _expect(message).to_have_attribute("data-save-state", "empty")
    _expect(progress).to_have_text("В 1/8: 0/8На вылет: 0/12")
    cleared = app.refresh_contest()
    assert cleared["swiss_stage_prediction"]["prediction"] is None
    assert cleared["swiss_stage_prediction"]["settings_locked"] is True
    # The API omits this optional counter when the participant has no stage bet.
    assert cleared["leaderboard"][0].get("swiss_stage_prediction_count", 0) == 0
    assert cleared["leaderboard"][0]["calculated_predictions_count"] == 0


@pytest.mark.parametrize("kind", ["champion", "stage", "tie"])
def test_server_deadline_rejection_never_confirms_unsaved_change(
    browser_app, monkeypatch, kind
):
    app = browser_app
    path, form, choices = _prepare_choice(app, kind)
    _choose(form, kind, choices, 0)
    app.advance()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")
    monkeypatch.setattr("app.tma_api._utc_now", lambda: app.now + timedelta(hours=2))
    monkeypatch.setattr(
        "app.contest_service._resolve_now_utc",
        lambda now_utc: now_utc or app.now + timedelta(hours=2),
    )
    _choose(form, kind, choices, 1)
    with app.page.expect_response(
        lambda response: response.url.endswith(path)
    ) as response:
        app.advance()
    assert response.value.status == 409
    _expect(form.locator(".form-message")).to_have_attribute(
        "data-save-state", "closed"
    )
    _expect(form.locator(".form-message")).to_contain_text(
        "не сохранен" if kind == "tie" else "не сохранены"
    )
    _expect(form.get_by_role("button", name="Повторить сохранение")).to_be_hidden()
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 0)
    app.advance(10000)
    assert len(app.saves(path)) == 2


@pytest.mark.parametrize("kind", ["champion", "stage", "tie"])
def test_return_from_background_closes_deadline_and_preserves_confirmed_choice(
    browser_app, kind
):
    app = browser_app
    path, form, choices = _prepare_choice(app, kind)
    _choose(form, kind, choices, 0)
    app.advance()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")

    # WebViews may suspend timers. Jump wall time without running them, then
    # deliver the browser lifecycle event used when the page is restored.
    app.page.clock.set_system_time(app.now + timedelta(hours=2))
    app.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
    _expect(form).to_have_attribute("aria-disabled", "true")
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")
    if kind == "champion":
        _expect(form.get_by_role("combobox", name="Чемпион турнира")).to_be_disabled()
    elif kind == "stage":
        _expect(
            form.get_by_role("button", name=f"{choices[0]['name']}: Вылет", exact=True)
        ).to_be_disabled()
    else:
        _expect(
            form.get_by_role("radio", name=choices[1]["name"], exact=True)
        ).to_be_disabled()
    assert len(app.saves(path)) == 1
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 0)


@pytest.mark.parametrize("kind", ["champion", "stage", "tie"])
def test_response_received_after_deadline_confirms_only_the_accepted_choice(
    browser_app, kind
):
    app = browser_app
    path, form, choices = _prepare_choice(app, kind)
    pending = app.hold_next(path, persist=True)
    _choose(form, kind, choices, 0)
    app.advance()
    pending.wait()
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 0)

    app.page.clock.set_system_time(app.now + timedelta(hours=2))
    app.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
    _expect(form).to_have_attribute("aria-disabled", "true")
    _expect(form.locator(".form-message")).to_have_attribute(
        "data-save-state", "closed"
    )
    pending.release()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")
    assert len(app.saves(path)) == 1
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 0)


@pytest.mark.parametrize("kind", ["champion", "stage", "tie"])
def test_deadline_during_lost_response_never_confirms_reverted_draft(browser_app, kind):
    app = browser_app
    path, form, choices = _prepare_choice(app, kind)
    _choose(form, kind, choices, 0)
    app.advance()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")

    pending = app.hold_next(path, persist=True)
    _choose(form, kind, choices, 1)
    app.advance()
    pending.wait()
    _choose(form, kind, choices, 0)
    app.page.clock.set_system_time(app.now + timedelta(hours=2))
    app.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
    _expect(form.locator(".form-message")).to_have_attribute(
        "data-save-state", "closed"
    )
    pending.abort()
    app.advance()
    _expect(form.locator(".form-message")).to_have_attribute(
        "data-save-state", "closed"
    )
    _expect(form.locator(".form-message")).to_contain_text("не подтверждено")
    assert _stored_choice(app, kind) == _expected_choice(kind, choices, 1)
    assert len(app.saves(path)) == 2


def test_second_leg_draw_saves_after_first_leg_deadline_without_new_passage_choice(
    browser_app, monkeypatch
):
    app = browser_app
    tie = _create_pair(app)
    app.open()
    app.page.get_by_role("tab", name=tie["round_name"], exact=True).click()
    tie_form = app.page.locator(".two-legged-tie-prediction-form")
    tie_form.get_by_role("radio", name=tie["first_team"]["name"], exact=True).check()
    app.advance()
    _expect(tie_form.locator(".form-message")).to_have_attribute(
        "data-save-state", "saved"
    )

    after_first_leg = app.now + timedelta(hours=2)
    monkeypatch.setattr("app.tma_api._utc_now", lambda: after_first_leg)
    monkeypatch.setattr(
        "app.contest_service._resolve_now_utc",
        lambda now_utc: now_utc or after_first_leg,
    )
    app.page.clock.set_system_time(after_first_leg)
    app.page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
    _expect(
        tie_form.get_by_role("radio", name=tie["second_team"]["name"], exact=True)
    ).to_be_disabled()

    second_leg_id = tie["second_leg_match_id"]
    score = app.page.locator(f"#match-{second_leg_id}-home-score")
    form = score.locator("xpath=ancestor::form")
    _expect(score).to_be_enabled()
    score.fill("1")
    form.get_by_label(tie["first_team"]["name"], exact=True).fill("1")
    app.advance()
    _expect(form.locator(".form-message")).to_have_attribute("data-save-state", "saved")
    _expect(form.get_by_role("radio")).to_have_count(0)
    prediction = app.prediction(second_leg_id)
    assert prediction["home_score"] == 1
    assert prediction["away_score"] == 1
    assert prediction["advancing_team_id"] is None
    assert _stored_choice(app, "tie") == tie["first_team"]["id"]
    tie_path = f"{app.contest_path}/two-legged-ties/{tie['id']}/prediction"
    assert len(app.saves(tie_path)) == 1

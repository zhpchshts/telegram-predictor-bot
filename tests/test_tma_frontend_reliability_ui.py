from __future__ import annotations

import re

from app import main


def _source() -> str:
    return (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    source = _source()
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


def test_async_navigation_ignores_responses_for_replaced_views() -> None:
    source = _source()
    replace_source = _function_source("replaceAppContent")

    assert source.count("appContentElement.replaceChildren") == 1
    assert "currentViewToken += 1" in replace_source
    assert "return currentViewToken" in replace_source

    for function_name in (
        "openContest",
        "openSharedTournamentManagement",
        "openSharedTournament",
        "openManagement",
        "openContestList",
        "initialize",
    ):
        function_source = _function_source(function_name)
        assert "viewToken" in function_source
        assert function_source.count("if (!isCurrentView(viewToken))") >= 2

    audit_source = _function_source("loadAuditEvents")
    assert "const viewToken = renderAuditScreen" in audit_source
    assert "isCurrentView(viewToken)" in audit_source


def test_awaited_ui_requests_cannot_redraw_a_replaced_view() -> None:
    source = _source()
    request_source = _function_source("apiRequestForCurrentView")
    management_error_source = _function_source("handleManagementRequestError")

    assert "const viewToken = currentViewToken" in request_source
    assert request_source.count("isCurrentView(viewToken)") == 2
    assert request_source.count("throw new StaleViewRequestError()") == 2
    assert source.count("await apiRequest(") == 1
    assert "error instanceof StaleViewRequestError" in management_error_source
    assert source.count("await apiRequestForCurrentView(") >= 30


def test_match_autosaves_are_flushed_and_serialized_across_renders() -> None:
    source = _source()
    replace_source = _function_source("replaceAppContent")
    flush_source = _function_source("flushMatchPredictionForms")
    queue_source = _function_source("queueMatchPredictionSave")
    wait_source = _function_source("waitForMatchPredictionSaves")
    prediction_source = _function_source("createMatchPredictionSection")
    open_source = _function_source("openContest")

    assert "flushMatchPredictionForms()" in replace_source
    assert "PREDICTION_FLUSH_EVENT" in flush_source
    assert "previousSave" in queue_source
    assert ".catch(() => undefined)" in queue_source
    assert "void trackedRequest.catch(() => undefined)" in queue_source
    assert "keepalive: true" in queue_source
    assert source.count("keepalive: true") == 1
    assert ".then(sendSave)" in queue_source
    assert ": sendSave()" in queue_source
    assert "matchPredictionSaveQueues.set" in queue_source
    assert "matchPredictionSaveQueues.delete" in queue_source
    assert "while (true)" in wait_source
    assert "Promise.allSettled(pendingSaves)" in wait_source
    assert "Promise.race([drainSaves(), timeout])" in wait_source
    assert "PREDICTION_SAVE_WAIT_TIMEOUT_MS" in wait_source
    assert "Не удалось дождаться сохранения прогноза" in wait_source
    assert "window.clearTimeout(timeoutId)" in wait_source
    timeout_match = re.search(
        r"const PREDICTION_SAVE_WAIT_TIMEOUT_MS = ([\d_]+);",
        source,
    )
    assert timeout_match is not None
    assert 0 < int(timeout_match.group(1).replace("_", "")) <= 15_000
    assert "queueMatchPredictionSave(" in prediction_source
    assert "form.addEventListener(PREDICTION_FLUSH_EVENT" in prediction_source
    assert "await waitForMatchPredictionSaves(contestId)" in open_source


def test_match_autosave_flushes_on_hard_close_lifecycle_events() -> None:
    source = _source()
    flush_source = _function_source("flushMatchPredictionForms")

    assert 'document.visibilityState === "hidden"' in source
    assert 'window.addEventListener("pagehide", flushMatchPredictionForms)' in source
    assert (
        'window.addEventListener("pageshow", syncVisiblePredictionDeadlines)' in source
    )
    assert 'appContentElement.querySelectorAll(\n    ".match-prediction-form",' in (
        flush_source
    )
    assert "form.dispatchEvent(new Event(PREDICTION_FLUSH_EVENT))" in flush_source


def test_match_autosave_closes_stale_form_after_deadline() -> None:
    source = _source()
    prediction_source = _function_source("createMatchPredictionSection")

    assert "PREDICTION_DEADLINE_SYNC_EVENT" in source
    assert 'document.visibilityState === "visible"' in source
    assert 'window.addEventListener("pageshow"' in source
    assert "function syncPredictionDeadline()" in prediction_source
    assert "if (isMatchPredictionOpen(match))" in prediction_source
    assert "homeScoreInput.disabled = true" in prediction_source
    assert "awayScoreInput.disabled = true" in prediction_source
    assert "seriesScoreInput.disabled = true" in prediction_source
    assert 'form.setAttribute("aria-disabled", "true")' in prediction_source
    assert "Последние изменения не сохранены" in prediction_source
    assert "function schedulePredictionDeadlineSync()" in prediction_source
    assert "Math.min(remaining, MAX_TIMER_DELAY_MS)" in prediction_source
    assert "!form.isConnected" in prediction_source
    assert "window.clearTimeout(deadlineTimer)" in prediction_source
    assert "lastSavedFingerprint = null" not in prediction_source


def test_dynamic_content_uses_safe_text_and_accessible_statuses() -> None:
    source = _source()
    create_element_source = _function_source("createElement")
    form_message_source = _function_source("setFormMessage")
    status_card_source = _function_source("createStatusCard")

    for unsafe_sink in (
        ".innerHTML",
        ".outerHTML",
        "insertAdjacentHTML",
        "document.write(",
        "eval(",
        "new Function(",
    ):
        assert unsafe_sink not in source
    assert "element.textContent = text" in create_element_source
    assert 'type === "error" ? "alert" : "status"' in form_message_source
    assert 'card.setAttribute("role", "status")' in status_card_source


def test_tma_limits_referrer_cache_and_respects_mobile_safe_areas() -> None:
    index = (main.TMA_DIRECTORY / "index.html").read_text(encoding="utf-8")
    styles = (main.TMA_DIRECTORY / "styles.css").read_text(encoding="utf-8")
    request_source = _function_source("apiRequest")

    assert '<meta name="referrer" content="no-referrer" />' in index
    assert '<h1 class="visually-hidden">Клевер</h1>' in index
    assert 'cache: "no-store"' in request_source
    assert "calc(20px + env(safe-area-inset-top))" in styles
    assert "calc(16px + env(safe-area-inset-right))" in styles
    assert "calc(28px + env(safe-area-inset-bottom))" in styles
    assert "calc(16px + env(safe-area-inset-left))" in styles
    assert ".contest-back-link {" in styles
    assert ".contest-tab {" in styles
    assert ".info-card ol:not([class])" in styles
    assert ".info-card ol {" not in styles
    assert "@media (max-width: 420px)" in styles
    assert ".match-card-header {\n    display: grid;" in styles
    assert styles.count("min-height: 44px") >= 10


def test_initial_network_error_is_actionable_and_retryable() -> None:
    request_source = _function_source("apiRequest")
    render_error_source = _function_source("renderError")
    handle_error_source = _function_source("handleError")

    assert "Не удалось связаться с сервером" in request_source
    assert "Проверьте соединение и повторите попытку" in request_source
    assert '"Попробовать снова"' in render_error_source
    assert "void initialize()" in render_error_source
    assert "renderError(message, { canRetry: true })" in handle_error_source

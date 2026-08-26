from __future__ import annotations

import re

from app import main
from app.audit_service import AuditEventType


def _source() -> str:
    return (main.TMA_DIRECTORY / "app.js").read_text(encoding="utf-8")


def _function_source(name: str, _next_name: str | None = None) -> str:
    source = _source()
    match = re.search(
        rf"^(?:async )?function {re.escape(name)}\(",
        source,
        flags=re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"Function {name!r} was not found.")
    start = match.start()
    next_match = re.search(
        r"^(?:async )?function [A-Za-z_$][\w$]*\(",
        source[match.end() :],
        flags=re.MULTILINE,
    )
    end = len(source) if next_match is None else match.end() + next_match.start()
    return source[start:end]


def test_audit_ui_has_central_russian_labels_for_every_event_type() -> None:
    source = _source()
    registry_start = source.index("const AUDIT_EVENT_PRESENTATIONS")
    registry_end = source.index("const AUDIT_ROLE_LABELS", registry_start)
    registry = source[registry_start:registry_end]
    expected_labels = {
        AuditEventType.CONTEST_CREATED: "Создан конкурс",
        AuditEventType.CONTEST_UPDATED: "Изменены настройки конкурса",
        AuditEventType.CONTEST_FINISHED: "Конкурс завершён",
        AuditEventType.CONTEST_DELETED: "Удалён конкурс",
        AuditEventType.MATCH_CREATED: "Создан матч",
        AuditEventType.MATCH_UPDATED: "Изменено время матча",
        AuditEventType.MATCH_DELETED: "Удалён матч",
        AuditEventType.MATCH_RESULT_SET: "Внесён результат матча",
        AuditEventType.MATCH_RESULT_CHANGED: "Изменён результат матча",
        AuditEventType.CONTEST_CHAMPION_SET: "Указан чемпион",
        AuditEventType.CONTEST_CHAMPION_CHANGED: "Изменён чемпион",
        AuditEventType.INTERMEDIATE_LEADERBOARD_PUBLICATION_REQUESTED: (
            "Запрошена публикация промежуточного рейтинга"
        ),
        AuditEventType.SUPERMODERATOR_ASSIGNED: "Назначен супермодератор",
        AuditEventType.SUPERMODERATOR_REVOKED: ("Отозвана роль супермодератора"),
    }

    for event_type, label in expected_labels.items():
        assert f"{event_type.value}:" in registry
        assert f'label: "{label}"' in registry
    assert 'telegram_admin: "Администратор Telegram"' in source
    assert 'supermoderator: "Супермодератор"' in source
    assert 'participant: "Участник"' in source


def test_audit_ui_builds_human_readable_change_summaries() -> None:
    result_source = _function_source(
        "buildMatchResultAuditChanges",
        "buildAuditSummaryLines",
    )
    summary_source = _function_source(
        "buildAuditSummaryLines",
        "formatAuditStateValue",
    )
    contest_source = _function_source(
        "buildContestAuditChanges",
        "buildMatchResultAuditChanges",
    )
    title_source = _function_source(
        "getAuditEventTitle",
        "formatAuditDateTime",
    )

    assert "Результат: ${beforeScore} → ${afterScore}" in result_source
    assert "Прошла дальше:" in result_source
    assert "Статус:" in result_source
    assert "Публикация прогнозов" in contest_source
    assert "Прогноз на чемпиона" in contest_source
    assert "Дедлайн прогноза на чемпиона" in contest_source
    assert 'case "contest_champion_changed"' in summary_source
    assert "Чемпион изменён:" in summary_source
    assert 'case "contest_created"' in summary_source
    assert 'case "contest_deleted"' in summary_source
    assert 'case "match_created"' in summary_source
    assert 'case "match_deleted"' in summary_source
    assert 'case "supermoderator_assigned"' in summary_source
    assert 'case "supermoderator_revoked"' in summary_source
    assert 'case "intermediate_leaderboard_publication_requested"' in summary_source
    assert "Включена публикация прогнозов" in title_source
    assert "Выключена публикация прогнозов" in title_source


def test_audit_ui_renders_structured_before_and_after_details() -> None:
    source = _source()
    card_source = _function_source(
        "createAuditEventCard",
        "createAuditFilterSelect",
    )
    state_source = _function_source(
        "createAuditStatePanel",
        "createAuditEventCard",
    )

    assert '"До"' in card_source
    assert "event.before_state" in card_source
    assert '"После"' in card_source
    assert "event.after_state" in card_source
    assert "Сущность не существовала." in card_source
    assert "Сущность удалена." in card_source
    assert '"Показать подробности"' in card_source
    assert 'createElement("dl"' in state_source
    assert "JSON.stringify(value, null, 2)" in source


def test_audit_ui_has_loading_empty_filtered_and_error_states() -> None:
    list_source = _function_source(
        "createAuditListCard",
        "createAuditHeaderCard",
    )

    assert "Загружаем историю" in list_source
    assert "История действий пока пуста." in list_source
    assert "По выбранным фильтрам действий нет." in list_source
    assert "Попробовать снова" in list_source
    assert "Повторить загрузку" in list_source
    assert "state.loading && !state.initialized" in list_source


def test_audit_ui_applies_filters_and_resets_to_the_first_page() -> None:
    filters_source = _function_source(
        "createAuditFiltersCard",
        "hasActiveAuditFilters",
    )
    request_source = _function_source(
        "buildAuditRequestPath",
        "loadAuditEvents",
    )

    assert '"Все конкурсы"' in filters_source
    assert '"Без конкурса"' in filters_source
    assert '"Все действия"' in filters_source
    assert '"Все исполнители"' in filters_source
    assert '" · удалён"' in filters_source
    assert "void loadAuditEvents(bootstrap, state, false)" in filters_source
    assert 'parameters.set("contest_id"' in request_source
    assert 'parameters.set("event_type"' in request_source
    assert 'parameters.set("actor_user_id"' in request_source
    assert (
        'parameters.set("entity_type", "supermoderator_assignment")' in request_source
    )


def test_audit_ui_appends_cursor_pages_without_duplicate_events() -> None:
    load_source = _function_source(
        "loadAuditEvents",
        "openAuditHistory",
    )
    list_source = _function_source(
        "createAuditListCard",
        "createAuditHeaderCard",
    )
    request_source = _function_source(
        "buildAuditRequestPath",
        "loadAuditEvents",
    )

    assert 'parameters.set("cursor", state.nextCursor)' in request_source
    assert "append ? [...state.events, ...incomingEvents]" in load_source
    assert "const seenEventIds = new Set()" in load_source
    assert "seenEventIds.has(event.id)" in load_source
    assert "state.nextCursor = result.next_cursor || null" in load_source
    assert '"Показать ещё"' in list_source
    assert "loadMoreButton.disabled = state.loading" in list_source


def test_audit_navigation_uses_the_existing_administrative_access_decision() -> None:
    source = _source()
    screen_source = _function_source(
        "renderContestScreen",
        "renderBootstrap",
    )
    management_source = _function_source(
        "renderManagementScreen",
        "openManagement",
    )
    access_source = _function_source("createManagementAccessCard")

    assert "if (canManageContests(bootstrap))" in screen_source
    assert "createManagementNavigationCard(bootstrap)" in screen_source
    assert "createManagementAccessCard(bootstrap, capabilities)" in management_source
    assert "capabilities.can_read_audit === true" in access_source
    assert "openAuditHistory(bootstrap)" in access_source
    assert '"/api/tma/audit-events?' not in source
    assert "`/api/tma/audit-events?${parameters.toString()}`" in source


def test_participant_and_management_contours_are_separate() -> None:
    participant_source = _function_source(
        "renderContestDetailsScreen",
        "renderContestDetailsRoute",
    )
    management_source = _function_source(
        "renderContestManagementScreen",
        "openContest",
    )
    contest_list_source = _function_source(
        "renderContestScreen",
        "renderBootstrap",
    )

    assert "canManageResults: false" in participant_source
    assert "createMatchFormCard" not in participant_source
    assert "createContestCompletionCard" not in participant_source
    assert "createContestDeletionCard" not in participant_source
    assert "createMatchFormCard" in management_source
    assert "createContestCompletionCard" in management_source
    assert "createContestDeletionCard" in management_source
    assert "createManagementNavigationCard" in contest_list_source
    assert "createSupermoderatorManagementCard" not in contest_list_source
    assert "createContestFormCard" not in contest_list_source


def test_management_always_uses_explicit_contest_selection_and_lazy_api() -> None:
    source = _source()
    management_source = _function_source(
        "renderManagementScreen",
        "openManagement",
    )
    list_source = _function_source(
        "createManagementContestListCard",
        "createManagementHeaderCard",
    )
    row_source = _function_source("createManagementContestRow")

    assert 'apiRequestForCurrentView("/api/tma/management/contests")' in source
    assert (
        "createManagementContestListCard(contests, bootstrap, managementData)"
        in management_source
    )
    assert "contests.length === 1" not in management_source
    assert "contests.length === 1" not in list_source
    assert "{ managementMode: true }" in row_source


def test_management_open_errors_stay_in_management_contour() -> None:
    open_source = _function_source("openContest")
    error_source = _function_source(
        "renderContestManagementError",
        "openContest",
    )

    assert "state.managementMode === true" in open_source
    assert "handleManagementRequestError(" in open_source
    assert "renderContestManagementError(bootstrap, errorMessage)" in open_source
    assert "renderContestDetailsError(bootstrap, errorMessage)" in open_source
    assert "void openManagement(bootstrap)" in error_source
    assert "renderContestScreen(bootstrap)" not in error_source


def test_low_level_api_request_has_no_navigation_side_effects() -> None:
    request_source = _function_source("apiRequest")
    management_error_source = _function_source(
        "handleManagementRequestError",
        "openManagement",
    )

    assert "error.status = response.status" in request_source
    assert "openManagement" not in request_source
    assert "currentViewMode" not in request_source
    assert "setTimeout" not in request_source
    assert "error?.status !== 403" in management_error_source
    assert "void openManagement(activeBootstrap" in management_error_source


def test_participant_prediction_failures_do_not_use_management_handler() -> None:
    match_prediction_source = _function_source("createMatchPredictionSection")
    champion_prediction_source = _function_source(
        "createChampionPredictionChoiceSection"
    )

    assert "handleManagementRequestError" not in match_prediction_source
    assert "handleManagementRequestError" not in champion_prediction_source

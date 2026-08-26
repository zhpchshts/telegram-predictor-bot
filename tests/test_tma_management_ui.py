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


def test_management_hub_header_shows_chat_context_and_returns_to_contests() -> None:
    header_source = _function_source("createManagementHeaderCard")
    shared_header_source = _function_source("createAdministrativeHeader")
    refresh_source = _function_source("openContestList")

    assert 'title: "Управление"' in header_source
    assert 'backLabel: "← К конкурсам"' in header_source
    assert "openContestList(bootstrap)" in header_source
    assert "Режим управления" not in header_source
    assert 'bootstrap.context?.chat?.title || "этого чата"' in shared_header_source
    assert "Чат «${chatTitle}»" in shared_header_source
    assert 'apiRequestForCurrentView("/api/tma/bootstrap")' in refresh_source
    assert "renderContestScreen(refreshedBootstrap, state)" in refresh_source
    assert "renderContestScreen(bootstrap" in refresh_source


def test_management_hub_contains_navigation_without_embedded_admin_bodies() -> None:
    hub_source = _function_source("renderManagementScreen")

    assert (
        "createManagementContestListCard(contests, bootstrap, managementData)"
        in hub_source
    )
    assert "createManagementAccessCard(bootstrap, capabilities)" in hub_source
    assert "createChatSettingsCard(bootstrap, managementData, state)" in hub_source
    assert "createContestFormCard" not in hub_source
    assert "createContestConfirmationCard" not in hub_source
    assert "createSupermoderatorManagementCard" not in hub_source
    assert "createAuditFiltersCard" not in hub_source
    assert "createAuditListCard" not in hub_source


def test_management_hub_actions_are_gated_by_server_capabilities() -> None:
    contests_source = _function_source("createManagementContestListCard")
    access_source = _function_source("createManagementAccessCard")

    assert contests_source.count("if (capabilities.can_create_contests === true)") == 2
    assert contests_source.count("openContestCreation(bootstrap, managementData)") == 2
    assert '"Создать"' in contests_source
    assert '"Создать конкурс"' in contests_source

    assert "if (capabilities.can_read_audit === true)" in access_source
    assert "openAuditHistory(bootstrap)" in access_source
    assert '"Журнал действий"' in access_source
    assert '"История административных изменений"' in access_source

    assert "if (capabilities.can_manage_roles === true)" in access_source
    assert "openSupermoderatorManagement(bootstrap)" in access_source
    assert '"Супермодераторы"' in access_source
    assert '"Управление дополнительными правами"' in access_source


def test_chat_button_text_can_be_changed_from_management() -> None:
    source = _function_source("createChatSettingsCard")

    assert '"/api/tma/management/chat-settings"' in source
    assert 'method: "PUT"' in source
    assert "app_button_text: appButtonText" in source
    assert "input.maxLength = 64" in source
    assert "Уже отправленные сообщения не изменятся." in source


def test_management_contests_are_grouped_and_completed_are_collapsed() -> None:
    list_source = _function_source("createManagementContestListCard")
    completed_source = _function_source("createCompletedManagementContestGroup")
    row_source = _function_source("createManagementContestRow")

    assert 'contest.status === "active"' in list_source
    assert 'contest.status === "completed"' in list_source
    assert '"Активные"' in list_source
    assert "if (completedContests.length > 0)" in list_source
    assert (
        "createCompletedManagementContestGroup(completedContests, bootstrap)"
        in list_source
    )

    assert 'createElement("details"' in completed_source
    assert 'text: "Завершённые"' in completed_source
    assert "disclosure.open" not in completed_source

    assert 'contest.status === "completed" ? "Завершён" : "Активен"' in row_source
    assert 'text: "›"' in row_source
    assert "effective_role" not in row_source
    assert "AUDIT_ROLE_LABELS" not in row_source


def test_management_empty_state_keeps_access_navigation_available() -> None:
    list_source = _function_source("createManagementContestListCard")
    hub_source = _function_source("renderManagementScreen")

    assert "if (contests.length === 0)" in list_source
    assert "Конкурсов в этом чате пока нет." in list_source
    assert "Создайте первый конкурс, чтобы добавить матчи " in list_source
    assert '+ "и принимать прогнозы."' in list_source
    assert '"Создать конкурс"' in list_source
    assert "createManagementAccessCard(bootstrap, capabilities)" in hub_source


def test_contest_creation_has_its_own_screen_and_back_navigation() -> None:
    creation_source = _function_source("renderContestCreationScreen")
    open_source = _function_source("openContestCreation")
    hub_source = _function_source("renderManagementScreen")

    assert 'title: "Создание конкурса"' in creation_source
    assert 'backLabel: "← К управлению"' in creation_source
    assert "void openManagement(bootstrap)" in creation_source
    assert "createContestConfirmationCard(bootstrap, creationState)" in creation_source
    assert "createContestFormCard(bootstrap, creationState)" in creation_source
    assert "renderContestCreationScreen(bootstrap" in open_source
    assert "createContestFormCard" not in hub_source
    assert "createContestConfirmationCard" not in hub_source


def test_empty_template_catalog_disables_new_creation() -> None:
    select_source = _function_source("fillContestTemplateSelect")
    contest_form_source = _function_source("createContestFormCard")
    confirmation_source = _function_source("createContestConfirmationCard")
    shared_form_source = _function_source("createSharedTournamentCreationCard")
    shared_open_source = _function_source("openSharedTournamentManagement")
    full_source = _source()

    assert 'emptyOption.textContent = "Шаблонов нет"' in select_source
    assert "emptyOption.disabled = true" in select_source
    assert "emptyOption.selected = true" in select_source
    assert "select.disabled = true" in select_source

    assert "state.managementData?.contest_templates" in contest_form_source
    assert "creatableTemplateKeys.has(tournament.template_key)" in contest_form_source
    assert "continueButton.disabled = !hasTemplates" in contest_form_source
    assert "if (!creatableTemplateKeys.has(templateInput.value))" in contest_form_source
    assert "draftTemplateKey: templateInput.value" in contest_form_source
    assert "template_key: state.draftTemplateKey" in confirmation_source
    assert confirmation_source.index("if (!selectedTemplate)") < (
        confirmation_source.index('state.draftTemplateKey === "the_international_2026"')
    )
    assert 'error?.code === "template_unavailable"' in confirmation_source
    assert "contest_templates: contestTemplates" in confirmation_source
    assert 'mode: "form"' in confirmation_source
    assert '|| "world_cup_2026"' not in full_source

    assert (
        "fillContestTemplateSelect(templateInput, contestTemplates)"
        in shared_form_source
    )
    assert "submitButton.disabled = !hasTemplates" in shared_form_source
    assert "if (!hasTemplates || !templateInput.value)" in shared_form_source
    assert 'error?.code === "template_unavailable"' in shared_form_source
    assert "openSharedTournamentManagement(bootstrap" in shared_form_source
    assert "result.contest_templates" in shared_open_source


def test_ti_series_controls_and_historical_rules_are_preserved() -> None:
    match_form_source = _function_source("createMatchFormCard")
    prediction_source = _function_source("createMatchPredictionSection")
    result_source = _function_source("createMatchResultSection")
    score_options_source = _function_source("getSeriesScoreOptions")

    assert 'bestOfInput.id = "match-best-of"' in match_form_source
    assert "const value of [3, 5]" in match_form_source
    assert "{ best_of: bestOf }" in match_form_source

    assert "winsRequired" in score_options_source
    assert "losingScore < winsRequired" in score_options_source
    assert "getSeriesScoreOptions(match.best_of)" in prediction_source
    assert (
        "predicted_advancing_team_id"
        not in prediction_source[
            prediction_source.index("if (isSeries)") : prediction_source.index(
                "const homeScore = getNonNegativeIntegerInputValue"
            )
        ]
    )

    assert "getSeriesScoreOptions(match.best_of)" in result_source
    assert 'text: isSeries ? "Результат серии" : "Результат матча"' in result_source
    assert "{ advancing_team_id: advancingTeamId }" in result_source
    assert "if (isSeries)" in result_source


def test_contest_rules_follow_the_selected_tournament_template() -> None:
    rules_source = _function_source("createContestRulesCard")
    details_source = _function_source("renderContestDetailsScreen")

    assert "templateKey" in rules_source
    assert 'templateKey === "the_international_2026"' in rules_source
    assert 'tiOverviewParts.push("Swiss 2/1")' in rules_source
    assert "tiOverviewParts.push(`чемпион +${championPoints}`)" in rules_source
    assert 'tiOverviewParts.push("Double Elimination 2/1")' in rules_source
    assert "Double Elimination: точный счёт серии — 2 балла" in rules_source
    assert "Верный победитель серии при другом счёте — 1 балл" in rules_source
    assert "Прогноз на чемпиона открыт до старта Double Elimination." in rules_source
    assert (
        "Серии Double Elimination играются до двух побед в Bo3 или до трёх побед в Bo5."
    ) in rules_source
    assert rules_source.index("Швейцарский этап:") < rules_source.index(
        "Прогноз на чемпиона открыт"
    )
    assert rules_source.index("Прогноз на чемпиона открыт") < rules_source.index(
        "Double Elimination: точный счёт серии"
    )
    assert "contest.template_key" in details_source


def test_successful_management_creation_opens_the_created_contest() -> None:
    confirmation_source = _function_source("createContestConfirmationCard")
    management_start = confirmation_source.index("if (state.managementMode === true)")
    participant_start = confirmation_source.index(
        "renderContestScreen(nextBootstrap",
        management_start,
    )
    management_branch = confirmation_source[management_start:participant_start]

    assert "activeBootstrap = nextBootstrap" in management_branch
    assert "openContest(nextBootstrap, result.contest.id" in management_branch
    assert "managementMode: true" in management_branch
    assert "openManagement(nextBootstrap" not in management_branch


def test_role_and_audit_tools_have_separate_screens_with_hub_back_links() -> None:
    role_source = _function_source("renderSupermoderatorManagementScreen")
    audit_header_source = _function_source("createAuditHeaderCard")
    audit_screen_source = _function_source("renderAuditScreen")

    assert 'title: "Супермодераторы"' in role_source
    assert 'backLabel: "← К управлению"' in role_source
    assert "void openManagement(bootstrap)" in role_source
    assert "createSupermoderatorManagementCard()" in role_source

    assert 'title: "Журнал действий"' in audit_header_source
    assert 'backLabel: "← К управлению"' in audit_header_source
    assert "void openManagement(bootstrap)" in audit_header_source
    assert "createAuditHeaderCard(bootstrap)" in audit_screen_source
    assert "createAuditFiltersCard(bootstrap, state)" in audit_screen_source
    assert "createAuditListCard(bootstrap, state)" in audit_screen_source


def test_participant_tabs_separate_match_and_tournament_predictions() -> None:
    source = _source()
    active_tab_source = _function_source("getActiveContestTab")
    details_source = _function_source("renderContestDetailsScreen")
    match_predictions_source = _function_source("createMatchPredictionListItems")
    matches_card_source = _function_source("createMatchesCard")
    tournament_predictions_source = _function_source(
        "createTournamentPredictionListItems"
    )

    matches_tab = '{ id: "matches", label: "Матчи" }'
    tournament_tab = '{ id: "tournament", label: "Турнир" }'
    leaderboard_tab = '{ id: "leaderboard", label: "Рейтинг" }'
    assert source.index(matches_tab) < source.index(tournament_tab)
    assert source.index(tournament_tab) < source.index(leaderboard_tab)
    assert ': "matches"' in active_tab_source

    assert 'activeTab === "tournament"' in details_source
    assert "createTournamentPredictionListItems(" in details_source
    assert "createMatchPredictionListItems(contest, matches)" in details_source
    assert 'title: "Матчи"' in details_source
    assert 'title: "Турнир"' in details_source

    assert "createMatchListItem(" in match_predictions_source
    assert "createChampionPredictionCard(contest)" in (tournament_predictions_source)
    assert "createSwissStagePredictionCard(contest)" in (tournament_predictions_source)
    assert "createMatchListItem" not in tournament_predictions_source
    assert "if (!hasListItems)" in matches_card_source


def test_participant_tournament_predictions_save_without_reloading_contest() -> None:
    details_source = _function_source("renderContestDetailsScreen")
    champion_source = _function_source("createChampionPredictionChoiceSection")
    swiss_source = _function_source("createSwissStagePredictionCard")

    assert "createTournamentPredictionListItems(contest)" in details_source
    assert "openContest" not in champion_source
    assert 'setFormMessage(message, "Прогноз сохранён.", "success")' in (
        champion_source
    )
    assert "openContest" not in swiss_source
    assert 'successMessage: "Прогноз сохранён."' in swiss_source


def test_leaderboard_keeps_name_and_optional_username_in_one_line() -> None:
    card_source = _function_source("createLeaderboardCard")
    row_source = _function_source("createLeaderboardRow")
    styles = (main.TMA_DIRECTORY / "styles.css").read_text(encoding="utf-8")

    assert "entry?.participant_username" in card_source
    assert "participantUsername !== null" in row_source
    assert "text: `@${participantUsername}`" in row_source
    assert 'className: "leaderboard-participant-identity"' in row_source
    assert 'className: "leaderboard-participant-username"' in row_source
    assert ".leaderboard-participant-identity" in styles
    assert "white-space: nowrap" in styles
    assert ".leaderboard-participant-username" in styles
    assert "text-overflow: ellipsis" in styles


def test_management_tabs_default_to_matches_and_keep_settings_separate() -> None:
    source = _source()
    active_tab_source = _function_source("getActiveContestManagementTab")
    management_source = _function_source("renderContestManagementScreen")
    settings_source = _function_source("createContestPredictionSettingsCard")
    publications_source = _function_source("createContestPublicationsCard")

    matches_tab = '{ id: "matches", label: "Матчи" }'
    settings_tab = '{ id: "settings", label: "Настройки" }'
    publications_tab = '{ id: "publications", label: "Публикации" }'
    management_tabs_start = source.index("const CONTEST_MANAGEMENT_TABS")
    assert source.index(matches_tab, management_tabs_start) < source.index(
        settings_tab,
        management_tabs_start,
    )
    assert source.index(settings_tab, management_tabs_start) < source.index(
        publications_tab,
        management_tabs_start,
    )
    assert ': "matches"' in active_tab_source

    assert "createContestManagementTabs(activeTab" in management_source
    assert 'activeTab === "settings"' in management_source
    assert 'activeTab === "publications"' in management_source
    assert "createTournamentTeamsAdministrationCard(" in management_source
    assert "createContestPredictionSettingsCard(" in management_source
    assert "createContestPublicationsCard(" in management_source
    assert "createContestCompletionCard(" in management_source
    assert "createContestDeletionCard(" in management_source
    assert management_source.index("createMatchFormCard(") < management_source.index(
        "createMatchesCard("
    )
    assert "Добавьте первый матч выше." in management_source

    assert "createMatchPredictionPublicationAdministrationCard" not in settings_source
    assert "createChampionAdministrationCard" in settings_source
    assert "createSwissStageAdministrationCard" in settings_source
    assert "createMatchPredictionPublicationAdministrationCard" in publications_source
    assert "createChampionAdministrationCard" not in publications_source
    assert "createSwissStageAdministrationCard" not in publications_source


def test_prediction_reminders_are_published_as_one_manual_message() -> None:
    reminder_source = _function_source("createPredictionReminderPublicationSection")
    publication_source = _function_source(
        "createMatchPredictionPublicationAdministrationCard"
    )

    assert '"Опубликовать предстоящие матчи"' in reminder_source
    assert "/prediction-reminders/publish`" in reminder_source
    assert '{ method: "POST" }' in reminder_source
    assert "отправит одно сообщение" in reminder_source
    assert "result?.published !== true" in reminder_source
    assert "createPredictionReminderPublicationSection(contest)" in (publication_source)


def test_intermediate_leaderboard_publication_is_an_idempotent_admin_action() -> None:
    leaderboard_source = _function_source(
        "createIntermediateLeaderboardPublicationSection"
    )
    publication_source = _function_source(
        "createMatchPredictionPublicationAdministrationCard"
    )

    assert '"Опубликовать промежуточный рейтинг"' in leaderboard_source
    assert "/leaderboard-publications`" in leaderboard_source
    assert 'method: "POST"' in leaderboard_source
    assert "[IDEMPOTENCY_KEY_HEADER]: idempotencyKey" in leaderboard_source
    assert 'createIdempotencyKey("leaderboard-publication")' in leaderboard_source
    assert "entry?.calculated_predictions_count" in leaderboard_source
    assert "result?.queued !== true" in leaderboard_source
    assert "createIntermediateLeaderboardPublicationSection(contest)" in (
        publication_source
    )


def test_participant_contour_does_not_render_administrative_forms() -> None:
    list_source = _function_source("renderContestScreen")
    details_source = _function_source("renderContestDetailsScreen")
    predictions_source = _function_source("createMatchPredictionListItems")

    assert "if (canManageContests(bootstrap))" in list_source
    assert "createManagementNavigationCard(bootstrap)" in list_source
    assert "createContestFormCard" not in list_source
    assert "createSupermoderatorManagementCard" not in list_source
    assert "createAuditFiltersCard" not in list_source

    assert "createMatchPredictionListItems(" in details_source
    assert "createTournamentPredictionListItems(" in details_source
    assert "createLeaderboardCard" in details_source
    assert "createMatchListItem(" in predictions_source
    assert "showPredictions: true" in predictions_source
    assert "showResults: false" in predictions_source
    assert "createMatchFormCard" not in details_source
    assert "createContestCompletionCard" not in details_source
    assert "createContestDeletionCard" not in details_source


def test_match_form_prefills_local_start_time_and_focuses_on_manual_open() -> None:
    local_format_source = _function_source("formatLocalDateTime")
    default_time_source = _function_source("getDefaultDateTimeLocal")
    form_source = _function_source("createMatchFormCard")

    assert "date.getFullYear()" in local_format_source
    assert "date.getMonth() + 1" in local_format_source
    assert "date.getDate()" in local_format_source
    assert "date.getHours()" in local_format_source
    assert "date.getMinutes()" in local_format_source
    assert "toISOString()" not in local_format_source

    assert "const startsAt = new Date(now.getTime())" in default_time_source
    assert "startsAt.setSeconds(0, 0)" in default_time_source
    assert "startsAt.setMinutes(startsAt.getMinutes() + 3)" in default_time_source
    assert "return formatLocalDateTime(startsAt)" in default_time_source

    assert "draft.startsAtLocal || getDefaultDateTimeLocal()" in form_source
    assert "const wasInitiallyOpen = disclosure.open" in form_source
    assert 'disclosure.addEventListener("toggle"' in form_source
    assert "disclosure.open" in form_source
    assert "!wasInitiallyOpen" in form_source
    assert "!state.matchDraft" in form_source
    assert "homeTeamInput.focus()" in form_source


def test_management_uses_tournament_team_list_and_ids_for_matches() -> None:
    teams_source = _function_source("createTournamentTeamsAdministrationCard")
    match_source = _function_source("createMatchFormCard")
    management_source = _function_source("renderContestManagementScreen")

    assert 'text: "Команды турнира"' in teams_source
    assert "team_names: teamNames" in teams_source
    assert "/teams`" in teams_source
    assert "tournamentTeams.is_locked" in teams_source
    assert "Список команд нельзя изменить после создания матчей" in teams_source
    assert "createTournamentTeamsAdministrationCard" in management_source

    assert "getTournamentTeams(contest).teams" in match_source
    assert "createChampionTeamSelect(tournamentTeams" in match_source
    assert "home_team_id: homeTeamId" in match_source
    assert "away_team_id: awayTeamId" in match_source
    assert "home_team_name: homeTeamName" not in match_source
    assert "away_team_name: awayTeamName" not in match_source


def test_empty_champion_deadline_uses_general_local_default() -> None:
    settings_source = _function_source("createChampionPredictionSettingsDisclosure")

    assert "formatDateTimeLocalValue(championPrediction.deadline_at)" in settings_source
    assert "|| getDefaultDateTimeLocal()" in settings_source
    assert settings_source.index(
        "formatDateTimeLocalValue(championPrediction.deadline_at)"
    ) < settings_source.index("|| getDefaultDateTimeLocal()")


def test_champion_settings_action_matches_the_persisted_enabled_state() -> None:
    settings_source = _function_source("createChampionPredictionSettingsDisclosure")

    assert 'submitButton.textContent = "Сохранить настройки"' in settings_source
    assert "isEnabled !== championPrediction.is_enabled" in settings_source
    assert "prediction.is_enabled" not in settings_source
    assert '? "Включить прогноз"' in settings_source
    assert ': "Выключить прогноз"' in settings_source


def test_existing_match_edit_keeps_match_time_and_current_edit_value() -> None:
    editing_source = _function_source("createMatchStartEditingSection")

    assert "isCurrentMatch && state.matchStartEditValue" in editing_source
    assert "? state.matchStartEditValue" in editing_source
    assert ": formatDateTimeLocalValue(match.starts_at_utc)" in editing_source
    assert "getDefaultDateTimeLocal" not in editing_source


def test_scheduled_match_card_shows_static_time_until_start() -> None:
    plural_source = _function_source("getRussianPlural")
    starts_in_source = _function_source("formatMatchStartsIn")
    item_source = _function_source("createMatchListItem")

    assert "lastTwoDigits >= 11 && lastTwoDigits <= 14" in plural_source
    assert "lastDigit === 1" in plural_source
    assert "lastDigit >= 2 && lastDigit <= 4" in plural_source

    assert 'match.status !== "scheduled"' in starts_in_source
    assert "remainingMilliseconds <= 0" in starts_in_source
    assert "Math.floor(remainingMilliseconds / 60_000)" in starts_in_source
    assert "Math.floor(totalMinutes / (24 * 60))" in starts_in_source
    assert "Math.floor((totalMinutes % (24 * 60)) / 60)" in starts_in_source
    assert "const minutes = totalMinutes % 60" in starts_in_source
    assert '"Начнётся через "' in starts_in_source
    assert '"день", "дня", "дней"' in starts_in_source
    assert '"час", "часа", "часов"' in starts_in_source
    assert '"минуту", "минуты", "минут"' in starts_in_source

    assert "const startsInText = formatMatchStartsIn(match)" in item_source
    assert "if (startsInText)" in item_source
    assert 'className: "match-meta match-starts-in"' in item_source
    assert "setInterval" not in item_source
    assert "setTimeout" not in item_source


def test_shared_tournament_ui_owns_all_common_deadlines_and_results() -> None:
    screen_source = _function_source("renderSharedTournamentScreen")
    champion_source = _function_source("createSharedChampionCard")
    swiss_source = _function_source("createSharedSwissStageCard")
    lifecycle_source = _function_source("createSharedTournamentLifecycleCard")
    list_source = _function_source("createSharedTournamentListCard")

    assert "createSharedTournamentLifecycleCard" in screen_source
    assert "createSharedTournamentTeamsCard" in screen_source
    assert "createSharedChampionCard" in screen_source
    assert "createSharedSwissStageCard" in screen_source
    assert "createSharedMatchCreationCard" in screen_source
    assert "tournament.is_archived !== true" in screen_source
    assert "/champion-prediction/settings`" in champion_source
    assert "/champion`" in champion_source
    assert "во всех чатах" in champion_source
    assert "/swiss-stage/settings`" in swiss_source
    assert "/swiss-stage/result`" in swiss_source
    assert "во всех чатах" in swiss_source
    assert "/archive`" in lifecycle_source
    assert "/restore`" in lifecycle_source
    assert "конкурсы нужно завершить отдельно" in lifecycle_source
    assert "Вернуть в активные" in lifecycle_source
    assert "tournament.is_archived === true" in champion_source
    assert "tournament.is_archived === true" in swiss_source
    assert '["Активные"' in list_source
    assert '["Завершённые"' in list_source


def test_linked_contest_hides_local_tournament_admin_forms() -> None:
    management_source = _function_source("renderContestManagementScreen")

    assert (
        "const isSharedTournament = contest.shared_tournament !== null"
        in management_source
    )
    assert "if (!isSharedTournament)" in management_source
    assert (
        "Команды, матчи, дедлайны и результаты редактируются только"
        in management_source
    )
    assert "isSharedTournament ? null" in management_source

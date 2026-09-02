from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source() -> str:
    return (ROOT / "tma" / "app.js").read_text(encoding="utf-8")


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


def test_tma_contains_mobile_swiss_stage_prediction_controls() -> None:
    source = _source()
    selector_source = _function_source("createSwissStageTeamSelector")
    card_source = _function_source("createSwissStagePredictionCard")

    assert "Прогноз на швейцарскую систему" in source
    assert 'directProgress: "Пройдут напрямую"' in source
    assert 'eliminationProgress: "Через стыковой раунд"' in source
    assert "элиминейшн" not in source.lower()
    assert "createSwissStageTeamSelector" in source
    assert "data-category='direct'" in selector_source
    assert "data-category='playoff'" in selector_source
    assert "data-category='elimination'" in selector_source
    assert "autosave: true" in card_source
    assert "submitLabel: prediction.prediction" not in card_source
    assert (
        "drag"
        not in source[
            source.index("function createSwissStageTeamSelector") : source.index(
                "function createSwissStageReadonlySelection"
            )
        ]
    )


def test_participant_stage_choices_autosave_but_results_remain_manual() -> None:
    selector_source = _function_source("createSwissStageTeamSelector")
    participant_source = _function_source("createSwissStagePredictionCard")
    administration_source = _function_source("createSwissStageAdministrationCard")

    assert "autosave = false" in selector_source
    assert "const isAutosave = autosave && !resultMode" in selector_source
    assert "queueSwissStagePredictionSave(" in selector_source
    assert "function scheduleSave()" in selector_source
    assert "function savePrediction()" in selector_source
    assert "let isSaving = false" in selector_source
    assert "lastSavedFingerprint" in selector_source
    assert "PREDICTION_FLUSH_EVENT" in selector_source
    assert "onClosed = () => {}" in selector_source
    assert "onClosed();" in selector_source
    assert "autosave: true" in participant_source
    assert "contestId: contest.id" in participant_source
    assert "syncSwissStageStatus(status, prediction)" in participant_source
    assert "resultMode: true" in administration_source
    assert "autosave: true" not in administration_source
    assert "window.confirm(" in selector_source


def test_stage_autosave_preserves_partial_and_exact_selection_modes() -> None:
    selector_source = _function_source("createSwissStageTeamSelector")

    assert 'prediction.selection_mode === "up_to_limits"' in selector_source
    assert "return allowPartial || isSelectionComplete()" in selector_source
    assert "function isDirty(" in selector_source
    assert "function getSavedSelectionFingerprint(" in selector_source
    assert "savedFingerprint !== fingerprint" in selector_source
    assert "fingerprint !== initialFingerprint" in selector_source
    assert ".sort((left, right) => left - right)" in selector_source
    assert "Черновик сохранён." in selector_source
    assert 'setSaveStatus("", "draft")' in selector_source
    assert "Заполните прогноз полностью — изменения пока не сохранены." in (
        selector_source
    )


def test_tma_uses_separate_swiss_stage_api_routes() -> None:
    source = _source()

    assert "/swiss-stage-prediction/settings" in source
    assert "/swiss-stage-prediction`" in source
    assert "/swiss-stage-result" in source
    assert "createSwissStageAdministrationCard" in source
    assert "Исправить итоги? Рейтинг будет пересчитан сразу." in source


def test_general_stage_editability_uses_server_state_and_shows_partial_progress() -> (
    None
):
    card_source = _function_source("createSwissStagePredictionCard")
    selector_source = _function_source("createSwissStageTeamSelector")

    assert "!prediction.is_open" in card_source
    assert "Date.now" not in card_source
    assert "deadline <= Date.now()" in selector_source
    assert "schedulePredictionDeadlineSync" in selector_source
    assert "form.dataset.predictionDeadline" in selector_source
    assert "PREDICTION_DEADLINE_SYNC_EVENT" in selector_source
    assert "prediction.is_open = false" in selector_source
    assert "if (resultMode)" in selector_source
    assert "!resultMode && error?.status === 409" in selector_source
    assert "`В 1/8: ${directIds.size}/${prediction.direct_qualifier_count}`" in (
        selector_source
    )
    assert "`На вылет: ${eliminationIds.size}/`" in selector_source
    assert "currentPlayoffCount" not in selector_source
    assert "playoffProgress" not in selector_source
    assert 'prediction.selection_mode === "up_to_limits"' in selector_source
    assert "!allowPartial" in selector_source


def test_tma_prediction_count_separates_calculated_and_pending() -> None:
    card_source = _function_source("createLeaderboardCard")
    row_source = _function_source("createLeaderboardRow")

    assert "entry?.calculated_predictions_count" in card_source
    assert "entry?.match_predictions_count" in card_source
    assert "entry?.champion_prediction_count" in card_source
    assert "entry?.swiss_stage_prediction_count" in card_source
    assert "savedPredictionsCount - calculatedPredictionsCount" in card_source
    assert "longTermPredictionSlotsCount" not in card_source
    assert "`${calculatedPredictionsCount + pendingPredictionsCount}`" in row_source


def test_tma_uses_server_awards_in_personal_card_and_leaderboard_history() -> None:
    breakdown_source = _function_source("createSwissStageAwardsBreakdown")
    personal_card_source = _function_source("createSwissStagePredictionCard")
    leaderboard_history_source = _function_source(
        "createLeaderboardSwissStagePredictionHistory"
    )
    score_summary_source = _function_source("createSwissStageScoreSummary")

    assert "award?.predicted_category" in breakdown_source
    assert "award?.actual_category" in breakdown_source
    assert "award?.points" in breakdown_source
    assert "Прогноз:" in breakdown_source
    assert "Факт:" in breakdown_source
    assert "formatSwissStageAwardPoints(award?.points)" in breakdown_source

    assert "prediction.actual_result" in personal_card_source
    assert "Array.isArray(prediction.awards)" in personal_card_source
    assert "prediction.awards," in personal_card_source
    assert "contest.template_key" in personal_card_source
    assert "prediction?.actual_result" in leaderboard_history_source
    assert "Array.isArray(prediction.awards)" in leaderboard_history_source
    assert "createSwissStageAwardsBreakdown(prediction.awards, templateKey)" in (
        leaderboard_history_source
    )
    assert "prediction.score_breakdown" in personal_card_source
    assert "prediction.score_breakdown" in leaderboard_history_source
    assert "scoreBreakdown.correct_direct_count" in score_summary_source
    assert "scoreBreakdown.direct_points" in score_summary_source
    assert "scoreBreakdown.correct_elimination_count" in score_summary_source
    assert "scoreBreakdown.elimination_points" in score_summary_source
    assert "scoreBreakdown.total_points" in score_summary_source


def test_tma_formats_swiss_stage_audit_result_with_team_names() -> None:
    formatter_source = _function_source("formatAuditSwissStageResult")
    state_value_source = _function_source("formatAuditStateValue")
    state_label_source = _function_source("getAuditStateFieldLabel")
    summary_source = _function_source("buildAuditSummaryLines")

    assert "value?.direct_team_ids" in formatter_source
    assert "value?.playoff_team_ids" not in formatter_source
    assert "value?.elimination_team_ids" in formatter_source
    assert "getAuditTeamName(event, teamId)" in formatter_source
    assert "getSwissStageCopy(event?.contest?.template_key)" in formatter_source
    assert "copy.directResult" in formatter_source
    assert "copy.playoffResult" not in formatter_source
    assert "copy.eliminationResult" in formatter_source
    assert 'key === "actual_result"' in state_value_source
    assert "formatAuditSwissStageResult(value, event)" in state_value_source
    assert 'key === "elimination_qualifier_count"' in state_label_source
    assert 'key === "direct_qualifier_count"' in state_label_source
    assert 'event?.contest?.template_key === "champions_league_2026_27"' in (
        state_label_source
    )
    assert 'return "Вылетят после общего этапа"' in state_label_source
    assert 'return "Напрямую в 1/8"' in state_label_source
    assert 'case "swiss_stage_result_set"' in summary_source
    assert 'case "swiss_stage_result_changed"' in summary_source
    assert "swissStageCopy.stageName.toLowerCase()" in summary_source
    assert "swissStageCopy.stageGenitive" in summary_source
    assert "formatAuditSwissStageResult(after.actual_result, event)" in (summary_source)


def test_empty_swiss_stage_deadline_uses_general_local_default() -> None:
    settings_source = _function_source("createSwissStageSettingsForm")

    assert "formatDateTimeLocalValue(prediction.deadline_at)" in settings_source
    assert "|| getDefaultDateTimeLocal()" in settings_source
    assert settings_source.index(
        "formatDateTimeLocalValue(prediction.deadline_at)"
    ) < settings_source.index("|| getDefaultDateTimeLocal()")


def test_swiss_stage_ui_defaults_to_three_plus_five() -> None:
    prediction_source = _function_source("getSwissStagePrediction")

    assert "direct_qualifier_count: 3" in prediction_source
    assert "elimination_qualifier_count: 5" in prediction_source
    assert "? prediction.direct_qualifier_count\n      : 3" in prediction_source
    assert "? prediction.elimination_qualifier_count\n      : 5" in (prediction_source)


def test_swiss_stage_settings_match_champion_settings_copy_and_state() -> None:
    champion_source = _function_source("createChampionPredictionSettingsDisclosure")
    champion_card_source = _function_source("createChampionAdministrationCard")
    swiss_source = _function_source("createSwissStageSettingsForm")
    swiss_card_source = _function_source("createSwissStageAdministrationCard")
    swiss_status_source = _function_source("syncSwissStageStatus")

    for settings_source in (champion_source, swiss_source):
        assert 'text: "Настройки прогноза"' in settings_source
        assert 'text: "Включить прогноз"' in settings_source
        assert "summaryContent.append(title, overview)" in settings_source
        assert "disclosure.append(summary, description, form)" in settings_source

    assert 'text: "Прогноз закрывается"' in champion_source
    assert '"Начало первого матча общего этапа"' in swiss_source
    assert ': "Прогноз закрывается"' in swiss_source

    assert 'text: "Прогноз на чемпиона"' in champion_card_source
    assert "getSwissStageCopy(contest.template_key)" in swiss_card_source
    assert "text: copy.predictionTitle" in swiss_card_source
    assert 'status.textContent = "Не настроен"' in swiss_status_source
    assert "deadlineInput.disabled = !deadlineEditable" in swiss_source
    assert "deadlineInput.required = deadlineEditable" in swiss_source
    assert "isEnabled !== prediction.is_enabled" in swiss_source
    assert (
        'deadlineField.classList.toggle("is-disabled", !deadlineEditable)'
        in swiss_source
    )
    assert 'enabledInput.addEventListener("change", syncEnabledState)' in (swiss_source)
    assert "enabledInput.disabled = prediction.settings_locked" in swiss_source
    assert "prediction.settings_locked || hasFixedChampionsLeagueLimits" in (
        swiss_source
    )


def test_swiss_stage_settings_use_tournament_teams_without_separate_input() -> None:
    settings_source = _function_source("createSwissStageSettingsForm")

    assert "Используются команды турнира:" in settings_source
    assert "prediction.candidates.length" in settings_source
    assert "team_names" not in settings_source
    assert "teamsInput" not in settings_source


def test_tma_locked_settings_text_mentions_prediction_and_result() -> None:
    settings_source = _function_source("createSwissStageSettingsForm")

    assert (
        "Настройки зафиксированы после сохранения первого пользовательского "
        in settings_source
    )
    assert "прогноза или фактического результата, а дедлайн уже наступил." in (
        settings_source
    )
    assert "До наступления текущего дедлайна его можно изменить." in settings_source


def test_champions_league_uses_league_phase_copy_and_fixed_limits() -> None:
    copy_source = _function_source("getSwissStageCopy")
    scoring_source = _function_source("getSwissStageScoringRule")
    meta_source = _function_source("createSwissStageMeta")
    category_source = _function_source("getSwissStageCategoryLabel")
    selector_source = _function_source("createSwissStageTeamSelector")
    administration_source = _function_source("createSwissStageAdministrationCard")
    settings_source = _function_source("createSwissStageSettingsForm")
    shared_source = _function_source("createSharedSwissStageCard")
    readonly_source = _function_source("createSwissStageReadonlySelection")
    history_source = _function_source("createLeaderboardSwissStagePredictionHistory")

    assert 'templateKey === "champions_league_2026_27"' in copy_source
    assert 'stageName: "Общий этап"' in copy_source
    assert 'stageGenitive: "общего этапа"' in copy_source
    assert 'predictionTitle: "Прогноз на общий этап"' in copy_source
    assert 'directChoice: "Напрямую"' in copy_source
    assert 'playoffChoice: "Стыки"' in copy_source
    assert 'eliminationChoice: "Вылет"' in copy_source
    assert 'eliminationProgress: "Вылетят после общего этапа"' in copy_source
    assert 'directResult: "Вышли напрямую в 1/8"' in copy_source
    assert 'playoffResult: "Попали в стыки"' in copy_source
    assert 'eliminationResult: "Вылетели после общего этапа"' in copy_source
    assert 'predictedElimination: "вылетит после общего этапа"' in copy_source
    assert 'actualElimination: "вылетела после общего этапа"' in copy_source
    assert 'predictedPlayoff: "попадёт в стыки"' in copy_source
    assert 'actualPlayoff: "попала в стыки"' in copy_source
    assert 'actualUnselected: "попала в стыковой раунд"' in copy_source
    assert 'actualUnselected: "не прошла"' in copy_source
    assert "copy.actualUnselected" in category_source
    assert "resultMode = false" in selector_source
    assert "resultMode ? copy.directResult : copy.directChoice" in selector_source
    assert "resultMode" in selector_source
    assert 'prediction.selection_mode === "up_to_limits"' in selector_source
    assert "!resultMode" in selector_source
    assert 'setCategory(team.id, "playoff")' in selector_source
    assert "playoffIds" not in selector_source
    assert "removeFromEveryCategory(teamId)" in selector_source
    assert "function isPlayoffTeam(teamId)" in selector_source
    assert "!directIds.has(teamId)" in selector_source
    assert "!eliminationIds.has(teamId)" in selector_source
    assert "const playoffButton" in selector_source
    assert '"swiss-stage-team-actions--three"' in selector_source
    assert "categoryIds.size < categoryLimit" in selector_source
    assert "categoryIds.delete(teamId)" in selector_source
    assert "directIds.size >= prediction.direct_qualifier_count" in selector_source
    assert (
        "eliminationIds.size >= prediction.elimination_qualifier_count"
        in selector_source
    )
    assert "!allowPartial" in selector_source
    assert "allowPartial && !isSelectionComplete()" in selector_source
    assert "text: copy.selectionHint" in selector_source
    assert "copy.eliminationResult" in selector_source
    assert "playoff_team_ids" not in selector_source
    assert "resultMode: true" in administration_source
    assert "Баллы: +2 за прямой выход, +1 за вылет, за стыки — 0" in copy_source
    assert "`Дедлайн: ${formatMatchStartsAt(" in meta_source
    assert "Выберите ${prediction.direct_qualifier_count} команд" in meta_source
    assert "Остальные 16 останутся в стыках." in meta_source
    assert "В форме доступны все три варианта." not in meta_source
    assert "Прогноз закрывается с началом первого матча" not in meta_source
    assert "При подсчёте баллов учитываются только прямой выход и вылет" in (
        scoring_source
    )
    assert "За выбор «Стыки» баллы не начисляются" in scoring_source
    assert "Максимум — 28 баллов" in copy_source
    assert 'contest.template_key === "champions_league_2026_27"' in (settings_source)
    assert "Команды общего этапа:" in settings_source
    assert "варианты «Напрямую», «Стыки» и «Вылет»" in settings_source
    assert "частичный черновик сохраняется" in settings_source
    assert "Начало первого матча общего этапа" in settings_source
    assert "getChampionsLeaguePlayoffTeams" not in readonly_source
    assert "copy.playoffResult" not in readonly_source
    assert "copy.playoffChoice" not in history_source
    assert "getChampionsLeaguePlayoffTeams" not in history_source
    assert "hasFixedChampionsLeagueLimits" in shared_source
    assert "directCount.disabled = hasFixedChampionsLeagueLimits" in shared_source
    assert "eliminationCount.disabled = hasFixedChampionsLeagueLimits" in shared_source
    assert "категории «${copy.directChoice}», «${copy.playoffChoice}»" in shared_source
    assert "Баллы начисляются только за верный прямой выход" in shared_source
    assert "максимум — 28 баллов" in shared_source
    assert "settings.playoff_team_ids" not in shared_source
    assert "createLabeledField(copy.playoffResult" not in shared_source
    assert "selectedPlayoffCount" not in shared_source
    assert "selectedDirectCount !== requiredDirectCount" in shared_source
    assert "selectedEliminationCount !== requiredEliminationCount" in shared_source

    styles = (ROOT / "tma" / "styles.css").read_text(encoding="utf-8")
    assert ".swiss-stage-team-actions--three" in styles
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in styles

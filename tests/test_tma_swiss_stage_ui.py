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

    assert "Итоги швейцарского этапа" in source
    assert "Пройдут напрямую:" in source
    assert "Через элиминейшн-раунд:" in source
    assert "createSwissStageTeamSelector" in source
    assert "data-category='direct'" in source
    assert "data-category='elimination'" in source
    assert "Сохранить прогноз" in source
    assert (
        "drag"
        not in source[
            source.index("function createSwissStageTeamSelector") : source.index(
                "function createSwissStageReadonlySelection"
            )
        ]
    )


def test_tma_uses_separate_swiss_stage_api_routes() -> None:
    source = _source()

    assert "/swiss-stage-prediction/settings" in source
    assert "/swiss-stage-prediction`" in source
    assert "/swiss-stage-result" in source
    assert "createSwissStageAdministrationCard" in source
    assert "Исправить итоги? Рейтинг будет пересчитан сразу." in source


def test_tma_completeness_combines_enabled_long_term_predictions() -> None:
    source = _source()

    assert "longTermPredictionSlotsCount" in source
    assert "entry?.swiss_stage_prediction_count" in source
    assert "entry?.swiss_stage_prediction_history" in source


def test_tma_uses_server_awards_in_personal_card_and_leaderboard_history() -> None:
    breakdown_source = _function_source("createSwissStageAwardsBreakdown")
    personal_card_source = _function_source("createSwissStagePredictionCard")
    leaderboard_history_source = _function_source(
        "createLeaderboardSwissStagePredictionHistory"
    )

    assert "award?.predicted_category" in breakdown_source
    assert "award?.actual_category" in breakdown_source
    assert "award?.points" in breakdown_source
    assert "Прогноз:" in breakdown_source
    assert "Факт:" in breakdown_source
    assert "formatSwissStageAwardPoints(award?.points)" in breakdown_source

    assert "prediction.actual_result" in personal_card_source
    assert "Array.isArray(prediction.awards)" in personal_card_source
    assert "createSwissStageAwardsBreakdown(prediction.awards)" in (
        personal_card_source
    )
    assert "prediction?.actual_result" in leaderboard_history_source
    assert "Array.isArray(prediction.awards)" in leaderboard_history_source
    assert "createSwissStageAwardsBreakdown(prediction.awards)" in (
        leaderboard_history_source
    )


def test_tma_formats_swiss_stage_audit_result_with_team_names() -> None:
    formatter_source = _function_source("formatAuditSwissStageResult")
    state_value_source = _function_source("formatAuditStateValue")
    summary_source = _function_source("buildAuditSummaryLines")

    assert "value?.direct_team_ids" in formatter_source
    assert "value?.elimination_team_ids" in formatter_source
    assert "getAuditTeamName(event, teamId)" in formatter_source
    assert "Прошли напрямую:" in formatter_source
    assert "через элиминейшн-раунд:" in formatter_source
    assert 'key === "actual_result"' in state_value_source
    assert "formatAuditSwissStageResult(value, event)" in state_value_source
    assert 'case "swiss_stage_result_set"' in summary_source
    assert 'case "swiss_stage_result_changed"' in summary_source
    assert "formatAuditSwissStageResult(after.actual_result, event)" in (summary_source)


def test_tma_locked_settings_text_mentions_prediction_and_result() -> None:
    settings_source = _function_source("createSwissStageSettingsForm")

    assert (
        "Настройки зафиксированы после сохранения первого пользовательского "
        in settings_source
    )
    assert "прогноза или фактического результата." in settings_source

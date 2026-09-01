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


def test_two_legged_pair_is_a_separate_autosaved_participant_prediction() -> None:
    list_source = _function_source("createMatchPredictionListItems")
    prediction_source = _function_source("createTwoLeggedTiePredictionListItem")
    queue_source = _function_source("queueTwoLeggedTiePredictionSave")

    assert "getTwoLeggedTies(contest)" in list_source
    assert 'kind: "two-legged-tie"' in list_source
    assert "createTwoLeggedTiePredictionListItem(contest, item.tie)" in list_source

    assert 'text: "Кто пройдёт дальше?"' in prediction_source
    assert "getTwoLeggedTiePredictionDeadline(contest, tie)" in prediction_source
    assert "isTwoLeggedTiePredictionOpen(contest, tie)" in prediction_source
    assert "predicted_advancing_team_id: advancingTeamId" in prediction_source
    assert "predicted_home_score" not in prediction_source
    assert "queueTwoLeggedTiePredictionSave(" in prediction_source
    assert "PREDICTION_FLUSH_EVENT" in prediction_source
    assert "PREDICTION_DEADLINE_SYNC_EVENT" in prediction_source
    assert "form.dataset.predictionDeadline = deadlineAt" in prediction_source
    assert "начало первого матча" in prediction_source

    assert 'method: "PUT"' not in queue_source
    assert (
        "`/api/tma/contests/${contestId}/two-legged-ties/${tieId}/prediction`"
        in queue_source
    )


def test_two_legged_matches_save_only_the_score_after_ninety_minutes() -> None:
    prediction_source = _function_source("createMatchPredictionSection")
    result_source = _function_source("createMatchResultSection")

    for function_source in (prediction_source, result_source):
        assert "const isTwoLegged = isTwoLeggedMatch(match)" in function_source
        assert 'isTwoLegged ? "Счёт после 90 минут"' in function_source
        assert "isSeries || isTwoLegged" in function_source

    assert "if (isTwoLegged)" in prediction_source
    score_only_prediction_branch = prediction_source[
        prediction_source.index("if (isTwoLegged)") : prediction_source.index(
            "const advancingTeamId = advancingTeamField.getAdvancingTeamId()"
        )
    ]
    assert "predicted_home_score: homeScore" in score_only_prediction_branch
    assert "predicted_away_score: awayScore" in score_only_prediction_branch
    assert "predicted_advancing_team_id" not in score_only_prediction_branch

    assert "if (!isTwoLegged)" in result_source
    assert "advancingTeamId = advancingTeamField.getAdvancingTeamId()" in result_source
    assert "{ advancing_team_id: advancingTeamId }" in result_source
    assert "дополнительное время" in result_source.lower()


def test_two_legged_pair_creation_uses_the_exact_local_and_shared_contract() -> None:
    form_source = _function_source("createTwoLeggedTieFormCard")
    local_source = _function_source("renderContestManagementScreen")
    shared_source = _function_source("renderSharedTournamentScreen")

    for field_name in (
        "first_team_id",
        "second_team_id",
        "first_leg_starts_at_utc",
        "second_leg_starts_at_utc",
    ):
        assert field_name in form_source
    assert 'method: "POST"' in form_source
    assert "[IDEMPOTENCY_KEY_HEADER]: idempotencyKey" in form_source
    assert "secondLegStartsAt.getTime() <= firstLegStartsAt.getTime()" in form_source
    assert "response?.two_legged_tie" in form_source

    assert "`/api/tma/contests/${contest.id}/two-legged-ties`" in local_source
    assert (
        "`/api/tma/shared-tournaments/${tournament.id}/two-legged-ties`"
        in shared_source
    )

    local_ti_guard = 'if (contest.template_key !== "the_international_2026") {'
    shared_ti_guard = 'if (tournament.template_key !== "the_international_2026") {'
    assert local_ti_guard in local_source
    assert (
        local_source.index("createMatchFormCard")
        < local_source.index(local_ti_guard)
        < local_source.index("createTwoLeggedTieFormCard")
    )
    assert shared_ti_guard in shared_source
    assert (
        shared_source.index("createSharedMatchCreationCard")
        < shared_source.index(shared_ti_guard)
        < shared_source.index("createTwoLeggedTieFormCard")
    )


def test_pair_admin_result_and_delete_are_atomic_for_local_and_shared_ties() -> None:
    administration_source = _function_source("createTwoLeggedTieAdministrationItem")
    match_item_source = _function_source("createMatchListItem")
    shared_match_source = _function_source("createSharedMatchAdministrationCard")
    local_source = _function_source("renderContestManagementScreen")
    shared_source = _function_source("renderSharedTournamentScreen")

    for field_name in (
        "advancing_team_id",
        "second_leg_extra_time_home_score",
        "second_leg_extra_time_away_score",
        "second_leg_home_penalty_score",
        "second_leg_away_penalty_score",
    ):
        assert field_name in administration_source
    assert "Голы в дополнительное время ответного матча" in administration_source
    assert "Счёт серии пенальти" in administration_source
    assert "expected_version: tie.version" in administration_source
    assert "`?expected_version=${tie.version}`" in administration_source
    assert "Будут удалены обе игры" in administration_source

    assert "&& !isTwoLeggedMatch(match)" in match_item_source
    assert "if (isTwoLegged) {\n    return card;" in shared_match_source

    for source, prefix in (
        (local_source, "/api/tma/contests/${contest.id}"),
        (shared_source, "/api/tma/shared-tournaments/${tournament.id}"),
    ):
        assert f"`{prefix}/two-legged-ties/${{tieId}}/result`" in source
        assert f"`{prefix}/two-legged-ties/${{tieId}}`" in source


def test_pair_states_are_resilient_and_explained_in_rules_and_history() -> None:
    helpers_source = _function_source("getTwoLeggedTies")
    result_helper_source = _function_source("getTwoLeggedTieResult")
    open_helper_source = _function_source("isTwoLeggedTiePredictionOpen")
    rules_source = _function_source("createContestRulesCard")
    leaderboard_source = _function_source("createLeaderboardCard")
    history_source = _function_source("createLeaderboardTwoLeggedTiePredictionHistory")
    styles = (main.TMA_DIRECTORY / "styles.css").read_text(encoding="utf-8")

    assert "Array.isArray(container?.two_legged_ties)" in helpers_source
    assert "tie?.result" in result_helper_source
    assert "tie?.advancing_team_id" in result_helper_source
    assert "aggregate_first_team_score" in result_helper_source
    assert "tie?.is_prediction_open === false" in open_helper_source
    assert "getTwoLeggedTiePredictionDeadline(container, tie)" in open_helper_source

    assert "hasTwoLeggedTies" in rules_source
    assert "Максимум за пару — 7 баллов" in rules_source
    assert "Прогноз на проход закрывается с началом первого матча" in rules_source
    assert "Прогноз счёта ответного матча остаётся открыт" in rules_source

    assert "entry?.two_legged_tie_predictions_count" in leaderboard_source
    assert "twoLeggedTiePredictionHistory" in leaderboard_source
    assert 'createLeaderboardHistoryRow(\n      "Прогноз"' in history_source
    assert "awarded_points" in history_source

    assert ".two-legged-tie-list-item" in styles
    assert ".two-legged-tie-team-field" in styles
    assert "overflow-wrap: anywhere" in styles
    assert "@media (max-width: 420px)" in styles
    assert styles.count("min-height: 44px") >= 10

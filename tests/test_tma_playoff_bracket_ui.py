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


def test_champions_league_rounds_have_stable_keys_names_and_formats() -> None:
    source = _source()

    expected_rounds = (
        (
            'key: "playoff"',
            'name: "Стыковые матчи"',
            'selectorLabel: "Стыковые"',
        ),
        ('key: "round_of_16"', 'name: "1/8 финала"', 'selectorLabel: "1/8"'),
        ('key: "quarterfinal"', 'name: "1/4 финала"', 'selectorLabel: "1/4"'),
        ('key: "semifinal"', 'name: "1/2 финала"', 'selectorLabel: "1/2"'),
        ('key: "final"', 'name: "Финал"', 'selectorLabel: "Финал"'),
    )
    positions = []
    for round_key, round_name, selector_label in expected_rounds:
        positions.append(source.index(round_key))
        assert round_name in source
        assert selector_label in source
    assert positions == sorted(positions)
    assert 'format: "two_legged"' in source
    assert 'format: "single"' in source


def test_participant_bracket_groups_a_tie_and_both_legs_without_duplicates() -> None:
    bracket_source = _function_source("createPlayoffBracketCard")
    node_source = _function_source("createPlayoffBracketNode")
    references_source = _function_source("getPlayoffBracketReferences")
    list_source = _function_source("createMatchPredictionListItems")
    screen_source = _function_source("renderContestDetailsScreen")

    assert 'text: "Сетка плей-офф"' in bracket_source
    assert 'className: "playoff-round-selector"' in bracket_source
    assert 'className: "playoff-bracket-track"' in bracket_source
    assert 'role", "tablist"' in bracket_source
    assert 'role", "tabpanel"' in bracket_source
    assert 'button.setAttribute("aria-label", round.name)' in bracket_source
    assert 'column.setAttribute("aria-labelledby", button.id)' in bracket_source
    assert "?.selectorLabel || round.name" in bracket_source
    assert 'round.format === "single"' in bracket_source
    assert 'event.key === "ArrowRight"' in bracket_source
    assert 'event.key === "ArrowLeft"' in bracket_source
    assert 'event.key === "Home"' in bracket_source
    assert 'event.key === "End"' in bracket_source
    assert "buttons[nextIndex].focus()" in bracket_source

    assert "createTwoLeggedTiePredictionListItem(container, tie)" in node_source
    assert "for (const match of [firstLeg, secondLeg].filter(Boolean))" in node_source
    assert "createMatchListItem(container, match" in node_source
    assert "Пара появится автоматически" in node_source
    assert "Команды определены. Ждём официальные даты матчей." in node_source

    assert "tieIds.add(entityId)" in references_source
    assert "matchIds.add(firstLeg.id)" in references_source
    assert "matchIds.add(secondLeg.id)" in references_source
    assert "excludeMatchIds.has(match.id)" in list_source
    assert "excludeTieIds.has(tieId)" in list_source
    assert "createPlayoffBracketCard(contest, {" in screen_source
    assert "selectedRoundKey: currentPlayoffRoundKey" in screen_source
    assert '{ title: "Другие матчи" }' in screen_source


def test_participant_default_navigation_follows_latest_materialized_round() -> None:
    round_source = _function_source("getLatestMaterializedPlayoffRoundKey")
    default_tab_source = _function_source("getDefaultContestTab")
    active_tab_source = _function_source("getActiveContestTab")
    bracket_source = _function_source("createPlayoffBracketCard")
    screen_source = _function_source("renderContestDetailsScreen")

    assert 'container?.template_key !== "champions_league_2026_27"' in round_source
    assert "bracket.rounds.length - 1" in round_source
    assert 'entityType === "two_legged_tie"' in round_source
    assert "findBracketTie(container, node) !== null" in round_source
    assert 'entityType === "match"' in round_source
    assert "findBracketMatch(container, node) !== null" in round_source
    assert "return round.key" in round_source
    assert "return null" in round_source

    assert 'contest?.template_key === "champions_league_2026_27"' in default_tab_source
    assert "getLatestMaterializedPlayoffRoundKey(contest) === null" in (
        default_tab_source
    )
    assert '? "tournament"' in default_tab_source
    assert ': "matches"' in default_tab_source
    assert "getDefaultContestTab(contest)" in active_tab_source

    assert "selectedRoundKey = null" in bracket_source
    assert "round.key === selectedRoundKey" in bracket_source
    assert "selectRound(selectedRoundIndex >= 0 ? selectedRoundIndex : 0)" in (
        bracket_source
    )
    assert "track.scrollLeft = Math.max(" in bracket_source
    assert "getLatestMaterializedPlayoffRoundKey(contest)" in screen_source
    assert "selectedRoundKey: currentPlayoffRoundKey" in screen_source


def test_bracket_falls_back_to_round_metadata_and_keeps_legacy_matches() -> None:
    fallback_source = _function_source("buildPlayoffBracketFromRoundMetadata")
    getter_source = _function_source("getPlayoffBracket")
    screen_source = _function_source("renderContestDetailsScreen")
    match_item_source = _function_source("createMatchListItem")
    tie_item_source = _function_source("createTwoLeggedTiePredictionListItem")

    assert 'container?.template_key !== "champions_league_2026_27"' in fallback_source
    assert "getEntityRoundKey(entity)" in fallback_source
    assert 'entity: { type: "two_legged_tie", id: tieId }' in fallback_source
    assert 'entity: { type: "match", id: match.id }' in fallback_source
    assert "buildPlayoffBracketFromRoundMetadata(container)" in getter_source
    assert "if (!bracket || otherMatchItems.length > 0)" in screen_source
    assert "getEntityRoundName(match)" in match_item_source
    assert "getEntityRoundName(tie)" in tie_item_source


def test_shared_fixture_sync_ui_never_renders_the_provider_token() -> None:
    sync_source = _function_source("createFixtureSyncAdministrationCard")
    state_label_source = _function_source("getFixtureSyncStateLabel")
    shared_screen_source = _function_source("renderSharedTournamentScreen")
    participant_screen_source = _function_source("renderContestDetailsScreen")
    local_management_source = _function_source("renderContestManagementScreen")

    assert 'text: "Автоматическое обновление"' in sync_source
    assert 'attributionLink.textContent = "football-data.org"' in sync_source
    assert "только матчи, которые ещё не начались" in sync_source
    assert "точно и однозначно совпадать" in sync_source
    assert "пропущенные пары добавляются вручную" in sync_source
    assert 'idle: "Ждём пары"' in state_label_source
    assert "Источник доступен, но пары плей-офф ещё не опубликованы" in sync_source
    assert "или пока не получены" in sync_source
    assert "sync.token_configured !== true" in sync_source
    assert "sync.token" not in sync_source.replace("sync.token_configured", "")
    assert "api_token" not in sync_source
    assert "access_token" not in sync_source
    assert "`/api/tma/shared-tournaments/${tournament.id}/fixture-sync`" in sync_source
    assert (
        "`/api/tma/shared-tournaments/${tournament.id}/fixture-sync/run`" in sync_source
    )
    assert 'method: "PUT"' in sync_source
    assert 'method: "POST"' in sync_source
    assert "enabled: enabled.checked" in sync_source
    assert "expected_version: tournament.version" in sync_source
    assert "createFixtureSyncAdministrationCard(" in shared_screen_source
    assert "createFixtureSyncAdministrationCard(" not in participant_screen_source
    assert "createFixtureSyncAdministrationCard(" not in local_management_source
    assert 'mode: "management"' in shared_screen_source


def test_manual_correction_forms_send_round_key_and_exclude_final_for_pairs() -> None:
    round_select_source = _function_source("createPlayoffRoundSelect")
    local_match_source = _function_source("createMatchFormCard")
    shared_match_source = _function_source("createSharedMatchCreationCard")
    tie_source = _function_source("createTwoLeggedTieFormCard")
    wrapper_source = _function_source("createManualCorrectionCard")
    local_screen_source = _function_source("renderContestManagementScreen")
    shared_screen_source = _function_source("renderSharedTournamentScreen")

    assert 'if (!includeFinal && round.key === "final")' in round_select_source
    assert 'textContent = "Ручная коррекция"' in wrapper_source
    assert "round_key: roundKey" in local_match_source
    assert "round_key: roundSelect.value" in shared_match_source
    assert "includeFinal: false" in tie_source
    assert "round_key: roundInput.value" in tie_source
    assert "createManualCorrectionCard(manualCards" in local_screen_source
    assert "createManualCorrectionCard(manualCards" in shared_screen_source


def test_playoff_bracket_is_horizontal_on_desktop_and_one_round_on_mobile() -> None:
    styles = (main.TMA_DIRECTORY / "styles.css").read_text(encoding="utf-8")

    assert ".playoff-bracket-track" in styles
    assert "grid-auto-flow: column" in styles
    assert "overflow-x: auto" in styles
    assert "scroll-snap-type: inline mandatory" in styles
    assert ".playoff-round-column.is-mobile-selected" in styles
    assert ".playoff-round-selector" in styles
    assert (
        "grid-template-columns: minmax(0, 1.45fr) repeat(4, minmax(0, 1fr))" in styles
    )
    assert ".playoff-round-button {" in styles
    assert "min-height: 44px" in styles
    assert "@media (max-width: 560px)" in styles
    assert ".fixture-sync-card" in styles
    assert ".fixture-sync-status--idle" in styles
    idle_styles = styles[
        styles.index(".fixture-sync-status--idle") : styles.index(
            ".fixture-sync-status--syncing"
        )
    ]
    assert "color: var(--muted)" in idle_styles
    assert "var(--success)" not in idle_styles
    assert ".manual-correction-card" in styles

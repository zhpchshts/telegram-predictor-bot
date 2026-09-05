from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.database import database_connection, initialize_database
from app.main import create_app
from app.tma_api import get_telegram_administrators_client
from app.tma_launch import create_tma_launch_token
from tests.test_champion_predictions_api import (
    BOT_TOKEN,
    CHAT_TITLE,
    TELEGRAM_CHAT_ID,
    build_signed_init_data,
    build_tma_headers,
    configure_test_environment,
    create_tma_contest,
)


class FakeTelegramAdministratorsClient:
    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        return [SimpleNamespace(user=SimpleNamespace(id=123))]


def _client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_telegram_administrators_client] = (
        FakeTelegramAdministratorsClient
    )
    return TestClient(app)


def _headers_for_user(
    telegram_user_id: int,
    *,
    first_name: str,
    username: str,
) -> dict[str, str]:
    now = int(time.time())
    launch_token = create_tma_launch_token(
        chat_id=TELEGRAM_CHAT_ID,
        chat_type="supergroup",
        chat_title=CHAT_TITLE,
        secret=BOT_TOKEN,
        now=now,
    )
    init_data = build_signed_init_data(
        {
            "auth_date": str(now),
            "query_id": f"query-{telegram_user_id}",
            "user": json.dumps(
                {
                    "id": telegram_user_id,
                    "first_name": first_name,
                    "username": username,
                },
                separators=(",", ":"),
            ),
            "chat_type": "supergroup",
            "start_param": launch_token,
        }
    )
    return {"X-Telegram-Init-Data": init_data}


def _create_configured_ucl_contest(
    client: TestClient,
) -> tuple[int, list[int]]:
    create_response = client.post(
        "/api/tma/contests",
        headers={
            **build_tma_headers(),
            "Idempotency-Key": "create-ucl-general-stage-api",
        },
        json={
            "name": "Лига чемпионов 2026/27",
            "template_key": "champions_league_2026_27",
        },
    )
    assert create_response.status_code == 201
    contest_id = int(create_response.json()["contest"]["id"])
    teams_response = client.put(
        f"/api/tma/contests/{contest_id}/teams",
        headers=build_tma_headers(),
        json={"team_names": [f"Команда {number:02d}" for number in range(1, 37)]},
    )
    assert teams_response.status_code == 200
    team_ids = [
        int(team["id"]) for team in teams_response.json()["tournament_teams"]["teams"]
    ]
    settings_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": "2030-09-01T12:00:00Z",
            "direct_qualifier_count": 8,
            "elimination_qualifier_count": 12,
        },
    )
    assert settings_response.status_code == 200
    return contest_id, team_ids


def test_swiss_stage_api_configures_predicts_and_records_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )
    client = _client()
    contest = create_tma_contest(client)
    contest_id = int(contest["id"])

    teams_response = client.put(
        f"/api/tma/contests/{contest_id}/teams",
        headers=build_tma_headers(),
        json={"team_names": ["Альфа", "Бета", "Гамма"]},
    )
    assert teams_response.status_code == 200

    settings_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": "2030-01-01T12:00:00Z",
            "direct_qualifier_count": 1,
            "elimination_qualifier_count": 1,
        },
    )
    assert settings_response.status_code == 200
    settings = settings_response.json()["swiss_stage_prediction"]
    assert settings["is_enabled"] is True
    assert settings["settings_locked"] is False
    team_ids = {team["name"]: team["id"] for team in settings["candidates"]}

    monkeypatch.setattr(
        "app.tma_api._utc_now",
        lambda: datetime(2029, 1, 1, tzinfo=timezone.utc),
    )
    prediction_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": [team_ids["Альфа"]],
            "elimination_team_ids": [team_ids["Бета"]],
        },
    )
    assert prediction_response.status_code == 200
    assert (
        prediction_response.json()["swiss_stage_prediction"]["settings_locked"] is True
    )

    deadline_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": "2030-02-01T12:00:00Z",
            "direct_qualifier_count": 1,
            "elimination_qualifier_count": 1,
        },
    )
    assert deadline_response.status_code == 200
    assert (
        deadline_response.json()["swiss_stage_prediction"]["deadline_at"]
        == "2030-02-01T12:00:00Z"
    )

    locked_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": False,
            "deadline_at": None,
            "direct_qualifier_count": 1,
            "elimination_qualifier_count": 1,
        },
    )
    assert locked_response.status_code == 409

    boolean_id_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": [True],
            "elimination_team_ids": [team_ids["Бета"]],
        },
    )
    assert boolean_id_response.status_code == 422

    monkeypatch.setattr(
        "app.tma_api._utc_now",
        lambda: datetime(2030, 2, 2, tzinfo=timezone.utc),
    )
    result_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-result",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": [team_ids["Альфа"]],
            "elimination_team_ids": [team_ids["Гамма"]],
        },
    )
    assert result_response.status_code == 200
    result = result_response.json()["swiss_stage_prediction"]
    assert result["awarded_points"] == 2

    identical_result_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-result",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": [team_ids["Альфа"]],
            "elimination_team_ids": [team_ids["Гамма"]],
        },
    )
    assert identical_result_response.status_code == 200
    assert identical_result_response.json()["swiss_stage_prediction"] == result

    contest_response = client.get(
        f"/api/tma/contests/{contest_id}",
        headers=build_tma_headers(),
    )
    assert contest_response.status_code == 200
    leaderboard = contest_response.json()["contest"]["leaderboard"]
    assert leaderboard[0]["total_points"] == 2
    assert leaderboard[0]["swiss_stage_prediction_count"] == 1
    assert leaderboard[0]["calculated_predictions_count"] == 1
    assert leaderboard[0]["swiss_stage_prediction_history"]["awarded_points"] == 2

    changed_result_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-result",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": [team_ids["Бета"]],
            "elimination_team_ids": [team_ids["Гамма"]],
        },
    )
    assert changed_result_response.status_code == 200
    with database_connection(database_path) as connection:
        audit_counts = {
            str(row["event_type"]): int(row["event_count"])
            for row in connection.execute(
                """
                SELECT event_type, COUNT(*) AS event_count
                FROM audit_events
                WHERE contest_id = ?
                  AND event_type IN (
                      'swiss_stage_result_set',
                      'swiss_stage_result_changed'
                  )
                GROUP BY event_type
                """,
                (contest_id,),
            ).fetchall()
        }
    assert audit_counts == {
        "swiss_stage_result_changed": 1,
        "swiss_stage_result_set": 1,
    }

    audit_response = client.get(
        "/api/tma/audit-events",
        headers=build_tma_headers(),
    )
    assert audit_response.status_code == 200
    result_events = [
        event
        for event in audit_response.json()["events"]
        if event["event_type"]
        in {"swiss_stage_result_set", "swiss_stage_result_changed"}
    ]
    assert len(result_events) == 2
    assert {
        team["name"] for event in result_events for team in event["related_teams"]
    } == {"Альфа", "Бета", "Гамма"}


def test_general_stage_api_counts_partial_and_clears_selection_until_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )
    client = _client()
    contest_id, team_ids = _create_configured_ucl_contest(client)
    monkeypatch.setattr(
        "app.tma_api._utc_now",
        lambda: datetime(2030, 8, 1, tzinfo=timezone.utc),
    )

    partial_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": team_ids[:5],
            "elimination_team_ids": team_ids[8:17],
        },
    )

    assert partial_response.status_code == 200
    details = partial_response.json()["swiss_stage_prediction"]
    assert details["selection_mode"] == "up_to_limits"
    assert details["direct_correct_points"] == 2
    assert details["elimination_correct_points"] == 1
    assert details["cross_category_points"] == 0
    assert details["maximum_points"] == 28
    assert details["prediction"]["is_complete"] is False
    assert "playoff_teams" not in details["prediction"]
    assert len(details["prediction"]["direct_teams"]) == 5
    assert len(details["prediction"]["elimination_teams"]) == 9
    partial_contest = client.get(
        f"/api/tma/contests/{contest_id}",
        headers=build_tma_headers(),
    ).json()["contest"]
    assert partial_contest["leaderboard"][0]["swiss_stage_prediction_count"] == 1
    assert partial_contest["leaderboard"][0]["calculated_predictions_count"] == 0

    empty_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction",
        headers=build_tma_headers(),
        json={"direct_team_ids": [], "elimination_team_ids": []},
    )
    assert empty_response.status_code == 200
    empty_prediction = empty_response.json()["swiss_stage_prediction"]["prediction"]
    assert empty_prediction is None
    empty_contest = client.get(
        f"/api/tma/contests/{contest_id}",
        headers=build_tma_headers(),
    ).json()["contest"]
    assert empty_contest["leaderboard"][0].get("swiss_stage_prediction_count", 0) == 0
    assert empty_contest["leaderboard"][0]["calculated_predictions_count"] == 0

    unknown_identity_field = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": team_ids[:1],
            "elimination_team_ids": [],
            "telegram_user_id": 999,
        },
    )
    assert unknown_identity_field.status_code == 422

    monkeypatch.setattr(
        "app.tma_api._utc_now",
        lambda: datetime(2030, 9, 1, 12, tzinfo=timezone.utc),
    )
    at_deadline_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction",
        headers=build_tma_headers(),
        json={"direct_team_ids": team_ids[:1], "elimination_team_ids": []},
    )
    assert at_deadline_response.status_code == 409
    closed_contest = client.get(
        f"/api/tma/contests/{contest_id}",
        headers=build_tma_headers(),
    ).json()["contest"]
    assert (
        closed_contest["leaderboard"][0].get("swiss_stage_prediction_history") is None
    )


def test_general_stage_api_keeps_partial_predictions_private_and_serializes_breakdown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )
    client = _client()
    contest_id, team_ids = _create_configured_ucl_contest(client)
    alice_headers = _headers_for_user(
        456,
        first_name="Алиса",
        username="alice",
    )
    monkeypatch.setattr(
        "app.tma_api._utc_now",
        lambda: datetime(2030, 8, 1, tzinfo=timezone.utc),
    )
    alice_prediction_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-prediction",
        headers=alice_headers,
        json={
            "direct_team_ids": team_ids[:8],
            "elimination_team_ids": team_ids[8:20],
        },
    )
    assert alice_prediction_response.status_code == 200
    assert (
        alice_prediction_response.json()["swiss_stage_prediction"]["prediction"][
            "is_complete"
        ]
        is True
    )

    admin_view_response = client.get(
        f"/api/tma/contests/{contest_id}",
        headers=build_tma_headers(),
    )
    assert admin_view_response.status_code == 200
    admin_view = admin_view_response.json()["contest"]
    assert admin_view["swiss_stage_prediction"]["prediction"] is None
    alice_before_deadline = next(
        entry
        for entry in admin_view["leaderboard"]
        if entry["participant_username"] == "alice"
    )
    assert alice_before_deadline["swiss_stage_prediction_history"] is None

    partial_result_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-result",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": team_ids[:7],
            "elimination_team_ids": team_ids[8:20],
        },
    )
    assert partial_result_response.status_code == 409

    monkeypatch.setattr(
        "app.tma_api._utc_now",
        lambda: datetime(2030, 9, 1, 12, tzinfo=timezone.utc),
    )
    invalid_result_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-result",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": team_ids[:7],
            "elimination_team_ids": team_ids[8:20],
        },
    )
    assert invalid_result_response.status_code == 422
    result_response = client.put(
        f"/api/tma/contests/{contest_id}/swiss-stage-result",
        headers=build_tma_headers(),
        json={
            "direct_team_ids": team_ids[:8],
            "elimination_team_ids": team_ids[8:20],
        },
    )
    assert result_response.status_code == 200

    alice_view_response = client.get(
        f"/api/tma/contests/{contest_id}",
        headers=alice_headers,
    )
    assert alice_view_response.status_code == 200
    alice_view = alice_view_response.json()["contest"]
    own_prediction = alice_view["swiss_stage_prediction"]
    assert own_prediction["awarded_points"] == 28
    assert own_prediction["score_breakdown"] == {
        "correct_direct_count": 8,
        "direct_points": 16,
        "correct_elimination_count": 12,
        "elimination_points": 12,
        "total_points": 28,
    }
    alice_history = next(
        entry
        for entry in alice_view["leaderboard"]
        if entry["participant_username"] == "alice"
    )["swiss_stage_prediction_history"]
    assert alice_history["prediction"]["is_complete"] is True
    assert alice_history["actual_result"]["is_complete"] is True
    assert alice_history["score_breakdown"] == own_prediction["score_breakdown"]

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.database import database_connection, initialize_database
from app.main import create_app
from app.tma_api import get_telegram_administrators_client
from tests.test_champion_predictions_api import (
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
        lambda: datetime(2030, 1, 2, tzinfo=timezone.utc),
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

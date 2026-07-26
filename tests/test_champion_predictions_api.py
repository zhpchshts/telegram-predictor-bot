from __future__ import annotations

from datetime import datetime, timezone
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import create_connection, database_connection, initialize_database
from app.main import create_app as create_application
from app.tma_api import get_telegram_administrators_client
from app.tma_auth import calculate_init_data_hash
from app.tma_launch import create_tma_launch_token


BOT_TOKEN = "123456789:test-token"
TELEGRAM_CHAT_ID = -1001234567890
CHAT_TITLE = "Футбольные прогнозы"
FUTURE_DEADLINE = "2035-01-01T12:00:00Z"
PAST_DEADLINE = "2020-01-01T12:00:00Z"


class FakeTelegramAdministratorsClient:
    async def get_chat_administrators(self, chat_id: int) -> list[object]:
        assert chat_id == TELEGRAM_CHAT_ID
        return [SimpleNamespace(user=SimpleNamespace(id=123))]


def create_app() -> FastAPI:
    app = create_application()
    app.dependency_overrides[get_telegram_administrators_client] = (
        FakeTelegramAdministratorsClient
    )
    return app


def build_signed_init_data(fields: dict[str, str]) -> str:
    signed_fields = fields.copy()
    signed_fields["hash"] = calculate_init_data_hash(
        signed_fields,
        bot_token=BOT_TOKEN,
    )
    return urlencode(signed_fields)


def configure_test_environment(*, monkeypatch, database_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("BOT_USERNAME", "ZhpchshtsPredictorBot")
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ROLE_ENFORCEMENT_ENABLED", "false")


def build_tma_headers() -> dict[str, str]:
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
            "query_id": "AAEAAAE",
            "user": (
                '{"id":123,"first_name":"Eugene",'
                '"last_name":"Sabir","username":"evsab"}'
            ),
            "chat_type": "supergroup",
            "start_param": launch_token,
        }
    )
    return {"X-Telegram-Init-Data": init_data}


def create_tma_contest(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/tma/contests",
        headers={
            **build_tma_headers(),
            "Idempotency-Key": "create-contest",
        },
        json={"name": "ЧМ-2026: прогнозы"},
    )

    assert response.status_code == 201
    return response.json()["contest"]


def create_tma_match(
    client: TestClient,
    *,
    contest_id: int,
    idempotency_key: str,
) -> dict[str, object]:
    response = client.post(
        f"/api/tma/contests/{contest_id}/matches",
        headers={
            **build_tma_headers(),
            "Idempotency-Key": idempotency_key,
        },
        json={
            "home_team_name": "Аргентина",
            "away_team_name": "Бразилия",
            "starts_at_utc": "2020-06-11T18:00:00Z",
        },
    )

    assert response.status_code == 201
    return response.json()["match"]


def test_champion_prediction_api_configures_selects_and_records_champion(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )
    client = TestClient(create_app())
    contest = create_tma_contest(client)
    match = create_tma_match(
        client,
        contest_id=contest["id"],
        idempotency_key="create-match",
    )

    settings_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": FUTURE_DEADLINE,
            "points": 7,
        },
    )

    assert settings_response.status_code == 200
    assert settings_response.json()["champion_prediction"] == {
        "is_enabled": True,
        "deadline_at": FUTURE_DEADLINE,
        "points": 7,
        "candidates": [
            {
                "id": match["home_team_id"],
                "name": "Аргентина",
            },
            {
                "id": match["away_team_id"],
                "name": "Бразилия",
            },
        ],
        "prediction": None,
        "actual_champion": None,
        "is_open": True,
        "is_tournament_completed": False,
        "awarded_points": None,
    }

    prediction_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction",
        headers=build_tma_headers(),
        json={"predicted_team_id": match["home_team_id"]},
    )

    assert prediction_response.status_code == 200
    assert prediction_response.json() == {
        "prediction": {
            "id": match["home_team_id"],
            "name": "Аргентина",
        }
    }

    unavailable_champion_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion",
        headers=build_tma_headers(),
        json={"champion_team_id": match["home_team_id"]},
    )

    assert unavailable_champion_response.status_code == 409
    assert unavailable_champion_response.json() == {
        "detail": ("Чемпиона можно указать после завершения всех матчей конкурса.")
    }

    result_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": match["home_team_id"],
        },
    )

    assert result_response.status_code == 201

    open_deadline_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion",
        headers=build_tma_headers(),
        json={"champion_team_id": match["home_team_id"]},
    )
    assert open_deadline_response.status_code == 409
    assert open_deadline_response.json() == {
        "detail": (
            "Фактического чемпиона можно указать после закрытия прогнозов на чемпиона."
        )
    }

    with database_connection(database_path) as connection:
        connection.execute(
            "UPDATE contests SET champion_team_id = ? WHERE id = ?",
            (match["home_team_id"], contest["id"]),
        )

    completion_response = client.post(
        f"/api/tma/contests/{contest['id']}/complete",
        headers=build_tma_headers(),
    )
    assert completion_response.status_code == 409
    assert completion_response.json() == {
        "detail": "Конкурс можно завершить после закрытия прогнозов на чемпиона."
    }

    monkeypatch.setattr(
        "app.tma_api._utc_now",
        lambda: datetime(2035, 1, 2, tzinfo=timezone.utc),
    )

    champion_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion",
        headers=build_tma_headers(),
        json={"champion_team_id": match["home_team_id"]},
    )

    assert champion_response.status_code == 200
    assert champion_response.json() == {
        "champion": {
            "id": match["home_team_id"],
            "name": "Аргентина",
        }
    }

    locked_settings_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": "2036-01-01T12:00:00Z",
            "points": 8,
        },
    )
    assert locked_settings_response.status_code == 409
    assert locked_settings_response.json() == {
        "detail": (
            "Настройки прогноза на чемпиона нельзя изменить после указания "
            "фактического чемпиона."
        )
    }

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    contest_details = contest_response.json()["contest"]
    assert contest_details["champion_prediction"]["prediction"] == {
        "id": match["home_team_id"],
        "name": "Аргентина",
    }
    assert contest_details["champion_prediction"]["actual_champion"] == {
        "id": match["home_team_id"],
        "name": "Аргентина",
    }
    assert contest_details["champion_prediction"]["awarded_points"] == 7
    assert contest_details["leaderboard"] == [
        {
            "place": 1,
            "participant_name": "Eugene Sabir",
            "total_points": 7,
            "match_predictions_count": 0,
            "champion_prediction_count": 1,
            "total_matches_count": 1,
            "prediction_history": [],
            "champion_prediction_history": {
                "prediction": {
                    "id": match["home_team_id"],
                    "name": "Аргентина",
                },
                "actual_champion": {
                    "id": match["home_team_id"],
                    "name": "Аргентина",
                },
                "awarded_points": 7,
            },
        }
    ]


def test_get_contest_exposes_closed_champion_prediction_in_leaderboard(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )
    client = TestClient(create_app())
    contest = create_tma_contest(client)
    match = create_tma_match(
        client,
        contest_id=contest["id"],
        idempotency_key="create-match",
    )

    settings_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": FUTURE_DEADLINE,
            "points": 5,
        },
    )

    assert settings_response.status_code == 200

    prediction_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction",
        headers=build_tma_headers(),
        json={"predicted_team_id": match["away_team_id"]},
    )

    assert prediction_response.status_code == 200

    with create_connection(database_path) as connection:
        connection.execute(
            """
            UPDATE contests
            SET champion_prediction_deadline_at = ?
            WHERE id = ?
            """,
            (PAST_DEADLINE, contest["id"]),
        )

    result_response = client.put(
        f"/api/tma/contests/{contest['id']}/matches/{match['id']}/result",
        headers=build_tma_headers(),
        json={
            "home_score": 2,
            "away_score": 1,
            "advancing_team_id": match["home_team_id"],
        },
    )

    assert result_response.status_code == 201

    champion_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion",
        headers=build_tma_headers(),
        json={"champion_team_id": match["home_team_id"]},
    )

    assert champion_response.status_code == 200

    contest_response = client.get(
        f"/api/tma/contests/{contest['id']}",
        headers=build_tma_headers(),
    )

    assert contest_response.status_code == 200
    leaderboard_entry = contest_response.json()["contest"]["leaderboard"][0]
    assert leaderboard_entry["champion_prediction_history"] == {
        "prediction": {
            "id": match["away_team_id"],
            "name": "Бразилия",
        },
        "actual_champion": {
            "id": match["home_team_id"],
            "name": "Аргентина",
        },
        "awarded_points": 0,
    }


def test_champion_prediction_api_rejects_a_prediction_after_deadline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(
        monkeypatch=monkeypatch,
        database_path=database_path,
    )
    client = TestClient(create_app())
    contest = create_tma_contest(client)
    match = create_tma_match(
        client,
        contest_id=contest["id"],
        idempotency_key="create-match",
    )

    settings_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction/settings",
        headers=build_tma_headers(),
        json={
            "enabled": True,
            "deadline_at": PAST_DEADLINE,
            "points": 5,
        },
    )

    assert settings_response.status_code == 200

    prediction_response = client.put(
        f"/api/tma/contests/{contest['id']}/champion-prediction",
        headers=build_tma_headers(),
        json={"predicted_team_id": match["home_team_id"]},
    )

    assert prediction_response.status_code == 409
    assert prediction_response.json() == {"detail": "Прогноз на чемпиона уже закрыт."}

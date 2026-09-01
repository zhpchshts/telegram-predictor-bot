from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import create_connection, initialize_database
from app.tma_launch import create_tma_launch_token
from tests.test_tma_api import (
    BOT_TOKEN,
    CHAT_TITLE,
    TELEGRAM_CHAT_ID,
    build_signed_init_data,
    build_tma_headers,
    build_tma_match_payload,
    configure_test_environment,
    create_app,
    create_tma_contest,
)


def _headers_for_user(telegram_user_id: int) -> dict[str, str]:
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
                    "first_name": f"User {telegram_user_id}",
                    "username": f"user{telegram_user_id}",
                },
                separators=(",", ":"),
            ),
            "chat_type": "supergroup",
            "start_param": launch_token,
        }
    )
    return {"X-Telegram-Init-Data": init_data}


def _create_pair(client: TestClient, *, contest_id: int) -> dict[str, object]:
    match_payload = build_tma_match_payload(
        client,
        contest_id=contest_id,
        home_team_name="Аргентина",
        away_team_name="Бразилия",
        starts_at_utc="2030-06-11T18:00:00Z",
    )
    response = client.post(
        f"/api/tma/contests/{contest_id}/two-legged-ties",
        headers=build_tma_headers(idempotency_key="create-two-legged-tie"),
        json={
            "first_team_id": match_payload["home_team_id"],
            "second_team_id": match_payload["away_team_id"],
            "first_leg_starts_at_utc": "2030-06-11T18:00:00Z",
            "second_leg_starts_at_utc": "2030-06-18T18:00:00Z",
        },
    )
    assert response.status_code == 201
    return response.json()["two_legged_tie"]


def test_two_legged_tie_api_is_idempotent_and_keeps_predictions_personal(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(create_app())
    contest = create_tma_contest(client)
    contest_id = int(contest["id"])
    tie = _create_pair(client, contest_id=contest_id)

    repeated = client.post(
        f"/api/tma/contests/{contest_id}/two-legged-ties",
        headers=build_tma_headers(idempotency_key="create-two-legged-tie"),
        json={
            "first_team_id": tie["first_team"]["id"],
            "second_team_id": tie["second_team"]["id"],
            "first_leg_starts_at_utc": "2030-06-11T18:00:00Z",
            "second_leg_starts_at_utc": "2030-06-18T18:00:00Z",
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["was_created"] is False
    assert repeated.json()["two_legged_tie"]["id"] == tie["id"]

    contest_response = client.get(
        f"/api/tma/contests/{contest_id}", headers=build_tma_headers()
    )
    assert contest_response.status_code == 200
    details = contest_response.json()["contest"]
    assert details["two_legged_ties"] == [tie]
    assert [
        (match["is_two_legged"], match["leg_number"]) for match in details["matches"]
    ] == [
        (True, 1),
        (True, 2),
    ]

    tie_id = int(tie["id"])
    first_team_id = int(tie["first_team"]["id"])
    second_team_id = int(tie["second_team"]["id"])
    own_save = client.put(
        f"/api/tma/contests/{contest_id}/two-legged-ties/{tie_id}/prediction",
        headers=build_tma_headers(),
        json={"predicted_advancing_team_id": first_team_id},
    )
    assert own_save.status_code == 201

    other_headers = _headers_for_user(456)
    other_details = client.get(f"/api/tma/contests/{contest_id}", headers=other_headers)
    assert other_details.status_code == 200
    assert other_details.json()["contest"]["two_legged_ties"][0]["prediction"] is None

    other_save = client.put(
        f"/api/tma/contests/{contest_id}/two-legged-ties/{tie_id}/prediction",
        headers=other_headers,
        json={"predicted_advancing_team_id": second_team_id},
    )
    assert other_save.status_code == 201
    own_details = client.get(
        f"/api/tma/contests/{contest_id}", headers=build_tma_headers()
    )
    assert own_details.json()["contest"]["two_legged_ties"][0]["prediction"] == {
        "advancing_team_id": first_team_id
    }


def test_two_legged_tie_api_deadlines_and_maximum_seven_points(
    monkeypatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "predictor.db"
    initialize_database(database_path)
    configure_test_environment(monkeypatch=monkeypatch, database_path=database_path)
    client = TestClient(create_app())
    contest = create_tma_contest(client)
    contest_id = int(contest["id"])
    tie = _create_pair(client, contest_id=contest_id)
    tie_id = int(tie["id"])
    first_team_id = int(tie["first_team"]["id"])
    second_team_id = int(tie["second_team"]["id"])
    first_leg_id = int(tie["first_leg_match_id"])
    second_leg_id = int(tie["second_leg_match_id"])

    tie_prediction = client.put(
        f"/api/tma/contests/{contest_id}/two-legged-ties/{tie_id}/prediction",
        headers=build_tma_headers(),
        json={"predicted_advancing_team_id": first_team_id},
    )
    assert tie_prediction.status_code == 201
    for match_id in (first_leg_id, second_leg_id):
        prediction = client.put(
            f"/api/tma/contests/{contest_id}/matches/{match_id}/prediction",
            headers=build_tma_headers(),
            json={"predicted_home_score": 1, "predicted_away_score": 0},
        )
        assert prediction.status_code == 201

    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE matches SET starts_at_utc = ? WHERE id = ?",
            ("2020-06-11T18:00:00Z", first_leg_id),
        )

    closed_tie_prediction = client.put(
        f"/api/tma/contests/{contest_id}/two-legged-ties/{tie_id}/prediction",
        headers=build_tma_headers(),
        json={"predicted_advancing_team_id": first_team_id},
    )
    assert closed_tie_prediction.status_code == 409
    still_open_second_leg = client.put(
        f"/api/tma/contests/{contest_id}/matches/{second_leg_id}/prediction",
        headers=build_tma_headers(),
        json={"predicted_home_score": 1, "predicted_away_score": 0},
    )
    assert still_open_second_leg.status_code == 200

    with create_connection(database_path) as connection:
        connection.execute(
            "UPDATE matches SET starts_at_utc = ? WHERE id = ?",
            ("2020-06-18T18:00:00Z", second_leg_id),
        )

    closed_second_leg = client.put(
        f"/api/tma/contests/{contest_id}/matches/{second_leg_id}/prediction",
        headers=build_tma_headers(),
        json={"predicted_home_score": 2, "predicted_away_score": 0},
    )
    assert closed_second_leg.status_code == 409

    for match_id in (first_leg_id, second_leg_id):
        result = client.put(
            f"/api/tma/contests/{contest_id}/matches/{match_id}/result",
            headers=build_tma_headers(),
            json={"home_score": 1, "away_score": 0},
        )
        assert result.status_code == 201
        assert result.json()["result"]["advancing_team_id"] is None

    tie_result = client.put(
        f"/api/tma/contests/{contest_id}/two-legged-ties/{tie_id}/result",
        headers=build_tma_headers(),
        json={
            "second_leg_extra_time_home_score": 0,
            "second_leg_extra_time_away_score": 1,
        },
    )
    assert tie_result.status_code == 201
    assert tie_result.json()["result"] == {
        "aggregate_first_team_score": 1,
        "aggregate_second_team_score": 1,
        "advancing_team_id": first_team_id,
        "resolution_method": "extra_time",
        "second_leg_extra_time_home_score": 0,
        "second_leg_extra_time_away_score": 1,
        "second_leg_home_penalty_score": None,
        "second_leg_away_penalty_score": None,
    }

    repeated_result = client.put(
        f"/api/tma/contests/{contest_id}/two-legged-ties/{tie_id}/result",
        headers=build_tma_headers(),
        json={
            "second_leg_extra_time_home_score": 0,
            "second_leg_extra_time_away_score": 1,
        },
    )
    assert repeated_result.status_code == 200
    assert repeated_result.json()["was_created"] is False

    details = client.get(
        f"/api/tma/contests/{contest_id}", headers=build_tma_headers()
    ).json()["contest"]
    assert details["two_legged_ties"][0]["awarded_points"] == 1
    assert details["leaderboard"][0]["total_points"] == 7
    assert details["leaderboard"][0]["match_predictions_count"] == 2
    assert details["leaderboard"][0]["two_legged_tie_predictions_count"] == 1
    assert details["leaderboard"][0]["calculated_predictions_count"] == 3

    corrected_result = client.put(
        f"/api/tma/contests/{contest_id}/two-legged-ties/{tie_id}/result",
        headers=build_tma_headers(),
        json={
            "second_leg_extra_time_home_score": 1,
            "second_leg_extra_time_away_score": 0,
        },
    )
    assert corrected_result.status_code == 200
    assert corrected_result.json()["result"]["advancing_team_id"] == second_team_id

    with create_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT event_type, entity_type, entity_id,
                   before_state, after_state, metadata
            FROM audit_events
            WHERE metadata IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
    pair_audit_events = [
        row
        for row in rows
        if json.loads(row["metadata"]).get("two_legged_tie_id") == tie_id
    ]
    assert [row["event_type"] for row in pair_audit_events] == [
        "match_result_set",
        "match_result_changed",
    ]
    assert all(row["entity_type"] == "match" for row in pair_audit_events)
    assert all(row["entity_id"] == second_leg_id for row in pair_audit_events)
    first_before = json.loads(pair_audit_events[0]["before_state"])
    first_after = json.loads(pair_audit_events[0]["after_state"])
    corrected_after = json.loads(pair_audit_events[1]["after_state"])
    assert first_before["two_legged_tie_result"]["resolution_method"] is None
    assert first_after["two_legged_tie_result"] == {
        "resolution_method": "extra_time",
        "second_leg_away_penalty_score": None,
        "second_leg_extra_time_away_score": 1,
        "second_leg_extra_time_home_score": 0,
        "second_leg_home_penalty_score": None,
    }
    assert corrected_after["advancing_team_id"] == second_team_id
    assert (
        corrected_after["two_legged_tie_result"]["second_leg_extra_time_home_score"]
        == 1
    )

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import tma_api, tma_contracts


@pytest.mark.parametrize(
    "contract_name",
    (
        "CreateContestRequest",
        "CreateMatchRequest",
        "CreateSharedTournamentRequest",
        "SaveMatchPredictionRequest",
        "SavePredictionReminderSettingsRequest",
        "SaveSharedMatchResultRequest",
        "SaveSwissStageSelectionRequest",
    ),
)
def test_tma_api_reexports_transport_contracts(contract_name: str) -> None:
    assert getattr(tma_api, contract_name) is getattr(tma_contracts, contract_name)


@pytest.mark.parametrize(
    "invalid_team_id",
    (True, False, -(2**63) - 1, 2**63),
)
def test_sqlite_integer_contract_rejects_boolean_and_out_of_range_ids(
    invalid_team_id: object,
) -> None:
    with pytest.raises(ValidationError):
        tma_contracts.SaveChampionPredictionRequest(predicted_team_id=invalid_team_id)


def test_contract_extraction_preserves_existing_extra_field_policies() -> None:
    with pytest.raises(ValidationError):
        tma_contracts.CreateMatchRequest(
            home_team_id=1,
            away_team_id=2,
            starts_at_utc="2026-09-02T12:00:00+00:00",
            unknown="rejected",
        )

    request = tma_contracts.UpdateMatchStartRequest(
        starts_at_utc="2026-09-02T12:00:00+00:00",
        unknown="currently ignored",
    )

    assert request.model_dump() == {"starts_at_utc": "2026-09-02T12:00:00+00:00"}

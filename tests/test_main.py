from fastapi.testclient import TestClient

from app.main import create_app


def test_tma_index_is_available() -> None:
    client = TestClient(create_app())

    response = client.get("/tma/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Клевер" in response.text

"""Smoke test: the app boots and the health probe answers."""

from fastapi.testclient import TestClient

from backend.app import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_never_leaks_the_api_key():
    """The probe may report whether a key is configured, never the key itself."""
    body = client.get("/health").json()

    assert isinstance(body["gemini_key_configured"], bool)
    assert all(
        not isinstance(value, str) or "AIza" not in value for value in body.values()
    )

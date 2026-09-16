import pytest
from fastapi.testclient import TestClient

from server.app import app

HEADERS = {"Content-Type": "application/json", "X-ComputerUse": "1"}


def test_remote_origin_and_missing_marker_cannot_control_computer():
    with TestClient(app) as client:
        assert client.post("/api/sessions", json={"task": "test"}).status_code == 403
        assert client.post("/api/config", json={}, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
        assert client.get("/api/sessions", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_keys_not_returned_or_echoed_on_validation_errors():
    with TestClient(app) as client:
        secret = "test-secret-only"
        result = client.post("/api/config", json={"provider": "custom", "base_url": "http://localhost:1111/v1", "api_key": secret}, headers=HEADERS)
        assert result.status_code == 200
        assert result.json()["has_key"]
        assert secret not in result.text
        assert secret not in client.get("/api/config").text
        # An invalid field must not echo the full submitted body.
        bad = client.post("/api/config", json={"api_key": secret, "model": ""}, headers=HEADERS)
        assert bad.status_code == 422
        assert secret not in bad.text
        switched = client.post("/api/config", json={"provider": "custom", "base_url": "http://localhost:2222/v1"}, headers=HEADERS)
        assert not switched.json()["has_key"]
        client.post("/api/config", json={"provider": "demo"}, headers=HEADERS)

"""Smoke tests for Fleet Kit Hub (copied semantics, protocol=fleet)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

os.environ["AI_FLEET_DB"] = "sqlite:////tmp/fleet_kit_smoke.db"
os.environ["AI_FLEET_API_KEY"] = "smoke-test-key"

from app.main import app  # noqa: E402

HEADERS = {"X-API-Key": "smoke-test-key"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "smoke.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield


def test_core_flow_protocol_fleet():
    client = TestClient(app)
    assert client.get("/health").json()["version"] == "1.0.0-fleet-kit"

    r = client.post(
        "/agents",
        json={
            "agent_id": "agent-a",
            "name": "Agent A",
            "endpoint": "http://a.local",
            "capabilities": ["review"],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert client.post("/agents/agent-a/approve", headers=HEADERS).json()["status"] == "Active"

    r = client.post(
        "/tasks",
        json={"agent_id": "agent-a", "title": "review the plan"},
        headers=HEADERS,
    )
    task_id = r.json()["task_id"]

    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-a", "event_type": "task.assigned", "payload": {"note": "go"}},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True
    envelope = r.json()["message"]
    assert envelope["protocol"] == "fleet"
    assert "a2a" not in envelope["protocol"].lower()

    from app.messaging.bus import bus

    replay = bus.republish(envelope)
    assert replay["stored"] is False
    assert len(client.get(f"/tasks/{task_id}/messages", headers=HEADERS).json()) == 1

    assert client.post(f"/tasks/{task_id}/status", json={"status": "working"}, headers=HEADERS).json()["status"] == "working"
    assert client.post(f"/tasks/{task_id}/status", json={"status": "completed"}, headers=HEADERS).json()["status"] == "completed"
    assert client.get("/audit/verify", headers=HEADERS).json()["ok"] is True


def test_auth_required():
    client = TestClient(app)
    assert client.get("/agents").status_code == 401

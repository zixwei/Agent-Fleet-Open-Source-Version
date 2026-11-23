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


def test_tenant_cannot_suspend_foreign_agent(tmp_path, monkeypatch):
    import json

    db_path = tmp_path / "tenants.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv(
        "AI_FLEET_TENANT_API_KEYS",
        json.dumps({"key-a": "tenant-a", "key-b": "tenant-b"}),
    )
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    client = TestClient(app)
    ha = {"X-API-Key": "key-a"}
    hb = {"X-API-Key": "key-b"}
    assert (
        client.post(
            "/agents",
            json={
                "agent_id": "agent-a",
                "name": "A",
                "endpoint": "http://a",
                "capabilities": [],
            },
            headers=ha,
        ).status_code
        == 200
    )
    assert client.post("/agents/agent-a/approve", headers=ha).status_code == 200
    # Tenant B cannot suspend A's agent
    assert client.post("/agents/agent-a/suspend", headers=hb).status_code == 404
    # Agent key cannot suspend either
    monkeypatch.setenv(
        "AI_FLEET_AGENT_API_KEYS",
        json.dumps({"agent-key": {"agent_id": "agent-a", "tenant_id": "tenant-a"}}),
    )
    assert client.post("/agents/agent-a/suspend", headers={"X-API-Key": "agent-key"}).status_code == 403


def test_client_idempotency_key_retry():
    client = TestClient(app)
    client.post(
        "/agents",
        json={
            "agent_id": "agent-a",
            "name": "Agent A",
            "endpoint": "http://a.local",
            "capabilities": [],
        },
        headers=HEADERS,
    )
    client.post("/agents/agent-a/approve", headers=HEADERS)
    task_id = client.post(
        "/tasks", json={"agent_id": "agent-a", "title": "idem"}, headers=HEADERS
    ).json()["task_id"]
    body = {
        "sender_agent_id": "agent-a",
        "event_type": "ping",
        "payload": {"n": 1},
        "idempotency_key": "client-retry-1",
    }
    r1 = client.post(f"/tasks/{task_id}/messages", json=body, headers=HEADERS)
    r2 = client.post(f"/tasks/{task_id}/messages", json=body, headers=HEADERS)
    assert r1.json()["stored"] is True
    assert r2.json()["stored"] is False
    assert len(client.get(f"/tasks/{task_id}/messages", headers=HEADERS).json()) == 1

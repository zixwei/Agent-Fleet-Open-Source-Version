"""Regression tests for the Stage 0 baseline blockers found by Cue.

Ported unchanged from prototype/tests (fleet-kit is now the single Hub).

T01 (two-way message round-trip): a reply from the task's agent must be
routable back to the other party. Previously the recipient was hardcoded to
the task's agent, so every reply went to the sender itself (100/100 FAIL).

T08 (cross-tenant isolation): a task must be visible only to the tenant
that owns it. Previously GET /tasks/{id} (and status/message endpoints)
returned any task to any authenticated caller, leaking another tenant's
task with HTTP 200.

Run:  python -m pytest tests/test_t01_t08.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from app.identity.keyhash import hash_key_map

os.environ["AI_FLEET_DB"] = "sqlite:////tmp/ai_fleet_t01_t08.db"

TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
HEADERS_A = {"X-API-Key": "key-a"}
HEADERS_B = {"X-API-Key": "key-b"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "t01_t08.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(hash_key_map(TENANT_KEYS)))
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield


def _make_agent(client, agent_id, headers):
    r = client.post(
        "/agents",
        json={"agent_id": agent_id, "name": agent_id, "endpoint": f"http://{agent_id}.local"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    r = client.post(f"/agents/{agent_id}/approve", headers=headers)
    assert r.json()["status"] == "Active"


def test_t01_two_way_round_trip():
    """A -> B then B -> A: the reply's recipient must be A, not B."""
    from app.main import app

    client = TestClient(app)
    _make_agent(client, "agent-a", HEADERS_A)
    _make_agent(client, "agent-b", HEADERS_A)

    # Task assigned to agent-b.
    r = client.post("/tasks", json={"agent_id": "agent-b", "title": "round trip"}, headers=HEADERS_A)
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    # A sends to B: recipient B.
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-a", "recipient_agent_id": "agent-b",
              "event_type": "task.assigned", "payload": {"note": "go"}},
        headers=HEADERS_A,
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"]["recipient_agent_id"] == "agent-b"
    assert r.json()["message"]["sender_agent_id"] == "agent-a"

    # B replies to A: recipient must be A (was B before the fix).
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-b", "recipient_agent_id": "agent-a",
              "event_type": "task.reply", "payload": {"note": "done"}},
        headers=HEADERS_A,
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"]["recipient_agent_id"] == "agent-a"
    assert r.json()["message"]["sender_agent_id"] == "agent-b"

    # 100 round trips: every reply lands on A.
    for _ in range(100):
        r = client.post(
            f"/tasks/{task_id}/messages",
            json={"sender_agent_id": "agent-b", "recipient_agent_id": "agent-a",
                  "event_type": "task.reply", "payload": {"note": "ping"}},
            headers=HEADERS_A,
        )
        assert r.json()["message"]["recipient_agent_id"] == "agent-a"


def test_t01_recipient_defaults_to_task_agent():
    """Omitting recipient keeps the old behaviour (task's agent)."""
    from app.main import app

    client = TestClient(app)
    _make_agent(client, "agent-a", HEADERS_A)
    _make_agent(client, "agent-b", HEADERS_A)
    r = client.post("/tasks", json={"agent_id": "agent-b", "title": "default"}, headers=HEADERS_A)
    task_id = r.json()["task_id"]
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-a", "event_type": "task.note", "payload": {}},
        headers=HEADERS_A,
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"]["recipient_agent_id"] == "agent-b"


def test_t01_unknown_sender_or_recipient_rejected():
    """Unknown sender or recipient is a 400, not a silent misroute."""
    from app.main import app

    client = TestClient(app)
    _make_agent(client, "agent-a", HEADERS_A)
    _make_agent(client, "agent-b", HEADERS_A)
    r = client.post("/tasks", json={"agent_id": "agent-b", "title": "reject"}, headers=HEADERS_A)
    task_id = r.json()["task_id"]
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "ghost", "event_type": "task.note", "payload": {}},
        headers=HEADERS_A,
    )
    assert r.status_code == 400
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-a", "recipient_agent_id": "ghost",
              "event_type": "task.note", "payload": {}},
        headers=HEADERS_A,
    )
    assert r.status_code == 400


def test_t08_cross_tenant_task_isolation():
    """Tenant B must not read, move, or message tenant A's task (404)."""
    from app.main import app

    client = TestClient(app)
    _make_agent(client, "agent-a", HEADERS_A)
    r = client.post("/tasks", json={"agent_id": "agent-a", "title": "secret"}, headers=HEADERS_A)
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    # Owner can read it.
    assert client.get(f"/tasks/{task_id}", headers=HEADERS_A).status_code == 200

    # Other tenant: 404 everywhere, no leak of existence or content.
    r = client.get(f"/tasks/{task_id}", headers=HEADERS_B)
    assert r.status_code == 404
    assert "secret" not in r.text
    r = client.post(f"/tasks/{task_id}/status", json={"status": "working"}, headers=HEADERS_B)
    assert r.status_code == 404
    r = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_B)
    assert r.status_code == 404
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-a", "event_type": "task.note", "payload": {}},
        headers=HEADERS_B,
    )
    assert r.status_code == 404

    # List is tenant-scoped: B sees no task of A.
    assert client.get("/tasks", headers=HEADERS_B).json() == []
    assert len(client.get("/tasks", headers=HEADERS_A).json()) == 1

    # Audit list is tenant-scoped: B sees none of A's entries. Since T09,
    # B does see its own four refused attempts above (request.denied, 404).
    b_entries = client.get("/audit", headers=HEADERS_B).json()
    assert len(b_entries) == 4
    assert all(e["action"] == "request.denied" and e["detail"]["status"] == 404 for e in b_entries)
    assert "secret" not in json.dumps(b_entries)
    a_actions = {e["action"] for e in client.get("/audit", headers=HEADERS_A).json()}
    assert {"agent.register", "task.create"} <= a_actions
    assert "request.denied" not in a_actions

    # Owner still fully works after the denied attempts.
    r = client.post(f"/tasks/{task_id}/status", json={"status": "working"}, headers=HEADERS_A)
    assert r.status_code == 200
    assert r.json()["status"] == "working"

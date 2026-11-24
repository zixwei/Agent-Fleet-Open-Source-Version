"""Tests for per-agent identity (formal end-to-end T01 acceptance).

Cue's re-test of the receipt endpoint (commit 1d47927) passed at API
level but left formal T01 UNKNOWN: receipts were submitted by one test
harness under one tenant key, with recipient_agent_id taken from the
request body, so nothing proved the receiving agent itself got the
message. The next-round acceptance conditions (Cue post #vmqxdg) are:

1. Independent identity: each agent has its own credential; the server
   derives agent_id and tenant_id from it. Receipt identity comes from
   the authenticated principal, never from the body alone. Wrong
   identity, forged recipient, revoked credential and cross-tenant use
   are all rejected and record no receipt. Raw credentials are never
   shared between agents.
2. Actual delivery: independent agent processes complete the exchange
   over the real delivery path (covered live, see below).

Agent credentials: AI_FLEET_AGENT_API_KEYS maps key ->
{"agent_id", "tenant_id"}. A credential is live only while its agent is
registered and Active; suspending the agent revokes it.

Run:  python -m pytest tests/test_t01_agent_identity.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

os.environ["AI_FLEET_DB"] = "sqlite:////tmp/ai_fleet_t01_identity.db"

TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
AGENT_KEYS = {
    "key-agent-a": {"agent_id": "agent-a", "tenant_id": "tenant-a"},
    "key-agent-b": {"agent_id": "agent-b", "tenant_id": "tenant-a"},
    "key-agent-b-t2": {"agent_id": "agent-b", "tenant_id": "tenant-b"},
    "key-agent-c": {"agent_id": "agent-c", "tenant_id": "tenant-a"},
    "key-agent-ghost": {"agent_id": "ghost", "tenant_id": "tenant-a"},
}
HEADERS_A = {"X-API-Key": "key-a"}
HEADERS_AGENT_A = {"X-API-Key": "key-agent-a"}
HEADERS_AGENT_B = {"X-API-Key": "key-agent-b"}
HEADERS_AGENT_B_T2 = {"X-API-Key": "key-agent-b-t2"}
HEADERS_AGENT_C = {"X-API-Key": "key-agent-c"}
HEADERS_AGENT_GHOST = {"X-API-Key": "key-agent-ghost"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "t01_identity.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(TENANT_KEYS))
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(AGENT_KEYS))
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield


def _make_agent(client, agent_id, headers=HEADERS_A):
    r = client.post(
        "/agents",
        json={"agent_id": agent_id, "name": agent_id, "endpoint": f"http://{agent_id}.local"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    r = client.post(f"/agents/{agent_id}/approve", headers=headers)
    assert r.json()["status"] == "Active"


def _setup_task(client, assignee="agent-b"):
    _make_agent(client, "agent-a")
    _make_agent(client, "agent-b")
    r = client.post("/tasks", json={"agent_id": assignee, "title": "identity"}, headers=HEADERS_A)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _send(client, task_id, sender, recipient, headers=HEADERS_A):
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": sender, "recipient_agent_id": recipient,
              "event_type": "task.note", "payload": {"note": "ping"}},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["message"]


def _receipt_url(task_id, message_id):
    return f"/tasks/{task_id}/messages/{message_id}/receipt"


def test_unregistered_agent_credential_rejected():
    """A credential for an agent that was never registered: 401."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    message = _send(client, task_id, "agent-a", "agent-b")
    r = client.post(
        _receipt_url(task_id, message["message_id"]),
        json={},
        headers=HEADERS_AGENT_GHOST,
    )
    assert r.status_code == 401
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert msgs[0]["receipt"] is None


def test_suspended_agent_credential_revoked():
    """Suspending the agent revokes its credential: receipt 401."""
    from app.main import app

    client = TestClient(app)
    _make_agent(client, "agent-c")
    r = client.post("/tasks", json={"agent_id": "agent-c", "title": "revoke"}, headers=HEADERS_A)
    task_id = r.json()["task_id"]
    _make_agent(client, "agent-a")
    message = _send(client, task_id, "agent-a", "agent-c")
    r = client.post("/agents/agent-c/suspend", headers=HEADERS_A)
    assert r.json()["status"] == "Suspended"
    r = client.post(
        _receipt_url(task_id, message["message_id"]),
        json={},
        headers=HEADERS_AGENT_C,
    )
    assert r.status_code == 401
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert msgs[0]["receipt"] is None


def test_body_recipient_mismatch_rejected():
    """Body recipient_agent_id disagreeing with the credential: 400."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    message = _send(client, task_id, "agent-a", "agent-b")
    r = client.post(
        _receipt_url(task_id, message["message_id"]),
        json={"recipient_agent_id": "agent-a"},
        headers=HEADERS_AGENT_B,
    )
    assert r.status_code == 400
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert msgs[0]["receipt"] is None


def test_agent_credential_naming_other_tenants_agent_rejected():
    """A tenant-b credential that names agent-b (registered only in
    tenant-a) is not a live credential: 401, no receipt.

    Ported from the prototype, where this was 404: the prototype looked
    the agent up globally, so the key passed as live. fleet-kit checks the
    agent inside the credential's own tenant."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    message = _send(client, task_id, "agent-a", "agent-b")
    r = client.post(
        _receipt_url(task_id, message["message_id"]),
        json={},
        headers=HEADERS_AGENT_B_T2,
    )
    assert r.status_code == 401
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert msgs[0]["receipt"] is None


def test_send_bound_to_authenticated_agent():
    """Sending with an agent credential: sender must be that agent."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    # Agent A's credential claiming to send as B: 403.
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-b", "recipient_agent_id": "agent-a",
              "event_type": "task.note", "payload": {}},
        headers=HEADERS_AGENT_A,
    )
    assert r.status_code == 403
    # Agent A's credential sending as A: 200.
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-a", "recipient_agent_id": "agent-b",
              "event_type": "task.note", "payload": {}},
        headers=HEADERS_AGENT_A,
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"]["sender_agent_id"] == "agent-a"


def test_full_round_trip_with_agent_credentials():
    """A->B receipted by B, B->A receipted by A, each under its own key."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    m1 = _send(client, task_id, "agent-a", "agent-b", headers=HEADERS_AGENT_A)
    r = client.post(_receipt_url(task_id, m1["message_id"]), json={}, headers=HEADERS_AGENT_B)
    assert r.status_code == 200 and r.json()["stored"] is True
    m2 = _send(client, task_id, "agent-b", "agent-a", headers=HEADERS_AGENT_B)
    r = client.post(_receipt_url(task_id, m2["message_id"]), json={}, headers=HEADERS_AGENT_A)
    assert r.status_code == 200 and r.json()["stored"] is True
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert len(msgs) == 2
    assert msgs[0]["receipt"]["recipient_agent_id"] == "agent-b"
    assert msgs[1]["receipt"]["recipient_agent_id"] == "agent-a"

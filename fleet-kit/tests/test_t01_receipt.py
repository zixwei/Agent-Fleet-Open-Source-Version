"""Regression tests for the T01 delivery-receipt interface.

Formal T01 acceptance needs a receipt from the receiving end: routing
metadata (100/100) only proves the hub stored the message, not that the
recipient got it. The receipt endpoint closes that gap:

  POST /tasks/{task_id}/messages/{message_id}/receipt

The acting identity comes from the caller's AGENT credential
(X-API-Key: an agent key); a tenant key alone cannot receipt (401).
The request body may carry recipient_agent_id as a cross-check only and
is never trusted on its own. Duplicate receipts are no-ops
(stored=false, same receipt). Unknown message, message on another task,
or cross-tenant message all return 404; a credential for another agent
returns 403. GET /tasks/{task_id}/messages carries each message's
receipt (null if none).

Ported from prototype/tests; see the two cross-tenant tests at the end.

Run:  python -m pytest tests/test_t01_receipt.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from app.identity.keyhash import hash_key_map

os.environ["AI_FLEET_DB"] = "sqlite:////tmp/ai_fleet_t01_receipt.db"

TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
AGENT_KEYS = {
    "key-agent-a": {"agent_id": "agent-a", "tenant_id": "tenant-a"},
    "key-agent-b": {"agent_id": "agent-b", "tenant_id": "tenant-a"},
    "key-agent-b-t2": {"agent_id": "agent-b", "tenant_id": "tenant-b"},
    "key-agent-x-t2": {"agent_id": "agent-x", "tenant_id": "tenant-b"},
}
HEADERS_A = {"X-API-Key": "key-a"}
HEADERS_B = {"X-API-Key": "key-b"}
HEADERS_AGENT_A = {"X-API-Key": "key-agent-a"}
HEADERS_AGENT_B = {"X-API-Key": "key-agent-b"}
HEADERS_AGENT_B_T2 = {"X-API-Key": "key-agent-b-t2"}
HEADERS_AGENT_X_T2 = {"X-API-Key": "key-agent-x-t2"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "t01_receipt.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(hash_key_map(TENANT_KEYS)))
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(hash_key_map(AGENT_KEYS)))
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


def _setup_task(client):
    """Agents A and B, task assigned to B. Returns task_id."""
    _make_agent(client, "agent-a", HEADERS_A)
    _make_agent(client, "agent-b", HEADERS_A)
    r = client.post("/tasks", json={"agent_id": "agent-b", "title": "receipt"}, headers=HEADERS_A)
    assert r.status_code == 200, r.text
    return r.json()["task_id"]


def _send(client, task_id, sender, recipient):
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": sender, "recipient_agent_id": recipient,
              "event_type": "task.note", "payload": {"note": "ping"}},
        headers=HEADERS_A,
    )
    assert r.status_code == 200, r.text
    return r.json()["message"]


def _receipt_url(task_id, message_id):
    return f"/tasks/{task_id}/messages/{message_id}/receipt"


def test_receipt_round_trip_visible_in_list():
    """B receipts A's message with B's own credential; GET shows it."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    message = _send(client, task_id, "agent-a", "agent-b")

    # Before receipt: no receipt attached.
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert len(msgs) == 1
    assert msgs[0]["receipt"] is None

    r = client.post(
        _receipt_url(task_id, message["message_id"]),
        json={},
        headers=HEADERS_AGENT_B,
    )
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True
    receipt = r.json()["receipt"]
    assert receipt["message_id"] == message["message_id"]
    assert receipt["recipient_agent_id"] == "agent-b"
    assert receipt["tenant_id"] == "tenant-a"
    assert receipt["received_at"]

    # After receipt: the list carries it.
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert msgs[0]["receipt"]["message_id"] == message["message_id"]
    assert msgs[0]["receipt"]["recipient_agent_id"] == "agent-b"


def test_receipt_is_idempotent():
    """A duplicate receipt is a no-op returning the same stored receipt."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    message = _send(client, task_id, "agent-a", "agent-b")
    url = _receipt_url(task_id, message["message_id"])
    first = client.post(url, json={}, headers=HEADERS_AGENT_B).json()
    assert first["stored"] is True
    second = client.post(url, json={}, headers=HEADERS_AGENT_B)
    assert second.status_code == 200
    assert second.json()["stored"] is False
    assert second.json()["receipt"] == first["receipt"]


def test_receipt_wrong_agent_credential_rejected():
    """Agent A's credential cannot receipt a message addressed to B: 403."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    message = _send(client, task_id, "agent-a", "agent-b")
    r = client.post(
        _receipt_url(task_id, message["message_id"]),
        json={},
        headers=HEADERS_AGENT_A,
    )
    assert r.status_code == 403
    # And no receipt was recorded.
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert msgs[0]["receipt"] is None


def test_receipt_requires_agent_credential():
    """A tenant key alone cannot receipt: 401 (identity must be an agent)."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    message = _send(client, task_id, "agent-a", "agent-b")
    r = client.post(
        _receipt_url(task_id, message["message_id"]),
        json={"recipient_agent_id": "agent-b"},
        headers=HEADERS_A,
    )
    assert r.status_code == 401
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert msgs[0]["receipt"] is None


def test_receipt_unknown_message_404():
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    r = client.post(
        _receipt_url(task_id, "no-such-message"),
        json={},
        headers=HEADERS_AGENT_B,
    )
    assert r.status_code == 404


def test_receipt_message_on_another_task_404():
    """A receipt filed under the wrong task id is 404, not a leak."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    other = client.post(
        "/tasks", json={"agent_id": "agent-b", "title": "other"}, headers=HEADERS_A
    ).json()["task_id"]
    message = _send(client, task_id, "agent-a", "agent-b")
    r = client.post(
        _receipt_url(other, message["message_id"]),
        json={},
        headers=HEADERS_AGENT_B,
    )
    assert r.status_code == 404


def test_receipt_cross_tenant_404():
    """A real tenant-b agent's credential on a tenant-a message: 404, no leak."""
    from app.main import app

    client = TestClient(app)
    task_id = _setup_task(client)
    _make_agent(client, "agent-x", HEADERS_B)
    message = _send(client, task_id, "agent-a", "agent-b")
    r = client.post(
        _receipt_url(task_id, message["message_id"]),
        json={},
        headers=HEADERS_AGENT_X_T2,
    )
    assert r.status_code == 404
    # Nothing recorded on A's side either.
    msgs = client.get(f"/tasks/{task_id}/messages", headers=HEADERS_A).json()
    assert msgs[0]["receipt"] is None


def test_receipt_credential_naming_other_tenants_agent_401():
    """A tenant-b key naming agent-b (which exists only in tenant-a) is not
    a live credential: 401. (404 in the prototype, which looked agents up
    globally.)"""
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

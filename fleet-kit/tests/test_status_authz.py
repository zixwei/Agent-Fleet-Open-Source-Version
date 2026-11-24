"""Key split: agent credentials only move their own tasks (PR #1 follow-up).

MCP hosts carry agent keys only, so task operations must not need the
tenant admin key, and an agent key must not be able to drive other
agents' tasks:

- assigned agent: working / completed / failed / cancelled
- creating agent: cancelled only
- any other agent: 403
- tenant/admin key: any legal transition (operator override)
- agent keys never pass admin endpoints (register/approve/suspend): 403

Run:  python -m pytest tests/test_status_authz.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
AGENT_KEYS = {
    "key-orch": {"agent_id": "orch", "tenant_id": "tenant-a"},
    "key-worker": {"agent_id": "worker", "tenant_id": "tenant-a"},
    "key-other": {"agent_id": "other", "tenant_id": "tenant-a"},
    "key-worker-wrong-tenant": {"agent_id": "worker", "tenant_id": "tenant-b"},
}
ADMIN = {"X-API-Key": "key-a"}
ORCH = {"X-API-Key": "key-orch"}
WORKER = {"X-API-Key": "key-worker"}
OTHER = {"X-API-Key": "key-other"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "authz.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(TENANT_KEYS))
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(AGENT_KEYS))
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield


@pytest.fixture
def client():
    from app.main import app

    c = TestClient(app)
    for agent_id in ("orch", "worker", "other"):
        r = c.post(
            "/agents",
            json={"agent_id": agent_id, "name": agent_id, "endpoint": f"http://{agent_id}"},
            headers=ADMIN,
        )
        assert r.status_code == 200, r.text
        assert c.post(f"/agents/{agent_id}/approve", headers=ADMIN).status_code == 200
    return c


def _task_by_orch(client):
    r = client.post("/tasks", json={"agent_id": "worker", "title": "authz"}, headers=ORCH)
    assert r.status_code == 200, r.text
    assert r.json()["created_by_agent_id"] == "orch"
    return r.json()["task_id"]


def _status(client, task_id, status, headers):
    return client.post(f"/tasks/{task_id}/status", json={"status": status}, headers=headers)


def test_assignee_drives_its_task(client):
    task_id = _task_by_orch(client)
    assert _status(client, task_id, "working", WORKER).status_code == 200
    r = _status(client, task_id, "completed", WORKER)
    assert r.status_code == 200 and r.json()["status"] == "completed"


def test_creator_may_cancel_but_not_progress(client):
    task_id = _task_by_orch(client)
    assert _status(client, task_id, "working", ORCH).status_code == 403
    assert _status(client, task_id, "cancelled", ORCH).status_code == 200


def test_unrelated_agent_refused(client):
    task_id = _task_by_orch(client)
    for status in ("working", "cancelled", "completed"):
        assert _status(client, task_id, status, OTHER).status_code == 403
    assert client.get(f"/tasks/{task_id}", headers=ADMIN).json()["status"] == "submitted"


def test_admin_override_still_works(client):
    r = client.post("/tasks", json={"agent_id": "worker", "title": "admin"}, headers=ADMIN)
    task_id = r.json()["task_id"]
    assert r.json()["created_by_agent_id"] is None
    assert _status(client, task_id, "cancelled", ADMIN).status_code == 200


def test_unknown_status_is_400(client):
    task_id = _task_by_orch(client)
    assert _status(client, task_id, "done-ish", WORKER).status_code == 400


def test_illegal_transition_is_400_for_assignee(client):
    task_id = _task_by_orch(client)
    assert _status(client, task_id, "completed", WORKER).status_code == 400


def test_agent_keys_cannot_use_admin_endpoints(client):
    for headers in (ORCH, WORKER):
        assert client.post("/agents/other/suspend", headers=headers).status_code == 403
        assert client.post("/agents/other/approve", headers=headers).status_code == 403
        r = client.post(
            "/agents",
            json={"agent_id": "new", "name": "n", "endpoint": "http://n"},
            headers=headers,
        )
        assert r.status_code == 403


def test_credential_naming_agent_outside_its_tenant_is_401(client):
    task_id = _task_by_orch(client)
    r = _status(client, task_id, "working", {"X-API-Key": "key-worker-wrong-tenant"})
    assert r.status_code == 401

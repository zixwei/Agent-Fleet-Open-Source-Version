"""T09 audit record correlation (whitepaper §9.1, plan §21.3 B).

Gaps this closes (found reading the PR #1 code):
1. trace_id / correlation_id / context_id were built into the envelope and
   then dropped: the messages table had no columns for them.
2. Audit entries named only the tenant, never the acting agent.
3. message.send entries carried no message_id, so they could not be joined
   to the message they record.
4. Refused requests (401/403/404/409) left no audit trace.

Run:  python -m pytest tests/test_t09_correlation.py -q
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
AGENT_KEYS = {
    "key-orch": {"agent_id": "orch", "tenant_id": "tenant-a"},
    "key-worker": {"agent_id": "worker", "tenant_id": "tenant-a"},
    "key-ghost": {"agent_id": "ghost", "tenant_id": "tenant-a"},
}
ADMIN = {"X-API-Key": "key-a"}
ADMIN_B = {"X-API-Key": "key-b"}
ORCH = {"X-API-Key": "key-orch"}
WORKER = {"X-API-Key": "key-worker"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "t09.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(TENANT_KEYS))
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(AGENT_KEYS))
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield db_path


@pytest.fixture
def client():
    from app.main import app

    c = TestClient(app)
    for agent_id in ("orch", "worker"):
        r = c.post(
            "/agents",
            json={"agent_id": agent_id, "name": agent_id, "endpoint": f"http://{agent_id}"},
            headers=ADMIN,
        )
        assert r.status_code == 200, r.text
        assert c.post(f"/agents/{agent_id}/approve", headers=ADMIN).status_code == 200
    return c


def _round_trip(client):
    """orch -> worker assign, receipt, worker -> orch reply, receipt, done."""
    task = client.post("/tasks", json={"agent_id": "worker", "title": "t09"}, headers=ORCH).json()
    tid = task["task_id"]
    m1 = client.post(
        f"/tasks/{tid}/messages",
        json={"sender_agent_id": "orch", "recipient_agent_id": "worker",
              "event_type": "task.assign", "payload": {}},
        headers=ORCH,
    ).json()["message"]
    assert client.post(f"/tasks/{tid}/messages/{m1['message_id']}/receipt", json={}, headers=WORKER).status_code == 200
    assert client.post(f"/tasks/{tid}/status", json={"status": "working"}, headers=WORKER).status_code == 200
    m2 = client.post(
        f"/tasks/{tid}/messages",
        json={"sender_agent_id": "worker", "recipient_agent_id": "orch",
              "event_type": "task.result", "payload": {"result": "ok"},
              "correlation_id": m1["message_id"]},
        headers=WORKER,
    ).json()["message"]
    assert client.post(f"/tasks/{tid}/messages/{m2['message_id']}/receipt", json={}, headers=ORCH).status_code == 200
    assert client.post(f"/tasks/{tid}/status", json={"status": "completed"}, headers=WORKER).status_code == 200
    return task, m1, m2


def _entries(db_path, **where):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()]
    conn.close()
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return [r for r in rows if all(r.get(k) == v for k, v in where.items())]


def test_correlation_report_is_complete(client):
    task, m1, m2 = _round_trip(client)
    r = client.get(f"/tasks/{task['task_id']}/correlation", headers=ADMIN)
    assert r.status_code == 200
    corr = r.json()
    assert corr["ok"] is True, corr
    assert corr["trace_id"] == task["trace_id"]
    assert corr["status"] == "completed"
    assert corr["messages_total"] == corr["messages_attributed"] == 2
    assert corr["messages_on_task_trace"] == 2
    assert corr["receipts_total"] == corr["receipts_audited"] == 2
    assert corr["problems"] == []


def test_trace_and_correlation_fields_are_persisted(client):
    task, m1, m2 = _round_trip(client)
    assert task["trace_id"]
    msgs = client.get(f"/tasks/{task['task_id']}/messages", headers=ADMIN).json()
    by_id = {m["message_id"]: m for m in msgs}
    for m in msgs:
        assert m["trace_id"] == task["trace_id"]
        assert m["context_id"] == task["task_id"]
        assert m["protocol"] == "fleet" and m["protocol_version"] == "1.0"
    assert by_id[m1["message_id"]]["correlation_id"] == m1["message_id"]
    assert by_id[m2["message_id"]]["correlation_id"] == m1["message_id"]


def test_audit_records_acting_agent_and_message_ids(client, fresh_db):
    task, m1, m2 = _round_trip(client)
    tid = task["task_id"]
    sends = _entries(fresh_db, action="message.send", target=tid)
    assert {e["detail"]["message_id"] for e in sends} == {m1["message_id"], m2["message_id"]}
    for e in sends:
        assert e["actor"] == "tenant-a"
        assert e["detail"]["principal"] == {"kind": "agent", "agent_id": e["detail"]["sender_agent_id"]}
        assert e["detail"]["trace_id"] == task["trace_id"]
    receipts = _entries(fresh_db, action="message.receipt", target=tid)
    assert {(e["detail"]["message_id"], e["detail"]["principal"]["agent_id"]) for e in receipts} == {
        (m1["message_id"], "worker"), (m2["message_id"], "orch")
    }
    statuses = [(e["detail"]["from"], e["detail"]["to"]) for e in _entries(fresh_db, action="task.status", target=tid)]
    assert statuses == [("submitted", "working"), ("working", "completed")]
    created = _entries(fresh_db, action="task.create", target=tid)
    assert created[0]["detail"]["principal"] == {"kind": "agent", "agent_id": "orch"}
    reg = _entries(fresh_db, action="agent.register")
    assert reg and all(e["detail"]["principal"] == {"kind": "tenant", "agent_id": None} for e in reg)


def test_duplicate_points_at_original_message(client, fresh_db):
    task = client.post("/tasks", json={"agent_id": "worker", "title": "dup"}, headers=ORCH).json()
    body = {"sender_agent_id": "orch", "event_type": "note", "payload": {}, "idempotency_key": "k1"}
    first = client.post(f"/tasks/{task['task_id']}/messages", json=body, headers=ORCH).json()
    again = client.post(f"/tasks/{task['task_id']}/messages", json=body, headers=ORCH).json()
    assert again["stored"] is False
    dup = _entries(fresh_db, action="message.duplicate", target=task["task_id"])
    assert len(dup) == 1
    assert dup[0]["detail"]["message_id"] == first["message"]["message_id"]
    assert client.get(f"/tasks/{task['task_id']}/correlation", headers=ADMIN).json()["ok"] is True


def test_caller_trace_id_is_propagated_and_validated(client):
    task = client.post("/tasks", json={"agent_id": "worker", "title": "trace"}, headers=ORCH).json()
    url = f"/tasks/{task['task_id']}/messages"
    r = client.post(url, json={"sender_agent_id": "orch", "event_type": "note", "payload": {},
                               "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}, headers=ORCH)
    assert r.status_code == 200
    assert r.json()["message"]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    corr = client.get(f"/tasks/{task['task_id']}/correlation", headers=ADMIN).json()
    assert corr["ok"] is True and corr["messages_on_task_trace"] == 0
    for bad in ("has space", "a:b", "x" * 129, ""):
        r = client.post(url, json={"sender_agent_id": "orch", "event_type": "note", "payload": {},
                                   "trace_id": bad}, headers=ORCH)
        assert r.status_code == 422, bad


def test_unaudited_message_is_reported(client):
    """A message written around the API (no audit entry) is not attributed."""
    task = client.post("/tasks", json={"agent_id": "worker", "title": "gap"}, headers=ORCH).json()
    from app.messaging.bus import bus

    rogue = bus.send(tenant_id="tenant-a", sender_agent_id="orch", recipient_agent_id="worker",
                     task_id=task["task_id"], event_type="note", payload={}, trace_id=task["trace_id"])
    corr = client.get(f"/tasks/{task['task_id']}/correlation", headers=ADMIN).json()
    assert corr["ok"] is False
    assert corr["messages_attributed"] == 0 and corr["messages_total"] == 1
    assert corr["problems"] == [{"message_id": rogue["message"]["message_id"], "missing": ["audit.message.send"]}]


def test_status_changed_around_the_api_is_reported(client):
    task = client.post("/tasks", json={"agent_id": "worker", "title": "status"}, headers=ORCH).json()
    from app import orchestration

    orchestration.set_status(task["task_id"], "cancelled")  # no audit entry
    corr = client.get(f"/tasks/{task['task_id']}/correlation", headers=ADMIN).json()
    assert corr["ok"] is False
    assert "audit.task.status" in corr["problems"][0]["missing"][0]


def test_denied_requests_are_audited(client, fresh_db):
    task = client.post("/tasks", json={"agent_id": "worker", "title": "deny"}, headers=ORCH).json()
    tid = task["task_id"]
    # 404: another tenant probes the task. Filed under the prober's tenant.
    assert client.get(f"/tasks/{tid}", headers=ADMIN_B).status_code == 404
    # 403: orchestrator (not the assignee) tries to complete it.
    assert client.post(f"/tasks/{tid}/status", json={"status": "working"}, headers=ORCH).status_code == 403
    # 401: unknown key.
    assert client.get("/tasks", headers={"X-API-Key": "nope-secret-value"}).status_code == 401
    # 401: a key for an agent that is not registered (refused agent key).
    assert client.get("/tasks", headers={"X-API-Key": "key-ghost"}).status_code == 401

    denied = _entries(fresh_db, action="request.denied")
    by_status = {}
    for e in denied:
        by_status.setdefault(e["detail"]["status"], []).append(e)
    probe = by_status[404][0]
    assert probe["actor"] == "tenant-b" and probe["detail"]["principal"]["kind"] == "tenant"
    forbidden = by_status[403][0]
    assert forbidden["actor"] == "tenant-a"
    assert forbidden["detail"]["principal"] == {"kind": "agent", "agent_id": "orch"}
    assert forbidden["detail"]["method"] == "POST" and forbidden["detail"]["path"] == f"/tasks/{tid}/status"
    actors_401 = {e["actor"] for e in by_status[401]}
    assert actors_401 == {"unauthenticated", "tenant-a"}
    ghost = [e for e in by_status[401] if e["actor"] == "tenant-a"][0]
    assert ghost["detail"]["principal"] == {"kind": "refused_agent_key", "agent_id": "ghost"}
    # The presented key is never stored.
    dump = json.dumps(_entries(fresh_db))
    assert "nope-secret-value" not in dump and "key-ghost" not in dump and "key-orch" not in dump
    # Denials do not break the chain or the task's correlation.
    assert client.get("/audit/verify", headers=ADMIN).json()["ok"] is True
    assert client.get(f"/tasks/{tid}/correlation", headers=ADMIN).json()["ok"] is True


def test_unknown_route_is_not_audited(client, fresh_db):
    before = len(_entries(fresh_db))
    assert client.get("/no-such-route", headers=ADMIN).status_code == 404
    assert len(_entries(fresh_db)) == before


def test_correlation_is_tenant_scoped(client):
    task, _, _ = _round_trip(client)
    assert client.get(f"/tasks/{task['task_id']}/correlation", headers=ADMIN_B).status_code == 404


def test_old_database_is_migrated(tmp_path, monkeypatch):
    """A pre-T09 database gains the new columns; old rows stay NULL and are
    reported as unattributed instead of being back-filled."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
    CREATE TABLE tasks (task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'submitted', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL);
    CREATE TABLE messages (idempotency_key TEXT PRIMARY KEY, message_id TEXT NOT NULL,
        event_id TEXT NOT NULL, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        sender_agent_id TEXT NOT NULL, recipient_agent_id TEXT NOT NULL, event_type TEXT NOT NULL,
        payload TEXT NOT NULL, created_at TEXT NOT NULL);
    INSERT INTO tasks VALUES ('t-old', 'tenant-a', 'worker', 'old', 'submitted', 'n', 'n');
    INSERT INTO messages VALUES ('k', 'm-old', 'e', 't-old', 'tenant-a', 'orch', 'worker', 'note', '{}', 'n');
    """)
    conn.commit()
    conn.close()
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db
    from app import observability as audit

    db.init_db()
    db.init_db()  # idempotent
    assert db.get_task("t-old")["trace_id"] is None
    assert db.get_message_by_id("m-old")["trace_id"] is None
    corr = audit.correlate_task("tenant-a", "t-old")
    assert corr["ok"] is False and corr["messages_attributed"] == 0

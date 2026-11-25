"""Codex review follow-ups on PR #1 / PR #8 (head 7a44718).

Covers the review items that were still open on that head, plus regression
pins for the ones already fixed by earlier follow-ups:

- tenant-scoped admin denial (agent approve/suspend)
- idempotency-key reuse and delimiter collision
- /audit/verify past 1,000 entries (window anchoring)
- atomic transitions under concurrency, and the audit recording the CAS target
- deletion / rewrite of the trusted checkpoint row
- task trace_id changed after creation
- legacy agents migrated into the configured default tenant
- retries of messages stored under the pre-encoding idempotency key
- correlation over mixed old-format / new-format status history

Run:  python -m pytest tests/test_review_followups.py -q
"""
import json
import os
import sqlite3
import sys
import threading
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
AGENT_KEYS = {
    "key-orch": {"agent_id": "orch", "tenant_id": "tenant-a"},
    "key-worker": {"agent_id": "worker", "tenant_id": "tenant-a"},
}
ADMIN = {"X-API-Key": "key-a"}
ADMIN_B = {"X-API-Key": "key-b"}
ORCH = {"X-API-Key": "key-orch"}
WORKER = {"X-API-Key": "key-worker"}
ROUNDS = 25


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "followups.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    # Hashed form only (B2): the app ignores plaintext entries (fail closed),
    # so the fixtures are stored as keyhash.hash_key_map(...) of the keys the
    # tests still present in plaintext in X-API-Key headers.
    from app.identity import keyhash

    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(keyhash.hash_key_map(TENANT_KEYS)))
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(keyhash.hash_key_map(AGENT_KEYS)))
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


def _entries(db_path, **where):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()]
    conn.close()
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return [r for r in rows if all(r.get(k) == v for k, v in where.items())]


def _sql(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    conn.execute(sql, params)
    conn.commit()
    conn.close()


# --- 1. tenant-scoped admin denial -------------------------------------------

def test_foreign_tenant_and_agent_keys_cannot_administer_agents(client, fresh_db):
    # Tenant B's admin key: 404 (existence not leaked), nothing changes.
    for action in ("suspend", "approve"):
        assert client.post(f"/agents/worker/{action}", headers=ADMIN_B).status_code == 404
    # Agent keys (even of the same tenant): 403, admin principal required.
    for headers in (ORCH, WORKER):
        assert client.post("/agents/worker/suspend", headers=headers).status_code == 403
    # The agent is still Active, its credential still works, its tasks run.
    agents = {a["agent_id"]: a for a in client.get("/agents", headers=ADMIN).json()}
    assert agents["worker"]["status"] == "Active"
    task = client.post("/tasks", json={"agent_id": "worker", "title": "still alive"}, headers=ORCH)
    assert task.status_code == 200
    r = client.post(f"/tasks/{task.json()['task_id']}/status", json={"status": "working"}, headers=WORKER)
    assert r.status_code == 200
    # No agent.suspend entry was written for anyone.
    assert _entries(fresh_db, action="agent.suspend") == []
    # The owning tenant admin still can.
    assert client.post("/agents/worker/suspend", headers=ADMIN).json()["status"] == "Suspended"


# --- 2/6. idempotency-key reuse and delimiter collision -----------------------

def _task(client):
    return client.post("/tasks", json={"agent_id": "worker", "title": "idem"}, headers=ORCH).json()["task_id"]


def _send(client, tid, event_type, key, headers=ORCH, sender="orch"):
    body = {"sender_agent_id": sender, "event_type": event_type, "payload": {"e": event_type}}
    if key is not None:
        body["idempotency_key"] = key
    r = client.post(f"/tasks/{tid}/messages", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_retry_with_same_client_key_returns_original_message(client):
    tid = _task(client)
    first = _send(client, tid, "note", "retry-1")
    again = _send(client, tid, "note", "retry-1")
    assert first["stored"] is True and again["stored"] is False
    assert again["message"]["message_id"] == first["message"]["message_id"]
    # Without a key every POST is a new message (platform key has a fresh id).
    assert _send(client, tid, "note", None)["stored"] is True
    assert _send(client, tid, "note", None)["stored"] is True
    # The same client key is scoped: another event type or sender is new work.
    assert _send(client, tid, "other", "retry-1")["stored"] is True
    assert _send(client, tid, "note", "retry-1", headers=WORKER, sender="worker")["stored"] is True
    assert len(client.get(f"/tasks/{tid}/messages", headers=ADMIN).json()) == 5


def test_delimiter_in_components_does_not_collide(client):
    tid = _task(client)
    a = _send(client, tid, "a:b", "c")
    b = _send(client, tid, "a", "b:c")
    assert a["stored"] is True and b["stored"] is True, (a, b)
    assert a["message"]["idempotency_key"] != b["message"]["idempotency_key"]
    assert len(client.get(f"/tasks/{tid}/messages", headers=ADMIN).json()) == 2
    # Retrying each one is still a no-op.
    assert _send(client, tid, "a:b", "c")["stored"] is False
    assert _send(client, tid, "a", "b:c")["stored"] is False


def test_idempotency_key_encoding_is_injective_and_backward_compatible():
    from app.messaging import idempotency_key

    assert idempotency_key("t", "s", "task", "a:b", "c") != idempotency_key("t", "s", "task", "a", "b:c")
    assert idempotency_key("t", "a:b", "task", "c", "k") != idempotency_key("t", "a", "task", "b:c", "k")
    assert idempotency_key("t", "s", "task", "e", "%3A") != idempotency_key("t", "s", "task", "e", ":")
    # Ordinary ids are unchanged, so keys stored before this change still match.
    assert idempotency_key("tenant-a", "orch", "1b2c-3d", "task.result", "k_1.x") == (
        "tenant-a:orch:1b2c-3d:task.result:k_1.x"
    )


def _legacy_message(tid, event_type, key):
    """A message row as stored before key components were percent-encoded."""
    from app.messaging import legacy_idempotency_key, now_iso

    return {
        "idempotency_key": legacy_idempotency_key("tenant-a", "orch", tid, event_type, key),
        "message_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "task_id": tid,
        "tenant_id": "tenant-a",
        "sender_agent_id": "orch",
        "recipient_agent_id": "worker",
        "event_type": event_type,
        "payload": {"e": event_type},
        "created_at": now_iso(),
    }


def test_retry_of_legacy_idempotency_key_does_not_re_execute(client):
    from app import storage as db
    from app.messaging import idempotency_key

    tid = _task(client)
    old = _legacy_message(tid, "a:b", "k:1")
    assert db.insert_message(old) is True
    again = _send(client, tid, "a:b", "k:1")
    assert again["stored"] is False, again
    assert again["message"]["message_id"] == old["message_id"]
    # Same row, now under the encoded key; no second row was written.
    assert again["message"]["idempotency_key"] == idempotency_key("tenant-a", "orch", tid, "a:b", "k:1")
    assert len(client.get(f"/tasks/{tid}/messages", headers=ADMIN).json()) == 1
    once_more = _send(client, tid, "a:b", "k:1")
    assert once_more["stored"] is False
    assert once_more["message"]["message_id"] == old["message_id"]
    assert len(client.get(f"/tasks/{tid}/messages", headers=ADMIN).json()) == 1


def test_lookup_by_encoded_key_finds_legacy_row():
    from app import storage as db
    from app.messaging import idempotency_key, legacy_form_of_key

    old = _legacy_message("task-legacy", "a:b", "k:1")
    assert db.insert_message(old) is True
    new_key = idempotency_key("tenant-a", "orch", "task-legacy", "a:b", "k:1")
    assert new_key != old["idempotency_key"]
    assert legacy_form_of_key(new_key) == old["idempotency_key"]
    found = db.get_message_by_key(new_key)
    assert found is not None and found["message_id"] == old["message_id"]


# --- 3. audit window anchoring past 1,000 entries -----------------------------

def test_audit_verify_endpoint_past_1000_entries(client, fresh_db):
    from app import observability as audit

    for i in range(1105):
        audit.record("tenant-a", "test.append", f"t-{i}", {"i": i})
    assert client.get("/audit/verify", headers=ADMIN).json() == {"ok": True}
    assert client.get("/audit/verify", headers=ADMIN).json() == {"ok": True}  # repeatable
    full = audit.verify_chain()
    assert full["ok"] is True and full["checked"] == full["head_seq"] > 1000
    # The oldest of the newest 1000 entries points at an excluded predecessor;
    # verification must not depend on that window.
    newest = audit.list_audit(1000)
    assert newest[-1]["prev_hash"] != "GENESIS"
    _sql(fresh_db, "UPDATE audit_log SET detail = ? WHERE seq = ?", (json.dumps({"i": "X"}), 1050))
    assert client.get("/audit/verify", headers=ADMIN).json() == {"ok": False}


# --- 5/7. atomic transitions under concurrency, audit records the CAS target --

def _call_status(task_id, status, auth):
    from app import main

    try:
        main.set_task_status(task_id, main.StatusIn(status=status), auth=auth)
        return True
    except HTTPException as e:
        assert e.status_code in (400, 409), e.detail
        return False


def _race(task_id, targets, auth):
    barrier = threading.Barrier(len(targets))
    outcome = {}

    def go(target):
        barrier.wait()
        outcome[target] = _call_status(task_id, target, auth)

    threads = [threading.Thread(target=go, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcome


ADMIN_AUTH = {"tenant_id": "tenant-a", "agent_id": None, "auth_kind": "tenant"}


def test_concurrent_transitions_one_winner_and_matching_audit(client, fresh_db):
    from app import observability as audit
    from app import orchestration

    for _ in range(ROUNDS):
        tid = _task(client)
        assert _call_status(tid, "working", ADMIN_AUTH)
        outcome = _race(tid, ["completed", "failed"], ADMIN_AUTH)
        winners = [t for t, ok in outcome.items() if ok]
        assert len(winners) == 1, outcome
        assert orchestration.get_task(tid)["status"] == winners[0]
        moves = [(e["detail"]["from"], e["detail"]["to"])
                 for e in _entries(fresh_db, action="task.status", target=tid)]
        assert moves == [("submitted", "working"), ("working", winners[0])], moves
        assert audit.correlate_task("tenant-a", tid)["ok"] is True

    for _ in range(ROUNDS):
        tid = _task(client)
        outcome = _race(tid, ["working", "cancelled"], ADMIN_AUTH)
        final = orchestration.get_task(tid)["status"]
        if outcome["cancelled"]:
            assert final == "cancelled", (outcome, final)
        else:
            assert outcome["working"] and final == "working", (outcome, final)
        moves = {(e["detail"]["from"], e["detail"]["to"])
                 for e in _entries(fresh_db, action="task.status", target=tid)}
        expected = set()
        if outcome["working"]:
            expected.add(("submitted", "working"))
        if outcome["cancelled"]:
            expected.add(("working", "cancelled") if outcome["working"] else ("submitted", "cancelled"))
        assert moves == expected, (outcome, moves)
        assert audit.correlate_task("tenant-a", tid)["ok"] is True


def test_audit_records_cas_target_not_reread_status(client, fresh_db, monkeypatch):
    """Overlap: right after this request's CAS (submitted -> working), another
    request moves the task working -> completed before the row is re-read."""
    from app import storage

    tid = _task(client)
    real = storage.set_task_status_atomic

    def cas_then_overtaken(task_id, from_status, to_status, updated_at):
        moved = real(task_id, from_status, to_status, updated_at)
        if moved and to_status == "working":
            assert real(task_id, "working", "completed", updated_at)
        return moved

    monkeypatch.setattr(storage, "set_task_status_atomic", cas_then_overtaken)
    r = client.post(f"/tasks/{tid}/status", json={"status": "working"}, headers=WORKER)
    assert r.status_code == 200
    (entry,) = _entries(fresh_db, action="task.status", target=tid)
    assert (entry["detail"]["from"], entry["detail"]["to"]) == ("submitted", "working")


def test_correlation_follows_chain_when_audits_land_out_of_order(client):
    from app import observability as audit
    from app import storage
    from app.messaging import now_iso

    tid = _task(client)
    assert storage.set_task_status_atomic(tid, "submitted", "working", now_iso())
    assert storage.set_task_status_atomic(tid, "working", "completed", now_iso())
    principal = {"kind": "tenant", "agent_id": None}
    # Second transition's entry is appended first.
    audit.record("tenant-a", "task.status", tid, {"task_id": tid, "from": "working", "to": "completed", "principal": principal})
    audit.record("tenant-a", "task.status", tid, {"task_id": tid, "from": "submitted", "to": "working", "principal": principal})
    assert audit.correlate_task("tenant-a", tid)["ok"] is True
    storage.set_task_status_atomic(tid, "completed", "failed", now_iso())  # around the API
    assert audit.correlate_task("tenant-a", tid)["ok"] is False


def test_correlation_mixed_old_and_new_status_history(client):
    from app import observability as audit
    from app import storage
    from app.messaging import now_iso

    principal = {"kind": "tenant", "agent_id": None}
    tid = _task(client)
    assert storage.set_task_status_atomic(tid, "submitted", "working", now_iso())
    assert storage.set_task_status_atomic(tid, "working", "completed", now_iso())
    # Old format (no "from") first, then new format.
    audit.record("tenant-a", "task.status", tid, {"task_id": tid, "status": "working"})
    audit.record("tenant-a", "task.status", tid, {"task_id": tid, "from": "working", "to": "completed"})
    assert audit.correlate_task("tenant-a", tid)["ok"] is True

    # Reverse mix: new format first, then old format.
    tid2 = _task(client)
    assert storage.set_task_status_atomic(tid2, "submitted", "working", now_iso())
    assert storage.set_task_status_atomic(tid2, "working", "completed", now_iso())
    audit.record("tenant-a", "task.status", tid2, {"task_id": tid2, "from": "submitted", "to": "working"})
    audit.record("tenant-a", "task.status", tid2, {"task_id": tid2, "status": "completed"})
    assert audit.correlate_task("tenant-a", tid2)["ok"] is True

    # Status changed around the API: still reported.
    assert storage.set_task_status_atomic(tid, "completed", "failed", now_iso())
    assert audit.correlate_task("tenant-a", tid)["ok"] is False


# --- 9. checkpoint-row deletion detection -------------------------------------

def _fill(n):
    from app import observability as audit

    for i in range(n):
        audit.record("tenant-a", "test.append", f"target-{i}", {"i": i})


def test_deleting_only_the_checkpoint_row_is_detected(fresh_db):
    from app import observability as audit

    _fill(30)
    checkpoint = audit.verify_chain()
    _fill(10)
    _sql(fresh_db, "DELETE FROM audit_log WHERE seq = ?", (checkpoint["head_seq"],))
    result = audit.verify_chain(after_seq=checkpoint["head_seq"], anchor_hash=checkpoint["head_hash"])
    assert result["ok"] is False, result
    assert result["checkpoint_seq"] == checkpoint["head_seq"]
    assert audit.verify_chain()["ok"] is False  # the full walk sees it too


def test_rewriting_the_checkpoint_row_is_detected(fresh_db):
    from app import observability as audit

    _fill(30)
    checkpoint = audit.verify_chain()
    _fill(5)
    _sql(fresh_db, "UPDATE audit_log SET detail = ? WHERE seq = ?",
         (json.dumps({"i": "TAMPERED"}), checkpoint["head_seq"]))
    result = audit.verify_chain(after_seq=checkpoint["head_seq"], anchor_hash=checkpoint["head_hash"])
    assert result["ok"] is False and result["broken_at_seq"] == checkpoint["head_seq"], result


def test_intact_checkpoint_still_verifies_incrementally():
    from app import observability as audit

    _fill(30)
    checkpoint = audit.verify_chain()
    _fill(7)
    inc = audit.verify_chain(after_seq=checkpoint["head_seq"], anchor_hash=checkpoint["head_hash"])
    assert inc == {"ok": True, "checked": 7, "head_seq": 37, "head_hash": audit.verify_chain()["head_hash"]}


def test_ops_endpoint_reports_missing_checkpoint(monkeypatch, fresh_db):
    from app.identity import keyhash

    op_key = "op-key-0123456789abcdef-xyz"
    # Hashed form only (B2); the header still presents the plaintext key.
    monkeypatch.setenv("AI_FLEET_OPERATOR_API_KEY", keyhash.hash_key(op_key))
    from app.main import app
    from app import observability as audit

    _fill(12)
    checkpoint = audit.verify_chain()
    _fill(3)
    _sql(fresh_db, "DELETE FROM audit_log WHERE seq = ?", (checkpoint["head_seq"],))
    r = TestClient(app).get(
        "/ops/audit/verify",
        params={"after_seq": checkpoint["head_seq"], "anchor_hash": checkpoint["head_hash"]},
        headers={"X-API-Key": op_key},
    )
    assert r.status_code == 200 and r.json()["ok"] is False, r.text


# --- 10. trace-ID mismatch detection ------------------------------------------

def test_task_trace_changed_after_creation_is_reported(client, fresh_db):
    tid = _task(client)
    assert client.get(f"/tasks/{tid}/correlation", headers=ADMIN).json()["ok"] is True
    _sql(fresh_db, "UPDATE tasks SET trace_id = ? WHERE task_id = ?", ("forged-trace", tid))
    corr = client.get(f"/tasks/{tid}/correlation", headers=ADMIN).json()
    assert corr["ok"] is False, corr
    assert corr["messages_total"] == 0 and corr["status"] == "submitted"
    assert {"task_id": tid, "missing": ["audit.task.trace_id_mismatch"]} in corr["problems"]


# --- 8. legacy tenant migration -----------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE agents (agent_id TEXT PRIMARY KEY, name TEXT NOT NULL, endpoint TEXT NOT NULL,
    capabilities TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'Pending',
    created_at TEXT NOT NULL);
CREATE TABLE tasks (task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'submitted', created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
INSERT INTO agents VALUES ('legacy', 'Legacy', 'http://legacy', '[]', 'Active', 'n');
INSERT INTO tasks VALUES ('t-legacy', 'acme', 'legacy', 'old', 'submitted', 'n', 'n');
"""


def _legacy_db(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    conn.commit()
    conn.close()
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    return db_path


def test_legacy_agents_join_configured_default_tenant(tmp_path, monkeypatch):
    import app.config as config
    from app import storage as db

    _legacy_db(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_TENANT", "acme")
    db.init_db()
    db.init_db()  # idempotent: no second migration
    assert db.get_agent("legacy", tenant_id="acme") is not None
    assert db.get_agent("legacy", tenant_id="default") is None
    assert db.get_task("t-legacy")["tenant_id"] == "acme"

    # Single-key mode: AI_FLEET_API_KEY maps to AI_FLEET_TENANT ("acme").
    monkeypatch.delenv("AI_FLEET_TENANT_API_KEYS", raising=False)
    # Hashed form only (B2); the header still presents the plaintext key.
    from app.identity import keyhash

    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(
        keyhash.hash_key_map({"key-legacy": {"agent_id": "legacy", "tenant_id": "acme"}})))
    from app.main import app

    c = TestClient(app)
    # Present the plaintext single key ("smoke-test-key", see conftest.py):
    # config.API_KEY already holds its hash, which the app would hash again.
    admin = {"X-API-Key": "smoke-test-key"}
    assert [a["agent_id"] for a in c.get("/agents", headers=admin).json()] == ["legacy"]
    # The migrated agent can be assigned new work and authenticate.
    r = c.post("/tasks", json={"agent_id": "legacy", "title": "new"}, headers=admin)
    assert r.status_code == 200, r.text
    assert c.get("/tasks", headers={"X-API-Key": "key-legacy"}).status_code == 200


def test_new_agents_after_migration_keep_their_tenant(tmp_path, monkeypatch):
    import app.config as config
    from app import registry, storage as db

    _legacy_db(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_TENANT", "acme")
    db.init_db()
    registry.register_agent("tenant-z", "fresh", "F", "http://f", [])
    db.init_db()
    assert db.get_agent("fresh", tenant_id="tenant-z") is not None
    assert db.get_agent("legacy", tenant_id="acme") is not None


def test_migration_fails_closed_on_invalid_default_tenant(tmp_path, monkeypatch):
    import app.config as config
    from app import storage as db

    db_path = _legacy_db(tmp_path)
    monkeypatch.setattr(config, "DEFAULT_TENANT", "bad:tenant")
    with pytest.raises(RuntimeError):
        db.init_db()
    conn = sqlite3.connect(str(db_path))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
    conn.close()
    assert "tenant_id" not in cols  # nothing half-migrated

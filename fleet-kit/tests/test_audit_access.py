"""Audit access scope and reserved system actors (review items P1-2, P1-3).

P1-2: /audit/verify was open to any key, agent keys included, and returned
the platform-wide entry count (checked/head_seq), revealing other tenants'
activity volume; each call also scans the whole log. Now: tenant admins
get {"ok"} only, agent keys get 403, details are operator-only (/ops/...).

P1-3: unauthenticated denials were filed under actor "unauthenticated",
and GET /audit filters by actor = tenant id, so a tenant configured with
that id read them all (source IPs included). Now system actors use the
"sys:" prefix and tenant ids cannot contain ":"; a key with a malformed
tenant id is refused and the startup check reports it.

Run:  python -m pytest tests/test_audit_access.py -q
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

OPERATOR_KEY = "op-key-0123456789abcdef-xyz"
TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
AGENT_KEYS = {"key-worker": {"agent_id": "worker", "tenant_id": "tenant-a"}}
ADMIN = {"X-API-Key": "key-a"}
WORKER = {"X-API-Key": "key-worker"}
OP = {"X-API-Key": OPERATOR_KEY}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "access.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(TENANT_KEYS))
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(AGENT_KEYS))
    monkeypatch.setenv("AI_FLEET_OPERATOR_API_KEY", OPERATOR_KEY)
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield db_path


@pytest.fixture
def client():
    from app.main import app

    c = TestClient(app)
    r = c.post("/agents", json={"agent_id": "worker", "name": "w", "endpoint": "http://w"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert c.post("/agents/worker/approve", headers=ADMIN).status_code == 200
    return c


# ------------------------------------------------------------ P1-2 verify scope


def test_agent_key_cannot_verify(client):
    assert client.get("/audit/verify", headers=WORKER).status_code == 403


def test_tenant_admin_gets_ok_only(client):
    assert client.get("/audit/verify", headers=ADMIN).json() == {"ok": True}


def test_operator_endpoints_reject_other_keys(client):
    for headers in (ADMIN, WORKER, {"X-API-Key": "nope"}, {}):
        assert client.get("/ops/audit/verify", headers=headers).status_code == 401
        assert client.get("/ops/audit", headers=headers).status_code == 401


def test_operator_key_does_not_pass_tenant_endpoints(client):
    assert client.get("/tasks", headers=OP).status_code == 401
    assert client.get("/audit/verify", headers=OP).status_code == 401


def test_operator_disabled_when_unset_short_or_reused(client, monkeypatch):
    monkeypatch.delenv("AI_FLEET_OPERATOR_API_KEY")
    assert client.get("/ops/audit", headers=OP).status_code == 401
    monkeypatch.setenv("AI_FLEET_OPERATOR_API_KEY", "short")
    assert client.get("/ops/audit", headers={"X-API-Key": "short"}).status_code == 401
    monkeypatch.setenv("AI_FLEET_OPERATOR_API_KEY", "key-a")  # reuses a tenant key
    assert client.get("/ops/audit", headers=ADMIN).status_code == 401
    from app import identity

    problems = identity.validate_key_config()
    assert any("must differ" in p for p in problems)
    assert any("at least" in p for p in problems)
    assert all("key-a" not in p for p in problems)  # key values never in messages


def test_operator_verify_returns_details(client):
    body = client.get("/ops/audit/verify", headers=OP).json()
    assert body["ok"] is True and body["checked"] >= 2 and "head_hash" in body


# ------------------------------------------------------------ P1-3 reserved actors


def test_unauthenticated_denials_use_reserved_actor(client, fresh_db):
    assert client.get("/tasks", headers={"X-API-Key": "bad"}).status_code == 401
    entries = client.get("/ops/audit", params={"actor": "sys:unauthenticated"}, headers=OP).json()
    assert len(entries) == 1
    assert entries[0]["action"] == "request.denied"
    assert entries[0]["detail"]["source"] == "testclient"
    # No tenant sees them.
    for headers in (ADMIN, {"X-API-Key": "key-b"}):
        actions = [e["actor"] for e in client.get("/audit", headers=headers).json()]
        assert "sys:unauthenticated" not in actions


@pytest.mark.parametrize("bad_tenant", ["sys:unauthenticated", "unauth:x", "", " a", "a/b", "x" * 65])
def test_tenant_key_with_malformed_tenant_id_is_refused(client, monkeypatch, bad_tenant):
    keys = dict(TENANT_KEYS, **{"key-evil": bad_tenant})
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(keys))
    assert client.get("/audit", headers={"X-API-Key": "key-evil"}).status_code == 401
    from app import identity

    assert any("invalid tenant id" in p for p in identity.validate_key_config())


def test_agent_key_with_malformed_tenant_id_is_refused(client, monkeypatch, fresh_db):
    keys = dict(AGENT_KEYS, **{"key-evil-agent": {"agent_id": "worker", "tenant_id": "sys:operator"}})
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(keys))
    assert client.get("/tasks", headers={"X-API-Key": "key-evil-agent"}).status_code == 401
    # Filed as unauthenticated, never under the malformed (system) actor.
    conn = sqlite3.connect(str(fresh_db))
    actors = {r[0] for r in conn.execute("SELECT actor FROM audit_log WHERE action = 'request.denied'")}
    conn.close()
    assert actors == {"sys:unauthenticated"}


def test_previous_unauthenticated_tenant_name_is_now_just_a_tenant(client, monkeypatch):
    """A tenant may still be called "unauthenticated" (no colon), but it no
    longer collides with the system actor and sees nothing it should not."""
    keys = dict(TENANT_KEYS, **{"key-u": "unauthenticated"})
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(keys))
    assert client.get("/tasks", headers={"X-API-Key": "bad"}).status_code == 401
    seen = client.get("/audit", headers={"X-API-Key": "key-u"}).json()
    assert seen == []


def test_default_tenant_is_validated(monkeypatch):
    import app.config as config
    from app import identity

    monkeypatch.setattr(config, "DEFAULT_TENANT", "sys:default")
    assert any("AI_FLEET_TENANT" in p for p in identity.validate_key_config())


def test_startup_refuses_bad_configuration(monkeypatch):
    from app import main

    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps({"k": "sys:x"}))
    with pytest.raises(RuntimeError):
        main.startup()


# ------------------------------------------------------------ paging bounds


def test_audit_limit_is_bounded(client):
    assert client.get("/audit", params={"limit": 0}, headers=ADMIN).status_code == 422
    assert client.get("/audit", params={"limit": 1001}, headers=ADMIN).status_code == 422
    assert client.get("/audit", params={"limit": 1000}, headers=ADMIN).status_code == 200
    assert client.get("/ops/audit", params={"limit": 1001}, headers=OP).status_code == 422


# ------------------------------------------------------------ indexes


@pytest.mark.parametrize(
    "sql,params,index",
    [
        ("SELECT * FROM audit_log WHERE actor = ? ORDER BY seq DESC LIMIT ?", ("t", 100), "idx_audit_actor_seq"),
        ("SELECT * FROM audit_log WHERE actor = ? AND target = ? ORDER BY seq ASC", ("t", "x"), "idx_audit_actor_target_seq"),
        ("SELECT * FROM messages WHERE task_id = ? ORDER BY created_at", ("x",), "idx_messages_task_created"),
        ("SELECT * FROM messages WHERE message_id = ?", ("x",), "idx_messages_message_id"),
        ("SELECT * FROM tasks WHERE tenant_id = ? ORDER BY created_at", ("t",), "idx_tasks_tenant_created"),
    ],
)
def test_query_paths_use_indexes(fresh_db, sql, params, index):
    conn = sqlite3.connect(str(fresh_db))
    plan = " ".join(str(r) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())
    conn.close()
    assert index in plan, plan

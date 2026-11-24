"""Denial-flood protection: refused requests cannot grow the audit log at
request rate (follow-up to T09, which audited every refused request).

Part 1 tests the limiter alone with a fake clock; part 2 goes through HTTP.

Run:  python -m pytest tests/test_denial_flood.py -q
"""
import json
import os
import sqlite3
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from app.observability.denials import OVERFLOW_SOURCE, DenialLimiter

UNAUTH = {"kind": None, "agent_id": None}
TENANT_B = {"kind": "tenant", "agent_id": None}


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _limiter(clock, **overrides):
    args = dict(window_seconds=60, per_source=1, per_principal=5, max_sources=3, max_individual=100)
    args.update(overrides)
    return DenialLimiter(clock=clock, **args)


def _unauth(lim, source="1.2.3.4", status=401, route="/tasks"):
    return lim.decide(actor="unauthenticated", principal=UNAUTH, source=source, status=status, route=route)


# ---------------------------------------------------------------- limiter


def test_source_budget_then_summary_on_next_window():
    clock = FakeClock()
    lim = _limiter(clock)
    writes = [_unauth(lim)[0] for _ in range(1000)]
    assert writes.count(True) == 1
    clock.t += 60
    write, summaries = _unauth(lim)
    assert write is True  # new window, new budget
    assert len(summaries) == 1
    s = summaries[0]
    assert s["actor"] == "unauthenticated"
    d = s["detail"]
    assert d["suppressed"] == 999 and d["logged_individually"] == 1
    assert d["statuses"] == {"401": 999} and d["routes"] == {"/tasks": 999}
    assert d["source"] == "1.2.3.4" and d["overflow"] is False and d["flushed_early"] is False


def test_quiet_window_writes_no_summary():
    clock = FakeClock()
    lim = _limiter(clock)
    assert _unauth(lim)[0] is True
    clock.t += 60
    assert lim.flush() == []


def test_flush_without_new_denials():
    clock = FakeClock()
    lim = _limiter(clock)
    for _ in range(4):
        _unauth(lim)
    assert lim.flush() == []  # window still open
    clock.t += 61
    summaries = lim.flush()
    assert len(summaries) == 1 and summaries[0]["detail"]["suppressed"] == 3
    assert lim.flush() == []  # flushed once


def test_source_cap_sends_new_sources_to_overflow():
    clock = FakeClock()
    lim = _limiter(clock, max_sources=3)
    writes = 0
    for i in range(10):
        for _ in range(2):
            writes += _unauth(lim, source=f"10.0.0.{i}")[0]
    assert writes == 3  # only the first 3 sources get an individual entry
    clock.t += 60
    summaries = lim.flush()
    by_source = {s["detail"]["source"]: s["detail"] for s in summaries}
    assert len(summaries) == 4
    assert by_source[OVERFLOW_SOURCE]["suppressed"] == 14
    assert by_source[OVERFLOW_SOURCE]["overflow"] is True
    assert by_source[OVERFLOW_SOURCE]["logged_individually"] == 0


def test_bound_holds_with_rotating_sources():
    """Spoofed/rotated sources cannot raise the number of entries."""
    clock = FakeClock()
    lim = _limiter(clock, max_sources=20, per_source=1)
    writes = sum(_unauth(lim, source=f"s{i}")[0] for i in range(10_000))
    clock.t += 60
    summaries = lim.flush()
    assert writes == 20
    assert len(summaries) <= 20 + 1
    assert writes + sum(s["detail"]["suppressed"] for s in summaries) == 10_000


def test_principal_budget_and_sources_sample():
    clock = FakeClock()
    lim = _limiter(clock)
    writes = sum(
        lim.decide(actor="tenant-b", principal=TENANT_B, source=f"h{i % 8}", status=404, route="/tasks/{task_id}")[0]
        for i in range(20)
    )
    assert writes == 5
    clock.t += 60
    (s,) = lim.flush()
    assert s["actor"] == "tenant-b"
    d = s["detail"]
    assert d["suppressed"] == 15 and d["statuses"] == {"404": 15}
    assert len(d["sources_sample"]) == 5 and "source" not in d


def test_global_individual_cap():
    clock = FakeClock()
    lim = _limiter(clock, per_principal=5, max_individual=7)
    writes = 0
    for agent in ("a", "b", "c"):
        for _ in range(5):
            writes += lim.decide(actor="t", principal={"kind": "agent", "agent_id": agent},
                                 source="x", status=403, route="/r")[0]
    assert writes == 7


def test_force_flush_closes_current_window():
    clock = FakeClock()
    lim = _limiter(clock)
    for _ in range(3):
        _unauth(lim)
    (s,) = lim.flush(force=True)
    assert s["detail"]["suppressed"] == 2 and s["detail"]["flushed_early"] is True
    assert lim.flush(force=True) == []


def test_concurrent_decisions_are_exact():
    clock = FakeClock()
    lim = _limiter(clock)
    results = []
    lock = threading.Lock()

    def worker():
        local = [_unauth(lim)[0] for _ in range(500)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    clock.t += 60
    (s,) = lim.flush()
    assert s["detail"]["suppressed"] == 3999


def test_invalid_settings_rejected():
    with pytest.raises(ValueError):
        DenialLimiter(window_seconds=0)
    for name in ("per_source", "per_principal", "max_sources", "max_individual"):
        with pytest.raises(ValueError):
            DenialLimiter(**{name: -1})


# ---------------------------------------------------------------- HTTP

TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
AGENT_KEYS = {"key-worker": {"agent_id": "worker", "tenant_id": "tenant-a"}}
ADMIN = {"X-API-Key": "key-a"}
ADMIN_B = {"X-API-Key": "key-b"}


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "flood.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(TENANT_KEYS))
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(AGENT_KEYS))
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield db_path


@pytest.fixture
def clock():
    from app import main

    fake = FakeClock()
    main.denial_limiter.clock = fake  # restored by conftest
    return fake


@pytest.fixture
def client(fresh_db, clock):
    from app.main import app

    c = TestClient(app)
    r = c.post("/agents", json={"agent_id": "worker", "name": "w", "endpoint": "http://w"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert c.post("/agents/worker/approve", headers=ADMIN).status_code == 200
    return c


def _rows(db_path, **where):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()]
    conn.close()
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return [r for r in rows if all(r.get(k) == v for k, v in where.items())]


def test_unauthenticated_flood_is_bounded(client, fresh_db, clock):
    total_before = len(_rows(fresh_db))
    for i in range(300):
        assert client.get("/tasks", headers={"X-API-Key": f"bad-key-{i}"}).status_code == 401
    denied = _rows(fresh_db, action="request.denied")
    assert len(denied) == 1
    assert denied[0]["actor"] == "unauthenticated"
    assert denied[0]["detail"]["source"] == "testclient"
    assert len(_rows(fresh_db)) == total_before + 1

    clock.t += 60
    verify = client.get("/audit/verify", headers=ADMIN).json()  # flushes, then verifies
    assert verify["ok"] is True
    (summary,) = _rows(fresh_db, action="request.denied.summary")
    d = summary["detail"]
    assert summary["actor"] == "unauthenticated"
    assert d["suppressed"] == 299 and d["statuses"] == {"401": 299}
    assert d["routes"] == {"/tasks": 299}
    assert "bad-key-" not in json.dumps(_rows(fresh_db))


def test_tenant_sees_its_own_summary(client, fresh_db, clock):
    task = client.post("/tasks", json={"agent_id": "worker", "title": "x"}, headers=ADMIN).json()
    for _ in range(20):
        assert client.get(f"/tasks/{task['task_id']}", headers=ADMIN_B).status_code == 404
    seen = client.get("/audit", headers=ADMIN_B).json()
    assert [e["action"] for e in seen] == ["request.denied"] * 5
    clock.t += 60
    seen = client.get("/audit", params={"limit": 50}, headers=ADMIN_B).json()
    summaries = [e for e in seen if e["action"] == "request.denied.summary"]
    assert len(summaries) == 1
    assert summaries[0]["detail"]["suppressed"] == 15
    assert summaries[0]["detail"]["routes"] == {"/tasks/{task_id}": 15}
    assert summaries[0]["detail"]["principal"] == {"kind": "tenant", "agent_id": None}
    a_actions = {e["action"] for e in client.get("/audit", params={"limit": 50}, headers=ADMIN).json()}
    assert "request.denied" not in a_actions and "request.denied.summary" not in a_actions


def test_long_path_is_clipped(client, fresh_db):
    long_id = "x" * 5000
    assert client.get(f"/tasks/{long_id}", headers={"X-API-Key": "bad"}).status_code == 401
    (entry,) = _rows(fresh_db, action="request.denied")
    assert len(entry["target"]) <= 256
    assert len(entry["detail"]["path"]) <= 256
    assert entry["detail"]["path_truncated"] is True


def test_shutdown_flushes_open_window(client, fresh_db):
    from app import main

    for i in range(5):
        client.get("/tasks", headers={"X-API-Key": f"bad-{i}"})
    main.shutdown()
    (summary,) = _rows(fresh_db, action="request.denied.summary")
    assert summary["detail"]["suppressed"] == 4
    assert summary["detail"]["flushed_early"] is True
    from app import observability as audit

    assert audit.verify_chain()["ok"] is True


def test_correlation_unaffected_by_denial_entries(client, fresh_db, clock):
    task = client.post("/tasks", json={"agent_id": "worker", "title": "y"}, headers=ADMIN).json()
    for _ in range(10):
        client.get(f"/tasks/{task['task_id']}", headers=ADMIN_B)
    clock.t += 60
    client.get("/audit", headers=ADMIN_B)  # writes the summary
    corr = client.get(f"/tasks/{task['task_id']}/correlation", headers=ADMIN).json()
    assert corr["ok"] is True

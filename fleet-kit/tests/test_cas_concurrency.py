"""Task status transitions are compare-and-set (PR #1 follow-up).

Codex's scenario: two requests read the same status and both pass the
transition check, then their unconditional writes land in either order
(e.g. a task left "working" after a successful cancel). The CAS update
(UPDATE ... WHERE status = <the status we read>) makes the loser fail.

Run:  python -m pytest tests/test_cas_concurrency.py -q
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.identity.keyhash import hash_key_map

ROUNDS = 40


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cas.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    from app import registry

    registry.register_agent("tenant-a", "agent-a", "A", "http://a", [])
    registry.approve_agent("tenant-a", "agent-a")
    yield


def _new_task(status="submitted"):
    from app import orchestration

    task = orchestration.create_task("tenant-a", "agent-a", "cas")
    if status == "working":
        orchestration.set_status(task["task_id"], "working")
    return task["task_id"]


def test_stale_conditional_write_is_rejected():
    """Deterministic: a write conditioned on a status the task no longer
    has changes nothing."""
    from app import orchestration
    from app import storage as db
    from app.messaging import now_iso

    task_id = _new_task()
    orchestration.set_status(task_id, "cancelled")
    assert db.set_task_status_atomic(task_id, "submitted", "working", now_iso()) is False
    assert orchestration.get_task(task_id)["status"] == "cancelled"


def _race(task_id, targets):
    from app import orchestration

    barrier = threading.Barrier(len(targets))
    outcome = {}

    def go(target):
        barrier.wait()
        try:
            orchestration.set_status(task_id, target)
            outcome[target] = True
        except ValueError:  # includes TransitionConflict
            outcome[target] = False

    threads = [threading.Thread(target=go, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcome


def test_concurrent_terminal_transitions_exactly_one_wins():
    """From working, completed and failed are both legal but mutually
    exclusive: exactly one may succeed, and the stored status is its."""
    from app import orchestration

    for _ in range(ROUNDS):
        task_id = _new_task("working")
        outcome = _race(task_id, ["completed", "failed"])
        winners = [t for t, ok in outcome.items() if ok]
        assert len(winners) == 1, outcome
        assert orchestration.get_task(task_id)["status"] == winners[0]


def test_successful_cancel_is_never_overwritten():
    """working vs cancelled from submitted: both may succeed only in the
    serial order submitted -> working -> cancelled. Whenever the cancel
    succeeded, the task must end cancelled."""
    from app import orchestration

    for _ in range(ROUNDS):
        task_id = _new_task()
        outcome = _race(task_id, ["working", "cancelled"])
        final = orchestration.get_task(task_id)["status"]
        if outcome["cancelled"]:
            assert final == "cancelled", (outcome, final)
        else:
            assert outcome["working"] and final == "working", (outcome, final)


def test_lost_race_is_http_409(monkeypatch):
    """A lost compare-and-set surfaces as 409 Conflict, not 400."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(hash_key_map({"key-a": "tenant-a"})))
    from app.main import app
    from app import storage

    client = TestClient(app)
    task_id = _new_task()
    monkeypatch.setattr(storage, "set_task_status_atomic", lambda *a, **k: False)
    r = client.post(
        f"/tasks/{task_id}/status", json={"status": "working"}, headers={"X-API-Key": "key-a"}
    )
    assert r.status_code == 409, r.text

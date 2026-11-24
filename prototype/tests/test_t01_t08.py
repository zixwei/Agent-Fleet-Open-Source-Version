"""Regression tests for the Stage 0 baseline blockers found by Cue.

T01 (two-way message round-trip): a reply from the task's agent must be
routable back to the other party. Previously the recipient was hardcoded to
the task's agent, so every reply went to the sender itself (100/100 FAIL).

T08 (cross-tenant isolation): a task must be visible only to the tenant
that owns it. Previously GET /tasks/{id} (and status/message endpoints)
returned any task to any authenticated caller, leaking another tenant's
task with HTTP 200.

Direction matters (Cue #ddjklg, independent review of e70289cd): the
owner is tenant B and the attacker is tenant A with a valid tenant-A
key. Tenant B creates a synthetic Task and pre-seeds one Message; tenant
A then probes Task read, Message read, status change and message write.
Each probe must be 403/404, leak no tenant-B data, and leave the owner's
task status and message count unchanged. 20 rounds, 80 sub-probes; the
run stops at the first leak or unauthorised change. Results are written
to tests/results/t08_corrected_2026-11-24.json.

Run:  python -m pytest tests/test_t01_t08.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

os.environ["AI_FLEET_DB"] = "sqlite:////tmp/ai_fleet_t01_t08.db"

TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
HEADERS_A = {"X-API-Key": "key-a"}
HEADERS_B = {"X-API-Key": "key-b"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "t01_t08.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(TENANT_KEYS))
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


def test_t01_reply_without_recipient_goes_to_other_party():
    """Implicit routing: B replying with no recipient field goes to A.

    The acceptance runner sends replies without recipient_agent_id, so
    the hub must route a reply from the task's agent back to the other
    party (the requester), not to the task's agent itself.
    """
    from app.main import app

    client = TestClient(app)
    _make_agent(client, "agent-a", HEADERS_A)
    _make_agent(client, "agent-b", HEADERS_A)
    r = client.post("/tasks", json={"agent_id": "agent-b", "title": "implicit"}, headers=HEADERS_A)
    task_id = r.json()["task_id"]
    # A -> B with no recipient field: recipient is the task's agent.
    r = client.post(
        f"/tasks/{task_id}/messages",
        json={"sender_agent_id": "agent-a", "event_type": "task.request", "payload": {}},
        headers=HEADERS_A,
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"]["recipient_agent_id"] == "agent-b"
    # B replies 10 times with no recipient field: every reply goes to A.
    for _ in range(10):
        r = client.post(
            f"/tasks/{task_id}/messages",
            json={"sender_agent_id": "agent-b", "event_type": "task.reply", "payload": {}},
            headers=HEADERS_A,
        )
        assert r.status_code == 200, r.text
        assert r.json()["message"]["recipient_agent_id"] == "agent-a"

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


T08_ROUNDS = 20
T08_RESULTS_PATH = os.path.join(
    os.path.dirname(__file__), "results", "t08_corrected_2026-11-24.json"
)


def _t08_probe(client, method, url, headers, **kwargs):
    """One cross-tenant probe: returns (status, body_text)."""
    r = getattr(client, method)(url, headers=headers, **kwargs)
    return r.status_code, r.text


def test_t08_cross_tenant_task_isolation():
    """Correct direction (Cue #ddjklg): tenant B owns, tenant A attacks.

    Tenant B creates a synthetic Task and pre-seeds one Message; tenant A,
    with a valid tenant-A key, probes Task read, Message read, status
    change and message write. Every probe must be 403/404, leak no
    tenant-B data, and leave the owner's task status and message count
    unchanged. 20 rounds, 80 sub-probes; stop at the first leak or
    unauthorised change.
    """
    from app.main import app

    client = TestClient(app)
    _make_agent(client, "agent-a", HEADERS_A)
    _make_agent(client, "agent-b", HEADERS_B)

    records = []
    stop_reason = None
    try:
        for run in range(1, T08_ROUNDS + 1):
            title_marker = f"tenant-b-secret-{run}"
            payload_marker = f"tenant-b-secret-payload-{run}"

            # Tenant B owns: create the Task and pre-seed one Message.
            r = client.post(
                "/tasks",
                json={"agent_id": "agent-b", "title": title_marker},
                headers=HEADERS_B,
            )
            assert r.status_code == 200, r.text
            task_id = r.json()["task_id"]
            r = client.post(
                f"/tasks/{task_id}/messages",
                json={"sender_agent_id": "agent-b", "recipient_agent_id": "agent-b",
                      "event_type": "task.note", "payload": {"note": payload_marker}},
                headers=HEADERS_B,
            )
            assert r.status_code == 200, r.text
            message_id = r.json()["message"]["message_id"]

            # Owner state before the probes.
            before = client.get(f"/tasks/{task_id}", headers=HEADERS_B).json()
            assert before["status"] == "submitted"
            before_count = len(client.get(f"/tasks/{task_id}/messages", headers=HEADERS_B).json())
            assert before_count == 1

            # Tenant A attacks with a valid tenant-A key: 4 probes.
            probes = []
            probe_calls = [
                ("task_read", "get", f"/tasks/{task_id}", {}),
                ("message_read", "get", f"/tasks/{task_id}/messages", {}),
                ("status_change", "post", f"/tasks/{task_id}/status",
                 {"json": {"status": "working"}}),
                ("message_write", "post", f"/tasks/{task_id}/messages",
                 {"json": {"sender_agent_id": "agent-a", "event_type": "task.note",
                           "payload": {}}}),
            ]
            for name, method, url, kwargs in probe_calls:
                status, body = _t08_probe(client, method, url, HEADERS_A, **kwargs)
                safe = status in (403, 404)
                no_leak = title_marker not in body and payload_marker not in body
                probes.append({
                    "probe": name,
                    "http_status": status,
                    "response_body": body,
                    "safe_status": safe,
                    "no_tenant_b_data_in_body": no_leak,
                })
                if not (safe and no_leak):
                    stop_reason = (
                        f"run {run} probe {name}: status {status}, "
                        f"leak={not no_leak}, body={body[:200]}"
                    )
                    records.append({
                        "run": run, "task_id": task_id, "message_id": message_id,
                        "probes": probes,
                        "owner_state_before": {"status": "submitted", "message_count": 1},
                        "owner_state_after": None,
                        "no_state_or_message_mutation": False,
                        "status": "FAIL",
                    })
                    pytest.fail(stop_reason)

            # Owner state after the probes: unchanged.
            after = client.get(f"/tasks/{task_id}", headers=HEADERS_B).json()
            after_count = len(client.get(f"/tasks/{task_id}/messages", headers=HEADERS_B).json())
            unchanged = after["status"] == "submitted" and after_count == 1
            records.append({
                "run": run,
                "task_id": task_id,
                "message_id": message_id,
                "probes": probes,
                "owner_state_before": {"status": "submitted", "message_count": 1},
                "owner_state_after": {"status": after["status"], "message_count": after_count},
                "no_state_or_message_mutation": unchanged,
                "status": "PASS" if unchanged else "FAIL",
            })
            if not unchanged:
                stop_reason = f"run {run}: owner state mutated after probes"
                pytest.fail(stop_reason)
    finally:
        passed = sum(1 for rec in records if rec["status"] == "PASS")
        rejected = sum(
            1 for rec in records for p in rec["probes"]
            if p["safe_status"] and p["no_tenant_b_data_in_body"]
        )
        result = {
            "test": "corrected T08 tenant isolation (tenant-B owner, tenant-A attacker)",
            "commit": os.environ.get("AI_FLEET_T08_COMMIT", "uncommitted"),
            "target_runs": T08_ROUNDS,
            "executed_runs": len(records),
            "scenario_passes": passed,
            "sub_probes_rejected": rejected,
            "verdict": "PASS" if passed == T08_ROUNDS and stop_reason is None else "FAIL",
            "stop_reason": stop_reason,
            "direction": "tenant B created each synthetic task and message; valid tenant A key performed probes",
            "probe_types": ["task_read", "message_read", "status_change", "message_write"],
            "executed_at_utc": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
            "records": records,
        }
        os.makedirs(os.path.dirname(T08_RESULTS_PATH), exist_ok=True)
        with open(T08_RESULTS_PATH, "w") as f:
            json.dump(result, f, indent=2)

    assert len(records) == T08_ROUNDS
    assert all(rec["status"] == "PASS" for rec in records)

    # List and audit are tenant-scoped: A sees none of B's tasks, and no
    # audit entry visible to A references any of B's task ids.
    assert client.get("/tasks", headers=HEADERS_A).json() == []
    assert len(client.get("/tasks", headers=HEADERS_B).json()) == T08_ROUNDS
    b_task_ids = {rec["task_id"] for rec in records}
    a_audit = client.get("/audit", headers=HEADERS_A).json()
    assert all(
        not any(tid in json.dumps(entry) for tid in b_task_ids) for entry in a_audit
    )
    assert len(client.get("/audit", headers=HEADERS_B).json()) > 0

    # Owner still fully works after the denied attempts.
    last_task = records[-1]["task_id"]
    r = client.post(f"/tasks/{last_task}/status", json={"status": "working"}, headers=HEADERS_B)
    assert r.status_code == 200
    assert r.json()["status"] == "working"

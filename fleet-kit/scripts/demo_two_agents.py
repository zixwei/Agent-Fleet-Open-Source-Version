#!/usr/bin/env python3
"""No-model green path: two agent identities, one task, message, receipt, audit.

Exit 0 only when S1/S2-style checks pass. Does not call any LLM API.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

HUB = os.environ.get("FLEET_HUB_URL", "http://127.0.0.1:8000").rstrip("/")
TENANT_KEY = os.environ["FLEET_API_KEY"]
AGENT_A_KEY = os.environ["FLEET_AGENT_A_KEY"]
AGENT_B_KEY = os.environ["FLEET_AGENT_B_KEY"]
OTHER_TENANT_KEY = os.environ.get("FLEET_OTHER_TENANT_KEY", "")


def req(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, dict | list | str]:
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(
        f"{HUB}{path}",
        data=data,
        method=method,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return e.code, parsed


def must(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: {msg}")


def main() -> None:
    code, health = req("GET", "/health", TENANT_KEY)
    must(code == 200 and health.get("status") == "ok", f"health {code} {health}")

    for agent_id, name in (("orchestrator", "Orchestrator"), ("worker", "Worker")):
        code, _ = req(
            "POST",
            "/agents",
            TENANT_KEY,
            {
                "agent_id": agent_id,
                "name": name,
                "endpoint": f"http://{agent_id}.local",
                "capabilities": ["demo"],
            },
        )
        must(code == 200, f"register {agent_id}")
        code, body = req("POST", f"/agents/{agent_id}/approve", TENANT_KEY)
        must(code == 200 and body.get("status") == "Active", f"approve {agent_id}")

    # Agent keys cannot do admin operations (register/approve/suspend).
    code, _ = req("POST", "/agents/orchestrator/suspend", AGENT_B_KEY)
    must(code == 403, f"worker key cannot suspend another agent (got {code})")

    # The orchestrator creates the task with its own agent key.
    code, task = req(
        "POST",
        "/tasks",
        AGENT_A_KEY,
        {"agent_id": "worker", "title": "fleet-kit demo: summarize ok"},
    )
    must(code == 200 and task.get("created_by_agent_id") == "orchestrator", f"create_task {task}")
    task_id = task["task_id"]
    print(f"task_id={task_id}")

    code, sent = req(
        "POST",
        f"/tasks/{task_id}/messages",
        AGENT_A_KEY,
        {
            "sender_agent_id": "orchestrator",
            "recipient_agent_id": "worker",
            "event_type": "task.assign",
            "payload": {"instruction": "reply with result ok"},
        },
    )
    must(code == 200 and sent.get("stored") is True, f"post_message {sent}")
    message = sent["message"]
    message_id = message["message_id"]
    must(message.get("protocol") == "fleet", f"protocol is fleet not a2a ({message.get('protocol')})")
    must(message["sender_agent_id"] != message["recipient_agent_id"], "two different agent ids on message")

    # Client retry with a stable idempotency_key: the second POST is a no-op.
    retry_body = {
        "sender_agent_id": "orchestrator",
        "recipient_agent_id": "worker",
        "event_type": "task.note",
        "payload": {"note": "retry-safe"},
        "idempotency_key": f"{task_id}:note:1",
    }
    code, first = req("POST", f"/tasks/{task_id}/messages", AGENT_A_KEY, retry_body)
    must(code == 200 and first.get("stored") is True, f"keyed message {first}")
    code, again = req("POST", f"/tasks/{task_id}/messages", AGENT_A_KEY, retry_body)
    must(
        code == 200 and again.get("stored") is False
        and again["message"]["message_id"] == first["message"]["message_id"],
        f"retry with same idempotency_key is a no-op {again}",
    )

    code, ack = req(
        "POST",
        f"/tasks/{task_id}/messages/{message_id}/receipt",
        AGENT_B_KEY,
        {},
    )
    must(code == 200 and ack.get("stored") is True, f"receipt {ack}")
    code, ack2 = req(
        "POST",
        f"/tasks/{task_id}/messages/{message_id}/receipt",
        AGENT_B_KEY,
        {},
    )
    must(code == 200 and ack2.get("stored") is False, f"duplicate receipt no-op {ack2}")

    code, reply = req(
        "POST",
        f"/tasks/{task_id}/messages",
        AGENT_B_KEY,
        {
            "sender_agent_id": "worker",
            "recipient_agent_id": "orchestrator",
            "event_type": "task.result",
            "payload": {"result": "ok"},
        },
    )
    must(code == 200 and reply.get("stored") is True, f"worker reply {reply}")
    reply_id = reply["message"]["message_id"]
    code, _ = req(
        "POST",
        f"/tasks/{task_id}/messages/{reply_id}/receipt",
        AGENT_A_KEY,
        {},
    )
    must(code == 200, "orchestrator receipts reply")

    # Only the assignee (worker) moves the task; the orchestrator is refused.
    code, _ = req("POST", f"/tasks/{task_id}/status", AGENT_A_KEY, {"status": "working"})
    must(code == 403, f"non-assignee cannot move the task (got {code})")
    code, _ = req("POST", f"/tasks/{task_id}/status", AGENT_B_KEY, {"status": "working"})
    must(code == 200, "status working (assignee)")
    code, done = req("POST", f"/tasks/{task_id}/status", AGENT_B_KEY, {"status": "completed"})
    must(code == 200 and done.get("status") == "completed", f"status completed {done}")

    code, task_view = req("GET", f"/tasks/{task_id}", TENANT_KEY)
    must(code == 200 and task_view.get("status") == "completed", "get_task completed")

    code, msgs = req("GET", f"/tasks/{task_id}/messages", TENANT_KEY)
    # assign + keyed note (once, despite the retry) + result = 3
    must(code == 200 and isinstance(msgs, list) and len(msgs) == 3, f"list_messages n={len(msgs) if isinstance(msgs, list) else msgs}")

    code, verify = req("GET", "/audit/verify", TENANT_KEY)
    must(code == 200 and verify.get("ok") is True, f"audit verify (whole chain) {verify}")

    if OTHER_TENANT_KEY:
        code, cross = req("GET", f"/tasks/{task_id}", OTHER_TENANT_KEY)
        must(code == 404, f"cross-tenant 404 got {code} {cross}")
    else:
        print("SKIP: cross-tenant (set FLEET_OTHER_TENANT_KEY to enable)")

    # MCP facade parity: same task via HTTP tools the MCP server wraps
    # (stdio MCP host attach is documented separately).
    print(
        json.dumps(
            {
                "demo": "fleet-kit-1.0",
                "orchestrator_agent_id": "orchestrator",
                "worker_agent_id": "worker",
                "task_id": task_id,
                "status": "completed",
                "protocol": "fleet",
                "audit_ok": True,
            }
        )
    )
    print("PASS")


if __name__ == "__main__":
    # Wait briefly for hub if just started
    for _ in range(30):
        try:
            code, _ = req("GET", "/health", TENANT_KEY)
            if code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        print("FAIL: hub not reachable", file=sys.stderr)
        sys.exit(1)
    main()

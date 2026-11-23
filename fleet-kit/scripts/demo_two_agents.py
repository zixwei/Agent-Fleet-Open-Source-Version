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

    code, task = req(
        "POST",
        "/tasks",
        TENANT_KEY,
        {"agent_id": "worker", "title": "fleet-kit demo: summarize ok"},
    )
    must(code == 200, f"create_task {task}")
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

    # Idempotent republish via Hub bus is covered in pytest; here re-post same logical
    # fields creates a NEW message_id (envelope builds new ids). Dedup is on idempotency_key
    # inside one envelope — exercise receipt duplicate instead.

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

    code, _ = req("POST", f"/tasks/{task_id}/status", TENANT_KEY, {"status": "working"})
    must(code == 200, "status working")
    code, done = req("POST", f"/tasks/{task_id}/status", TENANT_KEY, {"status": "completed"})
    must(code == 200 and done.get("status") == "completed", f"status completed {done}")

    code, task_view = req("GET", f"/tasks/{task_id}", TENANT_KEY)
    must(code == 200 and task_view.get("status") == "completed", "get_task completed")

    code, msgs = req("GET", f"/tasks/{task_id}/messages", TENANT_KEY)
    must(code == 200 and isinstance(msgs, list) and len(msgs) >= 2, f"list_messages n={len(msgs) if isinstance(msgs, list) else msgs}")

    code, verify = req("GET", "/audit/verify", TENANT_KEY)
    must(code == 200 and verify.get("ok") is True, f"audit verify {verify}")

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

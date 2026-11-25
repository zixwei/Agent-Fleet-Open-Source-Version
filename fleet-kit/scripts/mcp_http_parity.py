#!/usr/bin/env python3
"""Prove MCP tool wrappers hit the same Hub with agent keys only (no LLM).

Runs the FastMCP tool functions in-process against a running Hub. No tenant
admin key is used: the orchestrator creates the task and posts, the worker
receipts and moves the task, exactly as two MCP hosts would.
"""
from __future__ import annotations

import json
import os
import sys

# Ensure fleet-kit root on path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

os.environ.setdefault("FLEET_HUB_URL", "http://127.0.0.1:8000")
assert not os.environ.get("FLEET_API_KEY"), "parity must run without the tenant key"

import mcp_server as ms  # noqa: E402
from mcp_server import (  # noqa: E402
    create_task,
    get_task,
    list_messages,
    post_message,
    receipt,
    set_status,
)

ORCH_KEY = os.environ["FLEET_AGENT_A_KEY"]
WORKER_KEY = os.environ["FLEET_AGENT_B_KEY"]


def parse(s: str) -> dict:
    return json.loads(s)


def act_as(key: str, agent_id: str) -> None:
    # mcp_server reads these module globals at call time; one MCP host
    # process = one identity, so we switch identity by rebinding them.
    ms.AGENT_KEY = key
    ms.AGENT_ID = agent_id


def main() -> None:
    demo_task = os.environ["FLEET_DEMO_TASK_ID"]

    act_as(ORCH_KEY, "orchestrator")
    created = parse(create_task(agent_id="worker", title="mcp parity"))
    assert created["ok"], created
    task_id = created["data"]["task_id"]
    assert task_id != demo_task
    assert created["data"]["created_by_agent_id"] == "orchestrator", created

    key = f"{task_id}:mcp.check:1"
    sent = parse(post_message(
        task_id=task_id, event_type="mcp.check", payload_json='{"via":"mcp"}',
        recipient_agent_id="worker", idempotency_key=key,
    ))
    assert sent["ok"] and sent["data"]["stored"] is True, sent
    mid = sent["data"]["message"]["message_id"]

    # Retry with the same key (e.g. after a timeout): no second message.
    retry = parse(post_message(
        task_id=task_id, event_type="mcp.check", payload_json='{"via":"mcp"}',
        recipient_agent_id="worker", idempotency_key=key,
    ))
    assert retry["ok"] and retry["data"]["stored"] is False, retry
    assert retry["data"]["message"]["message_id"] == mid, retry

    # The orchestrator is not the assignee: it cannot complete the task.
    denied = parse(set_status(task_id=task_id, status="working"))
    assert not denied["ok"] and denied["status"] == 403, denied

    act_as(WORKER_KEY, "worker")
    ack = parse(receipt(task_id=task_id, message_id=mid))
    assert ack["ok"] and ack["data"]["stored"] is True, ack
    st = parse(set_status(task_id=task_id, status="working"))
    assert st["ok"], st
    st = parse(set_status(task_id=task_id, status="completed"))
    assert st["ok"] and st["data"]["status"] == "completed", st

    gt = parse(get_task(task_id=task_id))
    assert gt["ok"] and gt["data"]["task_id"] == task_id, gt
    lm = parse(list_messages(task_id=task_id))
    assert lm["ok"] and len(lm["data"]) == 1 and lm["data"][0]["message_id"] == mid, lm

    print(json.dumps({"mcp_parity_task_id": task_id, "agent_keys_only": True, "ok": True}))
    print("PASS")


if __name__ == "__main__":
    main()

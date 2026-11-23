#!/usr/bin/env python3
"""Prove MCP tool wrappers hit the same Hub task id (no LLM).

Starts the FastMCP tool functions in-process against a running Hub.
"""
from __future__ import annotations

import json
import os
import sys

# Ensure fleet-kit root on path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

os.environ.setdefault("FLEET_HUB_URL", "http://127.0.0.1:8000")

from mcp_server import (  # noqa: E402
    create_task,
    get_task,
    list_messages,
    post_message,
    receipt,
    set_status,
)


def parse(s: str) -> dict:
    return json.loads(s)


def main() -> None:
    task_id = os.environ["FLEET_DEMO_TASK_ID"]
    # create another tiny task via MCP facade then operate
    created = parse(create_task(agent_id="worker", title="mcp parity"))
    assert created["ok"], created
    mcp_task = created["data"]["task_id"]
    assert mcp_task != task_id

    sent = parse(
        post_message(
            task_id=mcp_task,
            event_type="mcp.check",
            payload_json='{"via":"mcp"}',
            recipient_agent_id="worker",
            sender_agent_id="orchestrator",
        )
    )
    assert sent["ok"] and sent["data"]["stored"] is True, sent
    mid = sent["data"]["message"]["message_id"]

    # receipt needs worker agent key
    os.environ["FLEET_AGENT_API_KEY"] = os.environ["FLEET_AGENT_B_KEY"]
    # reload headers path: mcp_server reads AGENT_KEY at import — rebind module var
    import mcp_server as ms

    ms.AGENT_KEY = os.environ["FLEET_AGENT_B_KEY"]
    ack = parse(receipt(task_id=mcp_task, message_id=mid))
    assert ack["ok"] and ack["data"]["stored"] is True, ack

    ms.AGENT_KEY = os.environ.get("FLEET_AGENT_A_KEY", "")
    ms.TENANT_KEY = os.environ["FLEET_API_KEY"]
    st = parse(set_status(task_id=mcp_task, status="working"))
    assert st["ok"], st
    st = parse(set_status(task_id=mcp_task, status="completed"))
    assert st["ok"] and st["data"]["status"] == "completed", st

    gt = parse(get_task(task_id=mcp_task))
    assert gt["ok"] and gt["data"]["task_id"] == mcp_task, gt
    lm = parse(list_messages(task_id=mcp_task))
    assert lm["ok"] and any(m["message_id"] == mid for m in lm["data"]), lm

    print(json.dumps({"mcp_parity_task_id": mcp_task, "ok": True}))
    print("PASS")


if __name__ == "__main__":
    main()

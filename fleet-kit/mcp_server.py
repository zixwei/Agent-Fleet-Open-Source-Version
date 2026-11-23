#!/usr/bin/env python3
"""Fleet Kit MCP facade — thin tool layer over the Hub HTTP API.

Identity comes from env FLEET_AGENT_API_KEY (or FLEET_API_KEY for tenant
admin tools). Message bodies never decide who you are.

Tools: create_task, post_message, get_task, list_messages, receipt, set_status
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

HUB_URL = os.environ.get("FLEET_HUB_URL", "http://127.0.0.1:8000").rstrip("/")
AGENT_KEY = os.environ.get("FLEET_AGENT_API_KEY", "")
TENANT_KEY = os.environ.get("FLEET_API_KEY", "")
AGENT_ID = os.environ.get("FLEET_AGENT_ID", "")

mcp = FastMCP(
    "fleet-kit",
    instructions=(
        "Agent Fleet Kit 1.0 task ledger. Tools talk to a local Hub. "
        "Not A2A-certified. Realtime means next tool call / short poll."
    ),
)


def _headers(prefer_agent: bool = True) -> dict[str, str]:
    key = AGENT_KEY if (prefer_agent and AGENT_KEY) else (TENANT_KEY or AGENT_KEY)
    if not key:
        raise RuntimeError("Set FLEET_AGENT_API_KEY and/or FLEET_API_KEY")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _client() -> httpx.Client:
    return httpx.Client(base_url=HUB_URL, timeout=30.0)


def _json(resp: httpx.Response) -> Any:
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        return {"ok": False, "status": resp.status_code, "error": data}
    return {"ok": True, "status": resp.status_code, "data": data}


@mcp.tool()
def create_task(agent_id: str, title: str) -> str:
    """Create a task assigned to an Active agent. Uses tenant/admin key."""
    with _client() as c:
        r = c.post(
            "/tasks",
            headers=_headers(prefer_agent=False),
            json={"agent_id": agent_id, "title": title},
        )
    return json.dumps(_json(r), ensure_ascii=False)


@mcp.tool()
def post_message(
    task_id: str,
    event_type: str,
    payload_json: str = "{}",
    recipient_agent_id: str | None = None,
    sender_agent_id: str | None = None,
) -> str:
    """Post a message on a task. Sender is the authenticated agent when an agent key is set."""
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False, "error": f"invalid payload_json: {e}"})
    sender = sender_agent_id or AGENT_ID
    if not sender:
        return json.dumps(
            {"ok": False, "error": "set sender_agent_id or FLEET_AGENT_ID"}
        )
    body: dict[str, Any] = {
        "sender_agent_id": sender,
        "event_type": event_type,
        "payload": payload,
    }
    if recipient_agent_id:
        body["recipient_agent_id"] = recipient_agent_id
    with _client() as c:
        r = c.post(
            f"/tasks/{task_id}/messages",
            headers=_headers(prefer_agent=True),
            json=body,
        )
    return json.dumps(_json(r), ensure_ascii=False)


@mcp.tool()
def get_task(task_id: str) -> str:
    """Get task status and metadata."""
    with _client() as c:
        r = c.get(f"/tasks/{task_id}", headers=_headers())
    return json.dumps(_json(r), ensure_ascii=False)


@mcp.tool()
def list_messages(task_id: str) -> str:
    """List messages (and receipts) for a task."""
    with _client() as c:
        r = c.get(f"/tasks/{task_id}/messages", headers=_headers())
    return json.dumps(_json(r), ensure_ascii=False)


@mcp.tool()
def receipt(task_id: str, message_id: str) -> str:
    """Confirm delivery of a message. Requires FLEET_AGENT_API_KEY for the recipient."""
    if not AGENT_KEY:
        return json.dumps({"ok": False, "error": "FLEET_AGENT_API_KEY required for receipt"})
    with _client() as c:
        r = c.post(
            f"/tasks/{task_id}/messages/{message_id}/receipt",
            headers=_headers(prefer_agent=True),
            json={},
        )
    return json.dumps(_json(r), ensure_ascii=False)


@mcp.tool()
def set_status(task_id: str, status: str) -> str:
    """Move task status: submitted -> working -> completed|failed|cancelled."""
    with _client() as c:
        r = c.post(
            f"/tasks/{task_id}/status",
            headers=_headers(prefer_agent=False),
            json={"status": status},
        )
    return json.dumps(_json(r), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()

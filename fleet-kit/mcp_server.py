#!/usr/bin/env python3
"""Fleet Kit MCP facade — thin tool layer over the Hub HTTP API.

Identity comes from env FLEET_AGENT_API_KEY only: one MCP host config = one
agent. The tenant admin key (FLEET_API_KEY) must never be put in a host
config; registering, approving and suspending agents are deliberately not
exposed as tools. Message bodies never decide who you are.

Tools: create_task, post_message, get_task, list_messages, receipt, set_status
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

HUB_URL = os.environ.get("FLEET_HUB_URL", "http://127.0.0.1:8000").rstrip("/")
AGENT_KEY = os.environ.get("FLEET_AGENT_API_KEY", "")
AGENT_ID = os.environ.get("FLEET_AGENT_ID", "")

if os.environ.get("FLEET_API_KEY"):
    # stderr: stdout carries the MCP stdio protocol.
    print(
        "fleet-kit MCP: FLEET_API_KEY is set but ignored; remove the tenant "
        "admin key from this host config (agent key only).",
        file=sys.stderr,
    )

mcp = FastMCP(
    "fleet-kit",
    instructions=(
        "Agent Fleet Kit 1.0 task ledger. Tools talk to a local Hub. "
        "Not A2A-certified. Realtime means next tool call / short poll."
    ),
)


def _headers() -> dict[str, str]:
    if not AGENT_KEY:
        raise RuntimeError("Set FLEET_AGENT_API_KEY (this host's agent key)")
    return {"X-API-Key": AGENT_KEY, "Content-Type": "application/json"}


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
    """Create a task assigned to an Active agent in your tenant.

    You are recorded as the task's creator: you may cancel it later; only the
    assigned agent can move it to working/completed/failed.
    """
    with _client() as c:
        r = c.post(
            "/tasks",
            headers=_headers(),
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
    idempotency_key: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
) -> str:
    """Post a message on a task. Sender must be the authenticated agent.

    idempotency_key: pick a stable value per logical message (for example
    "<task_id>:assign:1") and reuse it when retrying after a timeout or
    error; the Hub then returns the stored message (stored=false) instead
    of recording a second one. Use a new value for a new message.

    correlation_id: when replying, pass the message_id you are answering.
    trace_id: leave empty (the task's trace is used) unless you are
    propagating a trace that started outside the Hub.
    """
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
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    if correlation_id:
        body["correlation_id"] = correlation_id
    if trace_id:
        body["trace_id"] = trace_id
    with _client() as c:
        r = c.post(
            f"/tasks/{task_id}/messages",
            headers=_headers(),
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
    """Confirm delivery of a message addressed to you."""
    with _client() as c:
        r = c.post(
            f"/tasks/{task_id}/messages/{message_id}/receipt",
            headers=_headers(),
            json={},
        )
    return json.dumps(_json(r), ensure_ascii=False)


@mcp.tool()
def set_status(task_id: str, status: str) -> str:
    """Move task status: submitted -> working -> completed|failed|cancelled.

    The assigned agent may move its own task; the creator may only cancel.
    403 = not allowed for you; 409 = someone else changed the task first,
    re-read it with get_task before retrying.
    """
    with _client() as c:
        r = c.post(
            f"/tasks/{task_id}/status",
            headers=_headers(),
            json={"status": status},
        )
    return json.dumps(_json(r), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()

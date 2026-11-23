"""Module 2 - Fleet Control Plane (Agent Registry).

Discovery step 1 (plan v0.2.6): manual registration, then admin approval.
An agent starts as Pending; an approved agent becomes Active. Only Active
agents can receive tasks. Card verification states (VERIFIED / UNVERIFIED /
INVALID) are recorded on the agent; this prototype stores the state the
admin sets and does not claim verification it did not perform.
"""
import json

from app.messaging import now_iso
from app import storage as db


def register_agent(agent_id: str, name: str, endpoint: str, capabilities: list) -> dict:
    if db.get_agent(agent_id):
        raise ValueError(f"agent {agent_id} already registered")
    created_at = now_iso()
    db.insert_agent(agent_id, name, endpoint, capabilities, "Pending", created_at)
    return get_agent(agent_id)


def get_agent(agent_id: str):
    row = db.get_agent(agent_id)
    if not row:
        return None
    row["capabilities"] = json.loads(row["capabilities"])
    return row


def list_agents():
    out = []
    for row in db.list_agents():
        row["capabilities"] = json.loads(row["capabilities"])
        out.append(row)
    return out


def approve_agent(agent_id: str) -> dict:
    if not db.get_agent(agent_id):
        raise ValueError(f"agent {agent_id} not found")
    db.set_agent_status(agent_id, "Active")
    return get_agent(agent_id)


def suspend_agent(agent_id: str) -> dict:
    if not db.get_agent(agent_id):
        raise ValueError(f"agent {agent_id} not found")
    db.set_agent_status(agent_id, "Suspended")
    return get_agent(agent_id)

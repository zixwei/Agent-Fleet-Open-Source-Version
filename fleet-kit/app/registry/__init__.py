"""Module 2 - Fleet Control Plane (Agent Registry).

Agents are tenant-scoped. Admin approve/suspend requires the caller's tenant
to own the agent.
"""
import json

from app.messaging import now_iso
from app import storage as db


def register_agent(
    tenant_id: str, agent_id: str, name: str, endpoint: str, capabilities: list
) -> dict:
    existing = db.get_agent(agent_id)
    if existing:
        raise ValueError(f"agent {agent_id} already registered")
    created_at = now_iso()
    db.insert_agent(
        agent_id, tenant_id, name, endpoint, capabilities, "Pending", created_at
    )
    return get_agent(agent_id, tenant_id)


def get_agent(agent_id: str, tenant_id: str | None = None):
    row = db.get_agent(agent_id, tenant_id=tenant_id)
    if not row:
        return None
    row["capabilities"] = json.loads(row["capabilities"])
    return row


def list_agents(tenant_id: str | None = None):
    out = []
    for row in db.list_agents(tenant_id=tenant_id):
        row["capabilities"] = json.loads(row["capabilities"])
        out.append(row)
    return out


def approve_agent(tenant_id: str, agent_id: str) -> dict:
    if not db.get_agent(agent_id, tenant_id=tenant_id):
        raise ValueError(f"agent {agent_id} not found")
    if not db.set_agent_status(agent_id, "Active", tenant_id=tenant_id):
        raise ValueError(f"agent {agent_id} not found")
    return get_agent(agent_id, tenant_id)


def suspend_agent(tenant_id: str, agent_id: str) -> dict:
    if not db.get_agent(agent_id, tenant_id=tenant_id):
        raise ValueError(f"agent {agent_id} not found")
    if not db.set_agent_status(agent_id, "Suspended", tenant_id=tenant_id):
        raise ValueError(f"agent {agent_id} not found")
    return get_agent(agent_id, tenant_id)

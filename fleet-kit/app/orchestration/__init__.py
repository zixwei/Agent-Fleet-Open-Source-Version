"""Module 3 - Orchestration Layer (task lifecycle).

Task states: submitted -> working -> completed | failed | cancelled.
A task is never silently lost (principle 6): every transition is recorded
and the audit log keeps the history. Only Active agents can be assigned.
"""
import uuid

from app.messaging import now_iso
from app import storage as db

VALID_TRANSITIONS = {
    "submitted": {"working", "cancelled"},
    "working": {"completed", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def create_task(tenant_id: str, agent_id: str, title: str) -> dict:
    agent = db.get_agent(agent_id)
    if not agent:
        raise ValueError(f"agent {agent_id} not found")
    if agent["status"] != "Active":
        raise ValueError(f"agent {agent_id} is {agent['status']}, not Active")
    task_id = str(uuid.uuid4())
    now = now_iso()
    db.insert_task(task_id, tenant_id, agent_id, title, "submitted", now, now)
    return get_task(task_id)


def get_task(task_id: str):
    return db.get_task(task_id)


def list_tasks(tenant_id: str | None = None):
    return db.list_tasks(tenant_id)


def set_status(task_id: str, status: str) -> dict:
    task = db.get_task(task_id)
    if not task:
        raise ValueError(f"task {task_id} not found")
    if status not in VALID_TRANSITIONS.get(task["status"], set()):
        raise ValueError(f"cannot move task {task_id} from {task['status']} to {status}")
    db.set_task_status(task_id, status, now_iso())
    return get_task(task_id)

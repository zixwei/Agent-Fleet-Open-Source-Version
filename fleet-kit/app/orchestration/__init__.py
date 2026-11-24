"""Module 3 - Orchestration Layer (task lifecycle).

Task states: submitted -> working -> completed | failed | cancelled.
A task is never silently lost (principle 6): every transition is recorded
and the audit log keeps the history. Only Active agents can be assigned.

Who may request a transition (least privilege, so MCP hosts never need the
tenant admin key):

- tenant/admin credential: any legal transition (operator override)
- the task's assigned agent: working, completed, failed, cancelled
- the agent that created the task: cancelled only
- any other agent credential: nothing (403)
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

ASSIGNEE_TARGETS = {"working", "completed", "failed", "cancelled"}
CREATOR_TARGETS = {"cancelled"}


class TransitionConflict(ValueError):
    """The task changed between our read and our conditional write: a
    concurrent transition won the compare-and-set. Surfaced as HTTP 409."""


def create_task(
    tenant_id: str, agent_id: str, title: str, created_by_agent_id: str | None = None
) -> dict:
    agent = db.get_agent(agent_id, tenant_id=tenant_id)
    if not agent:
        raise ValueError(f"agent {agent_id} not found")
    if agent["status"] != "Active":
        raise ValueError(f"agent {agent_id} is {agent['status']}, not Active")
    task_id = str(uuid.uuid4())
    now = now_iso()
    db.insert_task(
        task_id, tenant_id, agent_id, title, "submitted", now, now,
        created_by_agent_id=created_by_agent_id,
    )
    return get_task(task_id)


def get_task(task_id: str):
    return db.get_task(task_id)


def list_tasks(tenant_id: str | None = None):
    return db.list_tasks(tenant_id)


def can_request_status(task: dict, auth: dict, status: str) -> bool:
    """Authorization only; legality of the transition is checked in set_status."""
    agent_id = auth.get("agent_id")
    if agent_id is None:
        return True
    if agent_id == task["agent_id"] and status in ASSIGNEE_TARGETS:
        return True
    if agent_id == task.get("created_by_agent_id") and status in CREATOR_TARGETS:
        return True
    return False


def set_status(task_id: str, status: str) -> dict:
    task = db.get_task(task_id)
    if not task:
        raise ValueError(f"task {task_id} not found")
    current = task["status"]
    if status not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f"cannot move task {task_id} from {current} to {status}")
    # Atomic CAS: concurrent transitions cannot both succeed from the same state.
    if not db.set_task_status_atomic(task_id, current, status, now_iso()):
        raise TransitionConflict(
            f"task {task_id} changed concurrently (was {current}); re-read and retry"
        )
    return get_task(task_id)

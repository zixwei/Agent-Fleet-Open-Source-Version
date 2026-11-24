"""Agent Fleet Hub - Core API (modular monolith, Stage 0 deliverable #1).

Endpoints (all except /health require X-API-Key):
  POST /agents              register an agent (manual registration, Pending)
  GET  /agents              list agents
  POST /agents/{id}/approve approve an agent (Pending -> Active)
  POST /agents/{id}/suspend suspend an agent
  POST /tasks               create a task for an Active agent
  GET  /tasks               list tasks
  GET  /tasks/{id}          get task status
  POST /tasks/{id}/status   move a task (submitted->working->completed/failed/cancelled)
  POST /tasks/{id}/messages send a message (idempotent on idempotency_key)
  POST /tasks/{id}/messages/{message_id}/receipt
       recipient confirms delivery (formal T01 acceptance; idempotent)
  GET  /tasks/{id}/messages list messages for a task (each carries its receipt, if any)
  GET  /audit               append-only audit log (hash-chained)
  GET  /audit/verify        verify the audit hash chain
  GET  /health              health check (no auth)

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import config
from app.identity import require_agent_auth, require_api_key
from app import observability as audit
from app import registry
from app.messaging.bus import bus
from app import orchestration
from app import storage as db

app = FastAPI(title="Agent Fleet Hub", version="0.1.0-stage0")


@app.on_event("startup")
def startup():
    db.init_db()


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-fleet-hub", "version": "0.1.0-stage0"}


class AgentIn(BaseModel):
    agent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    capabilities: list[str] = []


@app.post("/agents")
def create_agent(body: AgentIn, auth=Depends(require_api_key)):
    try:
        agent = registry.register_agent(body.agent_id, body.name, body.endpoint, body.capabilities)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.record(auth["tenant_id"], "agent.register", body.agent_id, {"name": body.name})
    return agent


@app.get("/agents")
def list_agents(auth=Depends(require_api_key)):
    return registry.list_agents()


@app.post("/agents/{agent_id}/approve")
def approve_agent(agent_id: str, auth=Depends(require_api_key)):
    try:
        agent = registry.approve_agent(agent_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    audit.record(auth["tenant_id"], "agent.approve", agent_id, {})
    return agent


@app.post("/agents/{agent_id}/suspend")
def suspend_agent(agent_id: str, auth=Depends(require_api_key)):
    try:
        agent = registry.suspend_agent(agent_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    audit.record(auth["tenant_id"], "agent.suspend", agent_id, {})
    return agent


class TaskIn(BaseModel):
    agent_id: str = Field(min_length=1)
    title: str = Field(min_length=1)


@app.post("/tasks")
def create_task(body: TaskIn, auth=Depends(require_api_key)):
    try:
        task = orchestration.create_task(auth["tenant_id"], body.agent_id, body.title)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(auth["tenant_id"], "task.create", task["task_id"], {"agent_id": body.agent_id})
    return task


@app.get("/tasks")
def list_tasks(auth=Depends(require_api_key)):
    return orchestration.list_tasks(auth["tenant_id"])


def _get_task_for_tenant(task_id: str, tenant_id: str) -> dict:
    """Object-level authorization (T08 fix): a task is visible only to the
    tenant that owns it. A cross-tenant id returns 404, never 403, so the
    response does not leak whether the task exists."""
    task = orchestration.get_task(task_id)
    if not task or task["tenant_id"] != tenant_id:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.get("/tasks/{task_id}")
def get_task(task_id: str, auth=Depends(require_api_key)):
    return _get_task_for_tenant(task_id, auth["tenant_id"])


class StatusIn(BaseModel):
    status: str = Field(min_length=1)


@app.post("/tasks/{task_id}/status")
def set_task_status(task_id: str, body: StatusIn, auth=Depends(require_api_key)):
    _get_task_for_tenant(task_id, auth["tenant_id"])
    try:
        task = orchestration.set_status(task_id, body.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(auth["tenant_id"], "task.status", task_id, {"status": body.status})
    return task


class MessageIn(BaseModel):
    sender_agent_id: str = Field(min_length=1)
    # T01 fix: the caller may name the recipient explicitly. When omitted,
    # routing is implicit: a message from anyone other than the task's
    # agent goes to the task's agent (previous behaviour); a reply from
    # the task's agent goes back to the other party on the task (the
    # sender of the most recent message that did not come from the task's
    # agent). Without a prior inbound message there is no other party, so
    # the task's agent stays the recipient. An explicit recipient always
    # wins over the implicit rule.
    recipient_agent_id: str | None = None
    event_type: str = Field(min_length=1)
    payload: dict = {}


def _implicit_recipient(task_id: str, sender_agent_id: str, task_agent_id: str) -> str:
    """Implicit reply routing (T01): the other party on the task.

    A message from anyone other than the task's agent goes to the task's
    agent. A reply from the task's agent goes back to the sender of the
    most recent message on this task that did not come from the task's
    agent (the requester). With no prior inbound message there is no
    other party, so the task's agent stays the recipient.
    """
    if sender_agent_id != task_agent_id:
        return task_agent_id
    for message in reversed(bus.list_for_task(task_id)):
        if message["sender_agent_id"] != task_agent_id:
            return message["sender_agent_id"]
    return task_agent_id


@app.post("/tasks/{task_id}/messages")
def send_message(task_id: str, body: MessageIn, auth=Depends(require_api_key)):
    task = _get_task_for_tenant(task_id, auth["tenant_id"])
    # Agent credential presented: the sender must be the authenticated
    # agent (formal T01 identity binding). A tenant key keeps the old
    # behaviour (sender must be a registered agent, checked below).
    if auth.get("agent_id") is not None and body.sender_agent_id != auth["agent_id"]:
        raise HTTPException(
            status_code=403, detail="sender does not match authenticated agent"
        )
    if not registry.get_agent(body.sender_agent_id):
        raise HTTPException(
            status_code=400, detail=f"sender agent {body.sender_agent_id} not found"
        )
    recipient_id = body.recipient_agent_id or _implicit_recipient(
        task_id, body.sender_agent_id, task["agent_id"]
    )
    if not registry.get_agent(recipient_id):
        raise HTTPException(
            status_code=400, detail=f"recipient agent {recipient_id} not found"
        )
    result = bus.send(
        tenant_id=auth["tenant_id"],
        sender_agent_id=body.sender_agent_id,
        recipient_agent_id=recipient_id,
        task_id=task_id,
        event_type=body.event_type,
        payload=body.payload,
    )
    audit.record(
        auth["tenant_id"],
        "message.send" if result["stored"] else "message.duplicate",
        task_id,
        {"event_type": body.event_type, "stored": result["stored"]},
    )
    return {"stored": result["stored"], "message": result["message"]}


@app.get("/tasks/{task_id}/messages")
def list_messages(task_id: str, auth=Depends(require_api_key)):
    _get_task_for_tenant(task_id, auth["tenant_id"])
    return bus.list_for_task(task_id)


class ReceiptIn(BaseModel):
    # Optional cross-check only. The acting identity is always derived
    # from the agent credential, never trusted from this field.
    recipient_agent_id: str | None = None


@app.post("/tasks/{task_id}/messages/{message_id}/receipt")
def receipt_message(task_id: str, message_id: str, body: ReceiptIn, auth=Depends(require_agent_auth)):
    """Delivery receipt (formal T01 acceptance).

    The recipient's identity comes from its agent credential
    (require_agent_auth): a tenant key alone cannot receipt (401), a
    credential for another agent cannot receipt this message (403), and
    a suspended/unregistered agent's credential is rejected (401).
    Routing metadata only proves the hub stored the message; the receipt
    proves the receiving agent itself got it. A duplicate receipt is a
    no-op returning the stored receipt (stored=false). Unknown message,
    message on another task, or cross-tenant message all return 404.
    """
    _get_task_for_tenant(task_id, auth["tenant_id"])
    recipient_id = auth["agent_id"]
    if body.recipient_agent_id is not None and body.recipient_agent_id != recipient_id:
        raise HTTPException(
            status_code=400,
            detail="recipient_agent_id does not match authenticated agent",
        )
    try:
        result = bus.acknowledge(
            tenant_id=auth["tenant_id"],
            task_id=task_id,
            message_id=message_id,
            recipient_agent_id=recipient_id,
        )
    except ValueError as e:
        if str(e) == "recipient mismatch":
            raise HTTPException(
                status_code=403,
                detail="authenticated agent is not the message recipient",
            )
        raise HTTPException(status_code=404, detail="message not found")
    audit.record(
        auth["tenant_id"],
        "message.receipt" if result["stored"] else "message.receipt.duplicate",
        task_id,
        {"message_id": message_id, "stored": result["stored"]},
    )
    return result


@app.get("/audit")
def list_audit(limit: int = 100, auth=Depends(require_api_key)):
    # T08: audit entries are scoped to the caller's tenant (actor is the
    # tenant id). The chain itself is still verified whole in /audit/verify.
    return audit.list_audit(limit, actor=auth["tenant_id"])


@app.get("/audit/verify")
def verify_audit(auth=Depends(require_api_key)):
    return audit.verify_chain()

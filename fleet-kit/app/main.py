"""Agent Fleet Hub - Core API (modular monolith, Stage 0 deliverable #1).

Endpoints (all except /health require X-API-Key; /ops/* take the operator key):
  POST /agents              register an agent (manual registration, Pending)
  GET  /agents              list agents
  POST /agents/{id}/approve approve an agent (Pending -> Active)
  POST /agents/{id}/suspend suspend an agent
  POST /tasks               create a task for an Active agent
  GET  /tasks               list tasks
  GET  /tasks/{id}          get task status
  POST /tasks/{id}/status   move a task (submitted->working->completed/failed/cancelled;
                            agent keys: assignee, or creator for cancel; 409 on a lost race)
  POST /tasks/{id}/messages send a message (idempotent on idempotency_key)
  POST /tasks/{id}/messages/{message_id}/receipt
       recipient confirms delivery (formal T01 acceptance; idempotent)
  GET  /tasks/{id}/messages list messages for a task (each carries its receipt, if any)
  GET  /tasks/{id}/correlation T09: messages/receipts/status vs. audit entries
  GET  /audit               append-only audit log (hash-chained)
  GET  /audit/verify        tenant admin: is the whole audit chain intact ({"ok"} only)
  GET  /ops/audit/verify    operator: full / incremental verification with checkpoint
  GET  /ops/audit           operator: audit log across tenants (actor filter)
  GET  /health              health check (no auth)

Run:  uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
import logging

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from app import config
from app import identity
from app.identity import (
    UNAUTHENTICATED_ACTOR,
    require_agent_auth,
    require_api_key,
    require_operator_auth,
    require_tenant_auth,
)
from app import observability as audit
from app.observability.denials import DenialLimiter
from app import registry
from app.messaging.bus import bus
from app import orchestration
from app import storage as db

app = FastAPI(title="Agent Fleet Hub", version="1.0.0-fleet-kit")
logger = logging.getLogger("agent_fleet_hub")


@app.on_event("startup")
def startup():
    # Refuse to start on a malformed key configuration (e.g. a tenant id that
    # collides with a reserved system actor, or a weak/reused operator key).
    problems = identity.validate_key_config()
    if problems:
        raise RuntimeError("invalid key configuration: " + "; ".join(problems))
    db.init_db()


@app.on_event("shutdown")
def shutdown():
    # Close the current denial window so its counts reach the audit log.
    _flush_denials(force=True)


# T09 / whitepaper §8.3 "security anomalies": requests our code refuses are
# audited. Only fastapi.HTTPException raised by this app counts; Starlette's
# own 404/405 for unknown routes are not recorded (scanner noise). 429 is the
# brute-force backoff (app/identity/attempts.py); like every denial it goes
# through denial_limiter, so a guessing flood cannot grow the log either.
AUDITED_DENIALS = {401, 403, 404, 409, 429}

# Flood protection: only the first few denials per source/principal and
# window get their own entry; the rest are counted into summaries. See
# app/observability/denials.py for the bound.
denial_limiter = DenialLimiter(
    window_seconds=config.DENIAL_WINDOW_SECONDS,
    per_source=config.DENIAL_PER_SOURCE,
    per_principal=config.DENIAL_PER_PRINCIPAL,
    max_sources=config.DENIAL_MAX_SOURCES,
    max_individual=config.DENIAL_MAX_INDIVIDUAL,
)

# Path and reason are partly caller-controlled (path parameters); keep the
# individual entries bounded in size.
MAX_LOGGED_TEXT = 256


def _clip(text: str) -> str:
    return text if len(text) <= MAX_LOGGED_TEXT else text[:MAX_LOGGED_TEXT]


def _route_template(request: Request) -> str:
    """Route pattern such as /tasks/{task_id}: a bounded set of values, unlike
    the raw path, so it is safe to count by."""
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    for candidate in request.app.router.routes:
        match, _ = candidate.matches(request.scope)
        if match == Match.FULL:
            return getattr(candidate, "path", "<unmatched>")
    return "<unmatched>"


def _source(request: Request) -> str:
    # The direct peer. Behind a reverse proxy this is the proxy for every
    # client; X-Forwarded-For is not trusted (spoofable). The bound holds
    # either way, only the per-source detail gets coarser.
    return request.client.host if request.client else "unknown"


def _write_summaries(summaries: list) -> None:
    for s in summaries:
        audit.record(s["actor"], "request.denied.summary", s["target"], s["detail"])


def _flush_denials(force: bool = False) -> None:
    try:
        _write_summaries(denial_limiter.flush(force=force))
    except Exception:
        logger.exception("failed to write denial summaries")


def _record_denial(request: Request, exc: HTTPException) -> None:
    try:
        _write_denial(request, exc)
    except Exception:
        # The request is refused either way; an audit write failure must
        # not turn a 401/403 into a 500. It is logged, not swallowed.
        logger.exception("failed to audit denied request %s %s", request.method, request.url.path)


def _write_denial(request: Request, exc: HTTPException) -> None:
    auth = getattr(request.state, "auth", None)
    claimed = getattr(request.state, "claimed", None)
    if auth is not None:
        actor, who = auth["tenant_id"], audit.principal(auth)
    elif claimed is not None:
        # A refused agent key: file it under the tenant the key names, so
        # that tenant sees a suspended/unregistered agent still trying.
        actor = claimed["tenant_id"]
        who = {"kind": "refused_agent_key", "agent_id": claimed["agent_id"]}
    else:
        actor, who = UNAUTHENTICATED_ACTOR, {"kind": None, "agent_id": None}
    source = _source(request)
    write, summaries = denial_limiter.decide(
        actor=actor,
        principal=who,
        source=source,
        status=exc.status_code,
        route=_route_template(request),
    )
    _write_summaries(summaries)
    if not write:
        return
    path = request.url.path
    detail = {
        "status": exc.status_code,
        "method": request.method,
        "path": _clip(path),
        "reason": _clip(exc.detail) if isinstance(exc.detail, str) else "denied",
        "principal": who,
        "source": source,
    }
    if len(path) > MAX_LOGGED_TEXT:
        detail["path_truncated"] = True
    audit.record(actor, "request.denied", _clip(path), detail)


@app.exception_handler(StarletteHTTPException)
async def audited_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if isinstance(exc, HTTPException) and exc.status_code in AUDITED_DENIALS:
        await run_in_threadpool(_record_denial, request, exc)
    return await http_exception_handler(request, exc)


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-fleet-hub", "version": "1.0.0-fleet-kit"}


class AgentIn(BaseModel):
    agent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    capabilities: list[str] = []


@app.post("/agents")
def create_agent(body: AgentIn, auth=Depends(require_tenant_auth)):
    try:
        agent = registry.register_agent(
            auth["tenant_id"], body.agent_id, body.name, body.endpoint, body.capabilities
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.record(
        auth["tenant_id"], "agent.register", body.agent_id,
        {"name": body.name, "principal": audit.principal(auth)},
    )
    return agent


@app.get("/agents")
def list_agents(auth=Depends(require_api_key)):
    return registry.list_agents(tenant_id=auth["tenant_id"])


@app.post("/agents/{agent_id}/approve")
def approve_agent(agent_id: str, auth=Depends(require_tenant_auth)):
    try:
        agent = registry.approve_agent(auth["tenant_id"], agent_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    audit.record(auth["tenant_id"], "agent.approve", agent_id, {"principal": audit.principal(auth)})
    return agent


@app.post("/agents/{agent_id}/suspend")
def suspend_agent(agent_id: str, auth=Depends(require_tenant_auth)):
    try:
        agent = registry.suspend_agent(auth["tenant_id"], agent_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    audit.record(auth["tenant_id"], "agent.suspend", agent_id, {"principal": audit.principal(auth)})
    return agent


class TaskIn(BaseModel):
    agent_id: str = Field(min_length=1)
    title: str = Field(min_length=1)


@app.post("/tasks")
def create_task(body: TaskIn, auth=Depends(require_api_key)):
    # An agent credential may create tasks in its own tenant; that agent is
    # recorded as the creator (it may later cancel the task, nothing more).
    try:
        task = orchestration.create_task(
            auth["tenant_id"], body.agent_id, body.title,
            created_by_agent_id=auth.get("agent_id"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        auth["tenant_id"],
        "task.create",
        task["task_id"],
        {
            "task_id": task["task_id"],
            "agent_id": body.agent_id,
            "created_by_agent_id": auth.get("agent_id"),
            "trace_id": task["trace_id"],
            "principal": audit.principal(auth),
        },
    )
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
    task = _get_task_for_tenant(task_id, auth["tenant_id"])
    if body.status not in orchestration.VALID_TRANSITIONS:
        raise HTTPException(status_code=400, detail=f"unknown status {body.status}")
    # Key split: an agent credential may only move tasks it is assigned to
    # (or cancel tasks it created). See orchestration module docstring.
    if not orchestration.can_request_status(task, auth, body.status):
        raise HTTPException(
            status_code=403,
            detail="this credential may not move this task to that status",
        )
    try:
        previous, task = orchestration.transition(task_id, body.status)
    except orchestration.TransitionConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(
        auth["tenant_id"],
        "task.status",
        task_id,
        {
            "task_id": task_id,
            "from": previous,
            "to": task["status"],
            "trace_id": task.get("trace_id"),
            "principal": audit.principal(auth),
        },
    )
    return task


# Opaque ids (UUIDs, W3C trace ids, message ids): bounded, no spaces or
# separators that could be confused with the idempotency-key format.
TRACE_ID_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"


class MessageIn(BaseModel):
    sender_agent_id: str = Field(min_length=1)
    # T01 fix: the caller may name the recipient. A reply from the task's
    # agent goes back to the other party, not to itself. When omitted, the
    # recipient defaults to the task's agent (previous behaviour).
    recipient_agent_id: str | None = None
    event_type: str = Field(min_length=1)
    payload: dict = {}
    # Stable client key for retries. When set, a timed-out retry with the
    # same key is a no-op instead of a second message.
    idempotency_key: str | None = None
    # T09. trace_id: defaults to the task's trace; set it only to propagate
    # a trace that started outside the Hub. correlation_id: for a reply,
    # the message_id being answered; defaults to the message's own id.
    trace_id: str | None = Field(default=None, pattern=TRACE_ID_PATTERN)
    correlation_id: str | None = Field(default=None, pattern=TRACE_ID_PATTERN)


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
    if not registry.get_agent(body.sender_agent_id, tenant_id=auth["tenant_id"]):
        raise HTTPException(
            status_code=400, detail=f"sender agent {body.sender_agent_id} not found"
        )
    recipient_id = body.recipient_agent_id or task["agent_id"]
    if not registry.get_agent(recipient_id, tenant_id=auth["tenant_id"]):
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
        client_idempotency_key=body.idempotency_key,
        trace_id=body.trace_id or task.get("trace_id"),
        correlation_id=body.correlation_id,
    )
    stored_msg = result["message"]
    # On a duplicate, stored_msg is the original message, so the duplicate
    # entry points at the message it was folded into.
    audit.record(
        auth["tenant_id"],
        "message.send" if result["stored"] else "message.duplicate",
        task_id,
        {
            "task_id": task_id,
            "message_id": stored_msg["message_id"],
            "event_type": body.event_type,
            "sender_agent_id": stored_msg["sender_agent_id"],
            "recipient_agent_id": stored_msg["recipient_agent_id"],
            "trace_id": stored_msg.get("trace_id"),
            "correlation_id": stored_msg.get("correlation_id"),
            "stored": result["stored"],
            "principal": audit.principal(auth),
        },
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
        {
            "task_id": task_id,
            "message_id": message_id,
            "recipient_agent_id": recipient_id,
            "stored": result["stored"],
            "principal": audit.principal(auth),
        },
    )
    return result


@app.get("/tasks/{task_id}/correlation")
def task_correlation(task_id: str, auth=Depends(require_api_key)):
    """T09: does every message, receipt and the current status of this task
    trace back to this tenant's audit entries? See observability.correlate_task."""
    _get_task_for_tenant(task_id, auth["tenant_id"])
    return audit.correlate_task(auth["tenant_id"], task_id)


AUDIT_PAGE_MAX = 1000


@app.get("/audit")
def list_audit(
    limit: int = Query(100, ge=1, le=AUDIT_PAGE_MAX),
    auth=Depends(require_api_key),
):
    # T08: audit entries are scoped to the caller's tenant (actor is the
    # tenant id). The chain itself is still verified whole in /audit/verify.
    _flush_denials()  # summaries of closed denial windows show up right away
    return audit.list_audit(limit, actor=auth["tenant_id"])


@app.get("/audit/verify")
def verify_audit(auth=Depends(require_tenant_auth)):
    """Tenant admin view: is the (platform-wide) audit chain intact?

    Only {"ok": bool}. The chain spans every tenant, so counts, sequence
    numbers and hashes would reveal other tenants' activity volume; they are
    on the operator endpoint. Agent keys get 403: each call scans the whole
    log, which an agent must not be able to trigger at will.
    """
    _flush_denials()
    return {"ok": audit.verify_chain()["ok"]}


@app.get("/ops/audit/verify")
def ops_verify_audit(
    after_seq: int = Query(0, ge=0),
    anchor_hash: str | None = None,
    auth=Depends(require_operator_auth),
):
    """Operator view: full result (checked, head_seq, head_hash or
    broken_at_seq). Whole chain by default; for an incremental check pass
    after_seq plus the head_hash kept from an earlier run (never read back
    from the log). Keep checkpoints where the Hub cannot write."""
    _flush_denials()
    try:
        return audit.verify_chain(after_seq=after_seq, anchor_hash=anchor_hash)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/ops/audit")
def ops_list_audit(
    limit: int = Query(100, ge=1, le=AUDIT_PAGE_MAX),
    actor: str | None = None,
    auth=Depends(require_operator_auth),
):
    """Operator view of the audit log across tenants, newest first. Optional
    actor filter, e.g. sys:unauthenticated for refused unauthenticated
    requests, which no tenant can read."""
    _flush_denials()
    return audit.list_audit(limit, actor=actor)

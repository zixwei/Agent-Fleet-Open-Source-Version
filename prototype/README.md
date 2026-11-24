# Agent Fleet Hub — Stage 0 prototype (deliverable #1)

Runnable minimal technical prototype for the Agent Fleet cross-vendor agent
collaboration platform (plan v0.2.6 §25 #1). Modular monolith (§4): six
logical modules as packages, one deployable process.

## What it does

- **Agent Registry** — manual registration, admin approval (Pending → Active).
- **Orchestration** — task lifecycle: submitted → working → completed / failed / cancelled. No silent task loss.
- **Message Bus** — unified envelope (plan v0.2.6) with platform idempotency
  keys (`tenant:sender:task:event:message`). A duplicate delivery is a no-op:
  no duplicate side effects (principle 5).
- **Audit log** — append-only, hash-chained. `/audit/verify` recomputes it.
- **Identity** — API key per tenant (Stage 0 placeholder; full auth in §7).

## Run (no Docker needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
AI_FLEET_API_KEY=dev-key-change-me .venv/bin/uvicorn app.main:app --port 8000
```

## Run (reference deployment, plan v0.2.6 §23)

```bash
docker compose up --build
```

This starts the API plus PostgreSQL 18.7 and Valkey 9.1.2 (pinned versions in
`docker-compose.yml`; frozen in `docs/ENVIRONMENT.md`). The prototype stores
data in SQLite and uses an in-process bus by default; the Postgres and Valkey
adapters are the next step, not yet wired.

## Try it

```bash
KEY=dev-key-change-me
curl -s localhost:8000/health
curl -s -X POST localhost:8000/agents -H "X-API-Key: $KEY" \
  -d '{"agent_id":"agent-a","name":"Agent A","endpoint":"http://a.local","capabilities":["review"]}'
curl -s -X POST localhost:8000/agents/agent-a/approve -H "X-API-Key: $KEY"
curl -s -X POST localhost:8000/tasks -H "X-API-Key: $KEY" \
  -d '{"agent_id":"agent-a","title":"review the plan"}'
# then POST /tasks/{id}/messages, GET /tasks/{id}, GET /audit/verify
```

## Test

```bash
.venv/bin/python -m pytest tests/ -q
```

## Fixes (Stage 0 baseline, Cue T01/T08)

- **T01 agent identity (formal end-to-end)** — agent credentials
  (`AI_FLEET_AGENT_API_KEYS`: key -> `{agent_id, tenant_id}`). Each
  agent has its own credential, never shared between agents; a credential
  is live only while its agent is registered and Active (suspending the
  agent revokes it). `POST /tasks/{id}/messages/{message_id}/receipt`
  now requires an agent credential: the receipt identity is derived from
  the authenticated principal, never trusted from the request-body field
  (a body value that disagrees is 400). A tenant key alone cannot receipt
  (401), another agent's credential cannot receipt the message (403),
  revoked/suspended/unregistered credentials are 401, cross-tenant is
  404. Sending with an agent credential binds the sender to that agent
  (403 on mismatch). Verified live by two independent agent processes,
  each with its own credential, exchanging 100 rounds over HTTP:
  200/200 messages, 200/200 receipts (reproduced by
  `prototype/tests/t01_live_two_process.py`: 100/100 rounds PASS,
  audit chain verified).
- **Audit chain under concurrency** — `record()` read the previous hash
  and inserted the entry in two separate connections, so concurrent
  appends forked the chain (`/audit/verify` broke at seq 59 in the
  two-process test). The append is now one BEGIN IMMEDIATE transaction
  (read prev + insert), with a regression test hammering it from 8
  threads x 25 appends.
- **T01 delivery receipts** — `POST
  /tasks/{id}/messages/{message_id}/receipt` with
  `{"recipient_agent_id": "..."}` lets the receiving agent confirm
  delivery. Routing metadata only proves the hub stored the message; the
  receipt proves the receiving end got it. A duplicate receipt is a
  no-op returning the stored receipt (`stored: false`). Unknown message,
  message on another task, or cross-tenant message return 404; a caller
  that is not the message's recipient returns 400. `GET
  /tasks/{id}/messages` carries each message's `receipt` (null if none).
  Backed by a `message_receipts` table; receipts are audit-logged as
  `message.receipt` (duplicates as `message.receipt.duplicate`).
- **T01 reply routing** — `POST /tasks/{id}/messages` takes
  an optional `recipient_agent_id`. A reply from the task's agent can now
  be routed back to the other party; previously the recipient was hardcoded
  to the task's agent, so every reply went to the sender itself. Omitting
  the field routes implicitly: a message from anyone other than the task's
  agent goes to the task's agent, and a reply from the task's agent goes
  back to the other party on the task (the sender of the most recent
  message not from the task's agent). Sender and recipient
  must both be registered agents (400 otherwise).
- **T08 cross-tenant isolation** — every task object endpoint (`GET`
  `/tasks/{id}`, status, messages) checks the task belongs to the caller's
  tenant; a cross-tenant id returns 404 (never 403, so existence is not
  leaked). Task list and audit list are tenant-scoped too.
- **Multi-tenant keys** — set `AI_FLEET_TENANT_API_KEYS` to a JSON map of
  API key to tenant id, e.g. `{"key-a": "tenant-a", "key-b": "tenant-b"}`,
  to test with distinct tenants. Without it, the single `AI_FLEET_API_KEY`
  maps to `DEFAULT_TENANT` as before.
- **GET /audit fixed** — it previously called a missing function; it now
  returns the caller's own tenant entries.

## What is NOT claimed

- A2A / MCP compatibility is **not** implemented or verified here. That is
  deliverables #2 / #3 (wuse). This prototype must not be presented as
  A2A- or MCP-compatible.
- The Postgres / Valkey adapters are **not** wired yet; the reference
  deployment runs them, but the API still uses SQLite + in-process bus.
- No performance numbers are claimed; none are measured yet (plan §22).

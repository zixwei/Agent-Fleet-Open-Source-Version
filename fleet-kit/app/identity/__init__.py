"""Module 1 - Identity & Authorization (minimal, Stage 0).

Two credential kinds, both via header X-API-Key:

- Tenant key: one API key per tenant (AI_FLEET_TENANT_API_KEYS maps key
  -> tenant id; when unset, the single AI_FLEET_API_KEY maps to
  DEFAULT_TENANT). Identifies a tenant only, never an agent.
- Agent key: one credential per agent (AI_FLEET_AGENT_API_KEYS maps key
  -> {"agent_id": ..., "tenant_id": ...}). Identifies the agent AND its
  tenant. An agent key is valid only while its agent is registered and
  Active; suspending the agent revokes the credential. No raw credential
  is ever shared between agents (principle 3).

require_api_key accepts either kind and returns
{"tenant_id", "agent_id" (None for tenant keys), "auth_kind"}.
require_agent_auth accepts only agent keys; endpoints that must prove
which agent acted (delivery receipts, formal T01) use it, so the acting
identity is derived from the authenticated principal, never trusted from
a request-body field.

Every resolver records what it learned on request.state so the audit
layer can attribute a denied request (T09):

- request.state.auth: the authenticated principal (set on success)
- request.state.claimed: {"tenant_id", "agent_id"} named by an agent key
  that was refused (suspended, unregistered, or not in its own tenant),
  so the owning tenant can see a revoked agent still knocking.
The presented key itself is never stored or logged.

Tenant ids and system actors (reserved-name fix):

- A tenant id must match TENANT_ID_PATTERN (letters, digits, ".", "_",
  "-"; no ":"). Audit entries are scoped by actor = tenant id, so a tenant
  configured as "unauthenticated" used to read every unauthenticated denial.
  System actors now carry the "sys:" prefix, which no tenant id can have.
  A key whose tenant id is malformed is refused (fail closed) and the
  startup check reports it.
- Operator key (AI_FLEET_OPERATOR_API_KEY, optional): platform-wide audit
  views (/ops/...). It is a separate credential: it never passes tenant or
  agent endpoints, and tenant/agent keys never pass operator endpoints.

This is a placeholder for the full auth model (plan v0.2.6 §7): per-agent
credentials, scoped tokens, and human approval for high-risk ops.
"""
import hmac
import json
import logging
import os
import re

from fastapi import Header, HTTPException, Request

from app import config

logger = logging.getLogger("agent_fleet_hub.identity")

TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SYSTEM_ACTOR_PREFIX = "sys:"
UNAUTHENTICATED_ACTOR = "sys:unauthenticated"
OPERATOR_ACTOR = "sys:operator"
OPERATOR_KEY_ENV = "AI_FLEET_OPERATOR_API_KEY"
MIN_OPERATOR_KEY_LENGTH = 24


def valid_tenant_id(tenant_id) -> bool:
    return isinstance(tenant_id, str) and bool(TENANT_ID_PATTERN.match(tenant_id))


def _key_map(var: str) -> dict:
    raw = os.environ.get(var, "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tenant_key_map() -> dict:
    return _key_map("AI_FLEET_TENANT_API_KEYS")


def _agent_key_map() -> dict:
    return _key_map("AI_FLEET_AGENT_API_KEYS")


def _operator_key_problems(key: str) -> list:
    problems = []
    if len(key) < MIN_OPERATOR_KEY_LENGTH:
        problems.append(f"{OPERATOR_KEY_ENV} must be at least {MIN_OPERATOR_KEY_LENGTH} characters")
    if key == config.API_KEY or key in _tenant_key_map() or key in _agent_key_map():
        problems.append(f"{OPERATOR_KEY_ENV} must differ from every tenant and agent key")
    return problems


def _operator_key() -> str:
    """The configured operator key, or "" when unset or unusable (fail closed)."""
    key = os.environ.get(OPERATOR_KEY_ENV, "")
    if not key:
        return ""
    if _operator_key_problems(key):
        logger.error("%s is set but unusable; operator endpoints are disabled", OPERATOR_KEY_ENV)
        return ""
    return key


def validate_key_config() -> list:
    """Problems in the key configuration (startup check). Messages name tenant
    ids and settings, never key values."""
    problems = []
    if not valid_tenant_id(config.DEFAULT_TENANT):
        problems.append(f"AI_FLEET_TENANT {config.DEFAULT_TENANT!r} is not a valid tenant id")
    for tenant_id in _tenant_key_map().values():
        if not valid_tenant_id(tenant_id):
            problems.append(f"AI_FLEET_TENANT_API_KEYS: invalid tenant id {tenant_id!r}")
    for entry in _agent_key_map().values():
        tenant_id = entry.get("tenant_id") if isinstance(entry, dict) else None
        if not valid_tenant_id(tenant_id):
            problems.append(f"AI_FLEET_AGENT_API_KEYS: invalid tenant id {tenant_id!r}")
    key = os.environ.get(OPERATOR_KEY_ENV, "")
    if key:
        problems.extend(_operator_key_problems(key))
    return problems


def _resolve(request: Request, x_api_key: str | None) -> dict:
    # Agent keys first: they carry the acting identity.
    entry = _agent_key_map().get(x_api_key)
    if entry is not None:
        agent_id = entry.get("agent_id") if isinstance(entry, dict) else None
        tenant_id = entry.get("tenant_id") if isinstance(entry, dict) else None
        if not agent_id or not valid_tenant_id(tenant_id):
            if tenant_id:
                logger.error("agent key with malformed tenant id %r refused", tenant_id)
            raise HTTPException(status_code=401, detail="invalid agent credential")
        # The credential is live only while its agent is registered in the
        # credential's own tenant and Active; suspending the agent revokes
        # it. (Looking the agent up globally let a key that names another
        # tenant's agent pass as live.)
        from app import registry

        agent = registry.get_agent(agent_id, tenant_id=tenant_id)
        if agent is None or agent["status"] != "Active":
            request.state.claimed = {"tenant_id": tenant_id, "agent_id": agent_id}
            raise HTTPException(status_code=401, detail="invalid agent credential")
        auth = {"tenant_id": tenant_id, "agent_id": agent_id, "auth_kind": "agent"}
    else:
        tenant_id = _tenant_key_map().get(x_api_key)
        if tenant_id is not None and not valid_tenant_id(tenant_id):
            logger.error("tenant key with malformed tenant id %r refused", tenant_id)
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        if tenant_id is not None:
            auth = {"tenant_id": tenant_id, "agent_id": None, "auth_kind": "tenant"}
        elif _tenant_key_map() or x_api_key != config.API_KEY:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        else:
            auth = {"tenant_id": config.DEFAULT_TENANT, "agent_id": None, "auth_kind": "tenant"}
    request.state.auth = auth
    return auth


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)):
    return _resolve(request, x_api_key)


def require_agent_auth(request: Request, x_api_key: str | None = Header(default=None)):
    """Only an agent credential passes. Used where the acting agent must
    be proven (delivery receipts): the identity comes from the credential,
    never from a request-body field."""
    auth = _resolve(request, x_api_key)
    if auth.get("agent_id") is None:
        raise HTTPException(status_code=401, detail="agent credential required")
    return auth


def require_tenant_auth(request: Request, x_api_key: str | None = Header(default=None)):
    """Tenant/admin key only. Agent keys cannot approve/suspend/register."""
    auth = _resolve(request, x_api_key)
    if auth.get("agent_id") is not None:
        raise HTTPException(status_code=403, detail="tenant admin credential required")
    return auth


def require_operator_auth(request: Request, x_api_key: str | None = Header(default=None)):
    """Operator key only (platform-wide audit views). Compared in constant time."""
    expected = _operator_key()
    if (
        not expected
        or not x_api_key
        or not hmac.compare_digest(x_api_key.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(status_code=401, detail="operator credential required")
    auth = {"tenant_id": OPERATOR_ACTOR, "agent_id": None, "auth_kind": "operator"}
    request.state.auth = auth
    return auth

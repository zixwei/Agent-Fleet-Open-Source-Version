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

This is a placeholder for the full auth model (plan v0.2.6 §7): per-agent
credentials, scoped tokens, and human approval for high-risk ops.
"""
import json
import os

from fastapi import Header, HTTPException

from app import config


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


def require_api_key(x_api_key: str | None = Header(default=None)):
    # Agent keys first: they carry the acting identity.
    entry = _agent_key_map().get(x_api_key)
    if entry is not None:
        agent_id = entry.get("agent_id") if isinstance(entry, dict) else None
        tenant_id = entry.get("tenant_id") if isinstance(entry, dict) else None
        if not agent_id or not tenant_id:
            raise HTTPException(status_code=401, detail="invalid agent credential")
        # The credential is live only while its agent is registered and
        # Active; suspending the agent revokes it.
        from app import registry

        agent = registry.get_agent(agent_id)
        if agent is None or agent["status"] != "Active":
            raise HTTPException(status_code=401, detail="invalid agent credential")
        return {"tenant_id": tenant_id, "agent_id": agent_id, "auth_kind": "agent"}
    tenant_id = _tenant_key_map().get(x_api_key)
    if tenant_id is not None:
        return {"tenant_id": tenant_id, "agent_id": None, "auth_kind": "tenant"}
    if _tenant_key_map():
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    if x_api_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return {"tenant_id": config.DEFAULT_TENANT, "agent_id": None, "auth_kind": "tenant"}


def require_agent_auth(x_api_key: str | None = Header(default=None)):
    """Only an agent credential passes. Used where the acting agent must
    be proven (delivery receipts): the identity comes from the credential,
    never from a request-body field."""
    auth = require_api_key(x_api_key)
    if auth.get("agent_id") is None:
        raise HTTPException(status_code=401, detail="agent credential required")
    return auth

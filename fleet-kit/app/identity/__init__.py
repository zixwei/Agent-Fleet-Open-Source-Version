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

Hash-only key storage (B2, app/identity/keyhash.py): every key setting
(AI_FLEET_TENANT_API_KEYS, AI_FLEET_AGENT_API_KEYS, AI_FLEET_API_KEY,
AI_FLEET_OPERATOR_API_KEY) holds the key's hash, "sha256:<64 hex>" or
"hmac-sha256:<64 hex>" (pepper: AI_FLEET_KEY_PEPPER), never the key. A
presented key is hashed and compared in constant time against every
configured entry. A configuration that still holds a plaintext key is
refused at startup (validate_key_config names the variable, never the
value); at request time a plaintext entry is never matched, so it fails
closed even where the startup check did not run.

Brute-force backoff (B2, app/identity/attempts.py): repeated failures of
the same presented key, or many failures from one source, are answered 429
(Retry-After) before the key is checked. Keys that authenticated recently
are exempt from the source scope, so an attacker behind a shared reverse
proxy cannot lock out the legitimate clients behind it.

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
import json
import logging
import math
import os
import re

from fastapi import Header, HTTPException, Request

from app import config
from app.identity import keyhash
from app.identity.attempts import AuthAttemptLimiter

logger = logging.getLogger("agent_fleet_hub.identity")

TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SYSTEM_ACTOR_PREFIX = "sys:"
UNAUTHENTICATED_ACTOR = "sys:unauthenticated"
OPERATOR_ACTOR = "sys:operator"
OPERATOR_KEY_ENV = "AI_FLEET_OPERATOR_API_KEY"
API_KEY_ENV = "AI_FLEET_API_KEY"
TENANT_KEYS_ENV = "AI_FLEET_TENANT_API_KEYS"
AGENT_KEYS_ENV = "AI_FLEET_AGENT_API_KEYS"
MIN_OPERATOR_KEY_LENGTH = 24
# Appended to every "plaintext key" startup error. Names what to do, never
# a key value.
MIGRATION_HINT = (
    "plaintext keys are refused; re-issue the key(s) and configure only their "
    "hashes ('sha256:<64 hex>' or 'hmac-sha256:<64 hex>'), e.g. with "
    "'python3 scripts/gen_keys.py new' - see fleet-kit-docs/04-identity-tenancy.md"
)

# Process-wide, like main.denial_limiter. Tests reset it (tests/conftest.py).
attempt_limiter = AuthAttemptLimiter(
    window_seconds=config.AUTH_FAIL_WINDOW_SECONDS,
    per_credential=config.AUTH_FAIL_PER_CREDENTIAL,
    per_source=config.AUTH_FAIL_PER_SOURCE,
    backoff_base_seconds=config.AUTH_BACKOFF_BASE_SECONDS,
    backoff_max_seconds=config.AUTH_BACKOFF_MAX_SECONDS,
    known_good_ttl_seconds=config.AUTH_KNOWN_GOOD_TTL_SECONDS,
    max_tracked=config.AUTH_MAX_TRACKED,
)


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
    return _key_map(TENANT_KEYS_ENV)


def _agent_key_map() -> dict:
    return _key_map(AGENT_KEYS_ENV)


def _pepper() -> str:
    return os.environ.get(keyhash.PEPPER_ENV, "")


def _hashed_entries(mapping: dict) -> list:
    """(normalized hash, value) for the hashed entries of a key map. A
    plaintext entry is left out: it can never match (fail closed)."""
    return [(keyhash.normalize(k), v) for k, v in mapping.items() if keyhash.is_hashed(k)]


def _single_key_hash() -> str:
    return keyhash.normalize(config.API_KEY) if keyhash.is_hashed(config.API_KEY) else ""


def _client_key_hashes() -> set:
    """Every configured tenant/agent key hash (operator reuse check)."""
    hashes = {h for h, _ in _hashed_entries(_tenant_key_map())}
    hashes |= {h for h, _ in _hashed_entries(_agent_key_map())}
    single = _single_key_hash()
    if single:
        hashes.add(single)
    return hashes


def _map_problems(var: str) -> list:
    raw = os.environ.get(var, "")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return [f"{var} is not valid JSON"]
    if not isinstance(parsed, dict):
        return [f"{var} must be a JSON object mapping key hashes to entries"]
    plaintext = sum(1 for k in parsed if not keyhash.is_hashed(k))
    if plaintext:
        return [f"{var}: {plaintext} of {len(parsed)} entries are not in hashed form; {MIGRATION_HINT}"]
    return []


def _operator_key_problems(value: str) -> list:
    """Problems with the operator key setting. Messages name settings only."""
    problems = []
    others = _client_key_hashes()
    if keyhash.is_hashed(value):
        # Only the hash is known here, so the length rule is enforced where
        # keys are issued (scripts/gen_keys.py); reuse is still detectable.
        if keyhash.normalize(value) in others:
            problems.append(f"{OPERATOR_KEY_ENV} must differ from every tenant and agent key")
        return problems
    problems.append(f"{OPERATOR_KEY_ENV} is not in hashed form; {MIGRATION_HINT}")
    # Still say what else is wrong with the value, so the re-issued key does
    # not repeat the mistake. Both checks only look at the value; it is never
    # echoed.
    if len(value) < MIN_OPERATOR_KEY_LENGTH:
        problems.append(f"{OPERATOR_KEY_ENV} must be at least {MIN_OPERATOR_KEY_LENGTH} characters")
    if any(h in others for h in keyhash.presented_hashes(value, _pepper()).values()):
        problems.append(f"{OPERATOR_KEY_ENV} must differ from every tenant and agent key")
    return problems


def _operator_key_hash() -> str:
    """The configured operator key hash, or "" when unset or unusable (fail closed)."""
    value = os.environ.get(OPERATOR_KEY_ENV, "")
    if not value:
        return ""
    unusable = bool(_operator_key_problems(value)) or (
        keyhash.scheme_of(value) == keyhash.SCHEME_HMAC and not _pepper()
    )
    if unusable:
        logger.error("%s is set but unusable; operator endpoints are disabled", OPERATOR_KEY_ENV)
        return ""
    return keyhash.normalize(value)


def _uses_hmac() -> bool:
    values = list(_tenant_key_map()) + list(_agent_key_map())
    values += [config.API_KEY, os.environ.get(OPERATOR_KEY_ENV, "")]
    return any(keyhash.is_hashed(v) and keyhash.scheme_of(v) == keyhash.SCHEME_HMAC for v in values)


def validate_key_config() -> list:
    """Problems in the key configuration (startup check). Messages name tenant
    ids and settings, never key values."""
    problems = []
    if not valid_tenant_id(config.DEFAULT_TENANT):
        problems.append(f"AI_FLEET_TENANT {config.DEFAULT_TENANT!r} is not a valid tenant id")
    problems.extend(_map_problems(TENANT_KEYS_ENV))
    problems.extend(_map_problems(AGENT_KEYS_ENV))
    for tenant_id in _tenant_key_map().values():
        if not valid_tenant_id(tenant_id):
            problems.append(f"AI_FLEET_TENANT_API_KEYS: invalid tenant id {tenant_id!r}")
    for entry in _agent_key_map().values():
        tenant_id = entry.get("tenant_id") if isinstance(entry, dict) else None
        if not valid_tenant_id(tenant_id):
            problems.append(f"AI_FLEET_AGENT_API_KEYS: invalid tenant id {tenant_id!r}")
    if config.API_KEY and not keyhash.is_hashed(config.API_KEY):
        problems.append(f"{API_KEY_ENV} is not in hashed form; {MIGRATION_HINT}")
    pepper = _pepper()
    if pepper and len(pepper) < keyhash.MIN_PEPPER_LENGTH:
        problems.append(f"{keyhash.PEPPER_ENV} must be at least {keyhash.MIN_PEPPER_LENGTH} characters")
    if not pepper and _uses_hmac():
        problems.append(f"hmac-sha256 key hashes are configured but {keyhash.PEPPER_ENV} is not set")
    key = os.environ.get(OPERATOR_KEY_ENV, "")
    if key:
        problems.extend(_operator_key_problems(key))
    return problems


def _source(request: Request) -> str:
    # The direct peer, as in main._source: X-Forwarded-For is not trusted
    # (client-controlled). Behind a proxy this is the proxy for everyone,
    # which is why the source scope exempts known-good keys (attempts.py).
    return request.client.host if request.client else "unknown"


def _too_many(wait: float) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="too many failed authentication attempts; retry later",
        headers={"Retry-After": str(max(1, math.ceil(wait)))},
    )


def _guarded(request: Request, realm: str, x_api_key: str, check):
    """Run check(request, x_api_key) under the brute-force backoff.

    A 401 from check is a failed attempt and is counted; anything else it
    raises (e.g. 403) happened after the key was accepted. While the key or
    its source is in backoff, the key is not checked at all: 429.
    """
    fingerprint = attempt_limiter.fingerprint(realm, x_api_key)
    source = _source(request)
    wait = attempt_limiter.check(fingerprint, source)
    if wait > 0:
        raise _too_many(wait)
    try:
        auth = check(request, x_api_key)
    except HTTPException as exc:
        if exc.status_code == 401:
            attempt_limiter.failure(fingerprint, source)
        raise
    attempt_limiter.success(fingerprint)
    return auth


def _check_client_key(request: Request, x_api_key: str) -> dict:
    presented = keyhash.presented_hashes(x_api_key, _pepper())
    # All three lookups always run (no short-circuit), each over every entry.
    entry = keyhash.find(presented, _hashed_entries(_agent_key_map()))
    tenant_id = keyhash.find(presented, _hashed_entries(_tenant_key_map()))
    single = _single_key_hash()
    single_ok = keyhash.find(presented, [(single, True)] if single else []) is True
    # Agent keys first: they carry the acting identity.
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
        if tenant_id is not None and not valid_tenant_id(tenant_id):
            logger.error("tenant key with malformed tenant id %r refused", tenant_id)
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        if tenant_id is not None:
            auth = {"tenant_id": tenant_id, "agent_id": None, "auth_kind": "tenant"}
        elif _tenant_key_map() or not single_ok:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        else:
            auth = {"tenant_id": config.DEFAULT_TENANT, "agent_id": None, "auth_kind": "tenant"}
    request.state.auth = auth
    return auth


def _resolve(request: Request, x_api_key: str | None) -> dict:
    if not x_api_key:
        # Nothing presented, nothing guessed: refused, not counted.
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return _guarded(request, "client", x_api_key, _check_client_key)


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


def _check_operator_key(request: Request, x_api_key: str) -> dict:
    expected = _operator_key_hash()
    presented = keyhash.presented_hashes(x_api_key, _pepper())
    if not expected or keyhash.find(presented, [(expected, True)]) is not True:
        raise HTTPException(status_code=401, detail="operator credential required")
    auth = {"tenant_id": OPERATOR_ACTOR, "agent_id": None, "auth_kind": "operator"}
    request.state.auth = auth
    return auth


def require_operator_auth(request: Request, x_api_key: str | None = Header(default=None)):
    """Operator key only (platform-wide audit views). Hash compared in
    constant time; failures share the brute-force backoff (own realm, so a
    tenant key that fails here is not backed off on tenant endpoints)."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="operator credential required")
    return _guarded(request, "operator", x_api_key, _check_operator_key)

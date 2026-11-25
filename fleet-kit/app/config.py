"""Configuration. All values come from the environment with sane defaults.

Reference environment is frozen in docs/ENVIRONMENT.md (plan v0.2.6 §23).
"""
import os

DATABASE_URL = os.environ.get("AI_FLEET_DB", "sqlite:///fleet.db")
DATABASE_PATH = DATABASE_URL.removeprefix("sqlite:///")
# Single tenant key (used only when AI_FLEET_TENANT_API_KEYS is unset). Like
# every key setting it holds the key's HASH ("sha256:<64 hex>" or
# "hmac-sha256:<64 hex>", see app/identity/keyhash.py), never the key.
# No default: unset means no single key is accepted (fail closed). The old
# guessable built-in default is gone; scripts/gen_keys.py issues random keys.
API_KEY = os.environ.get("AI_FLEET_API_KEY", "")
HOST = os.environ.get("AI_FLEET_HOST", "127.0.0.1")
PORT = int(os.environ.get("AI_FLEET_PORT", "8000"))
# Tenant used by the prototype when callers do not carry one.
DEFAULT_TENANT = os.environ.get("AI_FLEET_TENANT", "default")


def _int_env(name: str, default: int, minimum: int) -> int:
    """Integer setting; a malformed or out-of-range value fails at startup
    instead of silently falling back."""
    raw = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


# Denial-flood protection (app/observability/denials.py). Per window, at
# most AI_FLEET_DENIAL_MAX_INDIVIDUAL refused requests get their own audit
# entry; the rest are counted into request.denied.summary entries.
DENIAL_WINDOW_SECONDS = _int_env("AI_FLEET_DENIAL_WINDOW_SECONDS", 60, 1)
DENIAL_PER_SOURCE = _int_env("AI_FLEET_DENIAL_PER_SOURCE", 1, 0)
DENIAL_PER_PRINCIPAL = _int_env("AI_FLEET_DENIAL_PER_PRINCIPAL", 5, 0)
DENIAL_MAX_SOURCES = _int_env("AI_FLEET_DENIAL_MAX_SOURCES", 20, 0)
DENIAL_MAX_INDIVIDUAL = _int_env("AI_FLEET_DENIAL_MAX_INDIVIDUAL", 100, 0)

# Brute-force backoff (app/identity/attempts.py). Unlike the denial settings
# above, these DO refuse requests (429 + Retry-After) - but only for keys that
# keep failing, and only unproven keys from a noisy source; keys that have
# authenticated recently are never blocked by the source scope, so clients
# behind a shared reverse proxy cannot be locked out by an attacker there.
AUTH_FAIL_WINDOW_SECONDS = _int_env("AI_FLEET_AUTH_FAIL_WINDOW_SECONDS", 300, 1)
AUTH_FAIL_PER_CREDENTIAL = _int_env("AI_FLEET_AUTH_FAIL_PER_CREDENTIAL", 5, 1)
AUTH_FAIL_PER_SOURCE = _int_env("AI_FLEET_AUTH_FAIL_PER_SOURCE", 500, 1)
AUTH_BACKOFF_BASE_SECONDS = _int_env("AI_FLEET_AUTH_BACKOFF_BASE_SECONDS", 1, 1)
AUTH_BACKOFF_MAX_SECONDS = _int_env("AI_FLEET_AUTH_BACKOFF_MAX_SECONDS", 300, 1)
AUTH_KNOWN_GOOD_TTL_SECONDS = _int_env("AI_FLEET_AUTH_KNOWN_GOOD_TTL_SECONDS", 86400, 1)
AUTH_MAX_TRACKED = _int_env("AI_FLEET_AUTH_MAX_TRACKED", 10000, 1)

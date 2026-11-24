"""Configuration. All values come from the environment with sane defaults.

Reference environment is frozen in docs/ENVIRONMENT.md (plan v0.2.6 §23).
"""
import os

DATABASE_URL = os.environ.get("AI_FLEET_DB", "sqlite:///fleet.db")
DATABASE_PATH = DATABASE_URL.removeprefix("sqlite:///")
API_KEY = os.environ.get("AI_FLEET_API_KEY", "dev-key-change-me")
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

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

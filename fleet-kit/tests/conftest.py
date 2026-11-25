"""Shared test setup.

The denial limiter in app.main and the auth attempt limiter in app.identity
are process-wide state (counts per window), so without a reset, denials or
failed logins from one test would use up the budgets of the next. Every
test starts with empty limiters on the real clock.

Keys are configured in hashed form only (B2): tests present the plaintext
key in X-API-Key and configure app.identity.keyhash.hash_key(...) of it.
"""
import hashlib
import os
import sys
import time

# Set the test API key before any test module is collected: test_denial_flood
# imports app code at module level, which freezes config.API_KEY at import
# time. Without this, collection order decides the key and smoke tests 401.
# Same value as keyhash.hash_key("smoke-test-key"); app is not importable yet.
os.environ["AI_FLEET_API_KEY"] = "sha256:" + hashlib.sha256(b"smoke-test-key").hexdigest()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(autouse=True)
def _fresh_denial_limiter():
    from app import main

    main.denial_limiter.reset()
    main.denial_limiter.clock = time.time
    yield
    main.denial_limiter.reset()
    main.denial_limiter.clock = time.time


@pytest.fixture(autouse=True)
def _fresh_attempt_limiter():
    from app import identity

    identity.attempt_limiter.reset()
    identity.attempt_limiter.clock = time.monotonic
    yield
    identity.attempt_limiter.reset()
    identity.attempt_limiter.clock = time.monotonic

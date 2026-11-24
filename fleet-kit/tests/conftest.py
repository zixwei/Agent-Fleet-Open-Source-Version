"""Shared test setup.

The denial limiter in app.main is process-wide state (counts per window), so
without a reset, denials from one test would use up the budgets of the next.
Every test starts with an empty limiter on the real clock.
"""
import os
import sys
import time

# Set the test API key before any test module is collected: test_denial_flood
# imports app code at module level, which freezes config.API_KEY at import
# time. Without this, collection order decides the key and smoke tests 401.
os.environ["AI_FLEET_API_KEY"] = "smoke-test-key"

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

"""Regression test: concurrent audit appends must not fork the hash chain.

Found by the formal T01 two-process live test: two agent processes
sending/receipting concurrently broke /audit/verify at seq 59. Root
cause: record() read the previous hash and inserted the entry in two
separate connections, so two concurrent appends read the same prev_hash
and forked the chain. The append is now one BEGIN IMMEDIATE transaction.

Ported from prototype/tests; verify_chain now always checks the whole chain.

Run:  python -m pytest tests/test_audit_concurrency.py -q
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "audit_conc.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield


def test_concurrent_appends_keep_chain_valid():
    from app import observability as audit

    def append_batch(n):
        for i in range(n):
            audit.record("tenant-a", "test.append", f"target-{n}-{i}", {"i": i})

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(append_batch, 25) for _ in range(8)]
        for f in futures:
            f.result()

    result = audit.verify_chain()  # whole chain
    assert result["ok"] is True, result
    assert result["checked"] == 200

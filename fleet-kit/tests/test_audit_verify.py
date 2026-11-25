"""Audit chain verification covers the whole log (PR #1 follow-up).

The Codex fix anchored verification to the first entry of a fixed
1000-entry window, which meant /audit/verify only ever checked entries
1..1000: a tampered entry after that still reported ok=True. These tests
pin the corrected behaviour:

- the default verification walks the whole chain from GENESIS;
- tampering anywhere (inside or beyond the first 1000) is reported;
- incremental verification needs a trusted anchor supplied by the caller
  (never read back from the log), and a log that ends before a kept
  checkpoint is reported as truncated.

Run:  python -m pytest tests/test_audit_verify.py -q
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.identity.keyhash import hash_key, hash_key_map

N = 1200  # more than the old 1000-entry window


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "audit_verify.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield db_path


def _fill(n):
    from app import observability as audit

    for i in range(n):
        audit.record("tenant-a", "test.append", f"target-{i}", {"i": i})


def _tamper(db_path, seq):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE audit_log SET detail = ? WHERE seq = ?",
        (json.dumps({"i": "TAMPERED"}), seq),
    )
    conn.commit()
    conn.close()


def test_whole_chain_checked_beyond_1000():
    from app import observability as audit

    _fill(N)
    result = audit.verify_chain()
    assert result["ok"] is True, result
    assert result["checked"] == N
    assert result["head_seq"] == N


def test_tamper_after_first_1000_is_detected(fresh_db):
    from app import observability as audit

    _fill(N)
    _tamper(fresh_db, 1100)
    result = audit.verify_chain()
    assert result["ok"] is False, result
    assert result["broken_at_seq"] == 1100


def test_tamper_inside_first_window_is_detected(fresh_db):
    from app import observability as audit

    _fill(50)
    _tamper(fresh_db, 5)
    result = audit.verify_chain()
    assert result == {"ok": False, "broken_at_seq": 5, "checked": 4}


def test_empty_log_verifies():
    from app import observability as audit

    assert audit.verify_chain() == {
        "ok": True, "checked": 0, "head_seq": 0, "head_hash": "GENESIS"
    }


def test_incremental_from_trusted_checkpoint():
    from app import observability as audit

    _fill(30)
    first = audit.verify_chain()
    assert first["ok"] is True
    _fill(10)
    inc = audit.verify_chain(after_seq=first["head_seq"], anchor_hash=first["head_hash"])
    assert inc["ok"] is True, inc
    assert inc["checked"] == 10
    assert inc["head_seq"] == 40


def test_incremental_with_wrong_anchor_fails():
    from app import observability as audit

    _fill(30)
    first = audit.verify_chain()
    _fill(5)
    bad = audit.verify_chain(after_seq=first["head_seq"], anchor_hash="0" * 64)
    assert bad["ok"] is False
    # The checkpoint row itself no longer matches the kept anchor.
    assert bad["broken_at_seq"] == first["head_seq"]
    # Even with nothing appended after the checkpoint (previously ok=True).
    head = audit.verify_chain()
    assert audit.verify_chain(after_seq=head["head_seq"], anchor_hash="0" * 64)["ok"] is False


def test_incremental_requires_anchor():
    from app import observability as audit

    _fill(3)
    with pytest.raises(ValueError):
        audit.verify_chain(after_seq=2)
    with pytest.raises(ValueError):
        audit.verify_chain(after_seq=-1)


def test_log_shorter_than_checkpoint_is_reported(fresh_db):
    """Cutting off the newest entries keeps the remaining chain valid; only
    a kept checkpoint reveals it."""
    from app import observability as audit

    _fill(20)
    checkpoint = audit.verify_chain()
    conn = sqlite3.connect(str(fresh_db))
    conn.execute("DELETE FROM audit_log WHERE seq > 15")
    conn.commit()
    conn.close()
    assert audit.verify_chain()["ok"] is True  # chain alone cannot tell
    cut = audit.verify_chain(
        after_seq=checkpoint["head_seq"], anchor_hash=checkpoint["head_hash"]
    )
    assert cut["ok"] is False
    assert cut["head_seq"] == 15


OPERATOR_KEY = "op-key-0123456789abcdef-xyz"


def test_verify_endpoint_tenant_admin_sees_only_ok(monkeypatch, fresh_db):
    """Tenant admins learn only whether the chain is intact: no counts, seq
    numbers or hashes of the platform-wide log."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(hash_key_map({"key-a": "tenant-a"})))
    from app.main import app

    client = TestClient(app)
    headers = {"X-API-Key": "key-a"}
    _fill(5)
    assert client.get("/audit/verify", headers=headers).json() == {"ok": True}
    # Old incremental parameters are ignored here (operator endpoint only).
    assert client.get("/audit/verify", params={"after_seq": 3}, headers=headers).json() == {"ok": True}
    _tamper(fresh_db, 2)
    assert client.get("/audit/verify", headers=headers).json() == {"ok": False}


def test_verify_endpoint_operator_gets_details(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(hash_key_map({"key-a": "tenant-a"})))
    monkeypatch.setenv("AI_FLEET_OPERATOR_API_KEY", hash_key(OPERATOR_KEY))
    from app.main import app

    client = TestClient(app)
    op = {"X-API-Key": OPERATOR_KEY}
    _fill(5)
    r = client.get("/ops/audit/verify", headers=op)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["checked"] == 5 and body["head_seq"] == 5
    r = client.get(
        "/ops/audit/verify",
        params={"after_seq": body["head_seq"], "anchor_hash": body["head_hash"]},
        headers=op,
    )
    assert r.json()["ok"] is True and r.json()["checked"] == 0
    assert client.get("/ops/audit/verify", params={"after_seq": 3}, headers=op).status_code == 400
    assert client.get("/ops/audit/verify", params={"after_seq": -1}, headers=op).status_code == 422

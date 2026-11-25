"""B2: brute-force protection for API keys.

1. Authentication-failure backoff (app/identity/attempts.py): repeated
   failures answer 429 + Retry-After before the key is checked; a success
   resets the credential scope; an attacker behind a shared reverse proxy
   cannot lock out legitimate clients behind it (known-good keys skip the
   source scope). DenialLimiter still only bounds the audit log.
2. Hash-only key storage (app/identity/keyhash.py): tenant, agent, single
   and operator keys are configured as hashes and compared in constant time;
   a plaintext key configuration is refused at startup with an error that
   names the variable but never the value.
3. Random demo keys (scripts/gen_keys.py): no guessable literals in
   mcp-host.example.json, config.py or demo.sh; every run gets fresh keys.

Part 1 tests the limiter alone with a fake clock; the rest goes through HTTP.

Run:  python -m pytest tests/test_b2_bruteforce.py -q
"""
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from app.identity import keyhash
from app.identity.attempts import AuthAttemptLimiter
from app.identity.keyhash import hash_key, hash_key_map

ROOT = Path(__file__).resolve().parent.parent

OPERATOR_KEY = "op-key-0123456789abcdef-xyz"
TENANT_KEYS = {"key-a": "tenant-a", "key-b": "tenant-b"}
AGENT_KEYS = {"key-worker": {"agent_id": "worker", "tenant_id": "tenant-a"}}
ADMIN = {"X-API-Key": "key-a"}
ADMIN_B = {"X-API-Key": "key-b"}
WORKER = {"X-API-Key": "key-worker"}
OP = {"X-API-Key": OPERATOR_KEY}
WRONG = {"X-API-Key": "wrong-key-guess-0001"}
PLAINTEXT_SECRET = "plaintext-secret-value-0123456789"


class FakeClock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- limiter


def _limiter(clock, **overrides):
    args = dict(
        window_seconds=300,
        per_credential=5,
        per_source=50,
        backoff_base_seconds=1,
        backoff_max_seconds=8,
        known_good_ttl_seconds=3600,
        max_tracked=1000,
    )
    args.update(overrides)
    return AuthAttemptLimiter(clock=clock, **args)


def test_credential_backoff_after_n_consecutive_failures():
    clock = FakeClock()
    lim = _limiter(clock)
    fp = lim.fingerprint("client", "guess")
    for _ in range(4):
        lim.failure(fp, "10.0.0.1")
        assert lim.check(fp, "10.0.0.1") == 0
    lim.failure(fp, "10.0.0.1")  # 5th consecutive failure
    assert lim.check(fp, "10.0.0.1") == 1
    # Progressive: after the block every failure blocks again, twice as long,
    # capped at backoff_max.
    waits = []
    for _ in range(6):
        clock.t += lim.check(fp, "10.0.0.1")
        assert lim.check(fp, "10.0.0.1") == 0
        lim.failure(fp, "10.0.0.1")
        waits.append(lim.check(fp, "10.0.0.1"))
    assert waits == [2, 4, 8, 8, 8, 8]


def test_success_resets_credential_scope():
    clock = FakeClock()
    lim = _limiter(clock)
    fp = lim.fingerprint("client", "typo-then-right")
    for _ in range(4):
        lim.failure(fp, "10.0.0.1")
    lim.success(fp)
    for _ in range(4):
        lim.failure(fp, "10.0.0.1")
    assert lim.check(fp, "10.0.0.1") == 0  # counter started again from 0


def test_block_is_forgiven_after_a_quiet_window():
    clock = FakeClock()
    lim = _limiter(clock)
    fp = lim.fingerprint("client", "x")
    for _ in range(5):
        lim.failure(fp, "s")
    assert lim.check(fp, "s") > 0
    clock.t += 1 + 300  # block over, then a whole window without failures
    for _ in range(4):
        lim.failure(fp, "s")
    assert lim.check(fp, "s") == 0  # threshold applies again, no strike left


def test_source_backoff_spares_known_good_credentials():
    """Shared proxy: one source address for everybody."""
    clock = FakeClock()
    lim = _limiter(clock, per_source=10)
    proxy = "10.9.9.9"
    legit = lim.fingerprint("client", "legit-key")
    lim.success(legit)
    for i in range(10):
        lim.failure(lim.fingerprint("client", f"guess-{i}"), proxy)
    assert lim.check(lim.fingerprint("client", "guess-next"), proxy) == 1
    assert lim.check(legit, proxy) == 0  # known good: never blocked by the source
    assert lim.check(lim.fingerprint("client", "guess-next"), "10.1.1.1") == 0  # other sources unaffected


def test_success_does_not_reset_source_scope():
    """Otherwise a legitimate client on the shared proxy would launder the
    attacker's failures."""
    clock = FakeClock()
    lim = _limiter(clock, per_source=10)
    for i in range(9):
        lim.failure(lim.fingerprint("client", f"guess-{i}"), "proxy")
    lim.success(lim.fingerprint("client", "legit-key"))
    lim.failure(lim.fingerprint("client", "guess-9"), "proxy")
    assert lim.check(lim.fingerprint("client", "guess-10"), "proxy") > 0


def test_failure_revokes_known_good():
    clock = FakeClock()
    lim = _limiter(clock, per_source=2)
    fp = lim.fingerprint("client", "was-valid")
    lim.success(fp)
    lim.failure(fp, "proxy")  # e.g. the key was removed from the config
    lim.failure(lim.fingerprint("client", "guess"), "proxy")
    assert lim.check(fp, "proxy") > 0  # no longer exempt from the source scope


def test_known_good_expires():
    clock = FakeClock()
    lim = _limiter(clock, per_source=1, known_good_ttl_seconds=60)
    fp = lim.fingerprint("client", "old-key")
    lim.success(fp)
    clock.t += 61
    lim.failure(lim.fingerprint("client", "guess"), "proxy")
    assert lim.check(fp, "proxy") > 0


def test_tables_are_bounded():
    clock = FakeClock()
    lim = _limiter(clock, max_tracked=10)
    for i in range(1000):
        lim.failure(lim.fingerprint("client", f"g{i}"), f"s{i}")
        lim.success(lim.fingerprint("client", f"ok{i}"))
    assert len(lim._credentials) <= 10
    assert len(lim._sources) <= 10
    assert len(lim._known_good) <= 10


def test_fingerprint_is_not_the_key_and_realms_differ():
    lim = _limiter(FakeClock())
    fp = lim.fingerprint("client", "key-a")
    assert "key-a" not in fp
    assert fp != hash_key("key-a").split(":", 1)[1][: len(fp)]
    assert fp != lim.fingerprint("operator", "key-a")
    assert _limiter(FakeClock()).fingerprint("client", "key-a") != fp  # per-process secret


def test_invalid_settings_rejected():
    for name in (
        "window_seconds",
        "per_credential",
        "per_source",
        "backoff_base_seconds",
        "backoff_max_seconds",
        "known_good_ttl_seconds",
        "max_tracked",
    ):
        with pytest.raises(ValueError):
            AuthAttemptLimiter(**{name: 0})
    with pytest.raises(ValueError):
        AuthAttemptLimiter(backoff_base_seconds=10, backoff_max_seconds=5)


# ---------------------------------------------------------------- key hashing


def test_hash_forms():
    assert hash_key("k") == "sha256:" + hashlib.sha256(b"k").hexdigest()
    pepper = "p" * 40
    expected = hmac.new(pepper.encode(), b"k", hashlib.sha256).hexdigest()
    assert hash_key("k", pepper) == "hmac-sha256:" + expected
    assert keyhash.is_hashed(hash_key("k")) and keyhash.is_hashed(hash_key("k", pepper))
    for plaintext in ("k", "dev-key-change-me", "sha256:short", "md5:" + "0" * 64, None, 3):
        assert not keyhash.is_hashed(plaintext)
    upper = "sha256:" + hashlib.sha256(b"k").hexdigest().upper()
    assert keyhash.is_hashed(upper) and keyhash.normalize(upper) == hash_key("k")


def test_lookup_compares_every_entry_in_constant_time(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(keyhash.hmac, "compare_digest", counting)
    entries = [(hash_key("a"), "A"), (hash_key("b"), "B"), (hash_key("c"), "C")]
    assert keyhash.find(keyhash.presented_hashes("a"), entries) == "A"
    assert len(calls) == 3  # no early exit on the first entry
    assert keyhash.find(keyhash.presented_hashes("zzz"), entries) is None


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "b2.db"
    monkeypatch.setenv("AI_FLEET_DB", f"sqlite:///{db_path}")
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(hash_key_map(TENANT_KEYS)))
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", json.dumps(hash_key_map(AGENT_KEYS)))
    monkeypatch.setenv("AI_FLEET_OPERATOR_API_KEY", hash_key(OPERATOR_KEY))
    import app.config as config

    config.DATABASE_PATH = str(db_path)
    from app import storage as db

    db.init_db()
    yield db_path


@pytest.fixture
def clock():
    from app import identity

    fake = FakeClock()
    identity.attempt_limiter.clock = fake  # restored by conftest
    return fake


@pytest.fixture
def client(fresh_db, clock):
    from app.main import app

    c = TestClient(app)
    r = c.post("/agents", json={"agent_id": "worker", "name": "w", "endpoint": "http://w"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert c.post("/agents/worker/approve", headers=ADMIN).status_code == 200
    return c


def _rows(db_path, **where):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()]
    conn.close()
    for r in rows:
        r["detail"] = json.loads(r["detail"])
    return [r for r in rows if all(r.get(k) == v for k, v in where.items())]


# Existing behaviour, now on hashed keys.


def test_valid_hashed_keys_pass(client):
    assert client.get("/tasks", headers=ADMIN).status_code == 200
    assert client.get("/tasks", headers=ADMIN_B).status_code == 200
    assert client.get("/tasks", headers=WORKER).status_code == 200
    assert client.get("/ops/audit", headers=OP).status_code == 200


def test_hashed_config_starts(fresh_db):
    from app import identity, main

    assert identity.validate_key_config() == []
    main.startup()  # does not raise


def test_suspended_agent_key_rejected_and_success_resets_backoff(client):
    assert client.post("/agents/worker/suspend", headers=ADMIN).status_code == 200
    for _ in range(4):
        assert client.get("/tasks", headers=WORKER).status_code == 401
    assert client.post("/agents/worker/approve", headers=ADMIN).status_code == 200
    assert client.get("/tasks", headers=WORKER).status_code == 200  # resets the count
    assert client.post("/agents/worker/suspend", headers=ADMIN).status_code == 200
    for _ in range(5):
        assert client.get("/tasks", headers=WORKER).status_code == 401
    assert client.get("/tasks", headers=WORKER).status_code == 429


def test_single_api_key_is_hashed(fresh_db, monkeypatch):
    import app.config as config

    monkeypatch.delenv("AI_FLEET_TENANT_API_KEYS")
    monkeypatch.setattr(config, "API_KEY", hash_key("single-key-0123456789abcdef"))
    from app.main import app

    c = TestClient(app)
    assert c.get("/tasks", headers={"X-API-Key": "single-key-0123456789abcdef"}).status_code == 200
    assert c.get("/tasks", headers={"X-API-Key": config.API_KEY}).status_code == 401  # the hash is not a key


def test_no_default_key(fresh_db, monkeypatch):
    import app.config as config

    monkeypatch.delenv("AI_FLEET_TENANT_API_KEYS")
    monkeypatch.setattr(config, "API_KEY", "")
    from app.main import app

    c = TestClient(app)
    for headers in ({"X-API-Key": "dev-key-change-me"}, {"X-API-Key": ""}, {}):
        assert c.get("/tasks", headers=headers).status_code == 401
    assert "dev-key-change-me" not in (ROOT / "app" / "config.py").read_text()


def test_hmac_pepper_hashes(fresh_db, monkeypatch):
    pepper = "pepper-" + "x" * 40
    monkeypatch.setenv("AI_FLEET_KEY_PEPPER", pepper)
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(hash_key_map(TENANT_KEYS, pepper)))
    from app import identity
    from app.main import app

    c = TestClient(app)
    assert identity.validate_key_config() == []
    assert c.get("/tasks", headers=ADMIN).status_code == 200
    monkeypatch.delenv("AI_FLEET_KEY_PEPPER")
    assert c.get("/tasks", headers=ADMIN_B).status_code == 401  # fail closed without the pepper
    assert any("AI_FLEET_KEY_PEPPER is not set" in p for p in identity.validate_key_config())
    monkeypatch.setenv("AI_FLEET_KEY_PEPPER", "short")
    assert any("AI_FLEET_KEY_PEPPER must be at least" in p for p in identity.validate_key_config())


def test_hashed_operator_key_reuse_detected(fresh_db, monkeypatch):
    from app import identity
    from app.main import app

    monkeypatch.setenv("AI_FLEET_OPERATOR_API_KEY", hash_key("key-a"))  # same as a tenant key
    assert any("must differ" in p for p in identity.validate_key_config())
    assert TestClient(app).get("/ops/audit", headers=ADMIN).status_code == 401


# Plaintext configuration is refused.


@pytest.mark.parametrize(
    "var",
    ["AI_FLEET_TENANT_API_KEYS", "AI_FLEET_AGENT_API_KEYS", "AI_FLEET_API_KEY", "AI_FLEET_OPERATOR_API_KEY"],
)
def test_plaintext_key_config_refused_at_startup(fresh_db, monkeypatch, var):
    import app.config as config
    from app import main

    if var == "AI_FLEET_TENANT_API_KEYS":
        monkeypatch.setenv(var, json.dumps({PLAINTEXT_SECRET: "tenant-a"}))
    elif var == "AI_FLEET_AGENT_API_KEYS":
        monkeypatch.setenv(var, json.dumps({PLAINTEXT_SECRET: {"agent_id": "worker", "tenant_id": "tenant-a"}}))
    elif var == "AI_FLEET_API_KEY":
        monkeypatch.setattr(config, "API_KEY", PLAINTEXT_SECRET)  # frozen at import
    else:
        monkeypatch.setenv(var, PLAINTEXT_SECRET)
    with pytest.raises(RuntimeError) as excinfo:
        main.startup()
    message = str(excinfo.value)
    assert f"{var} " in message or f"{var}:" in message
    assert "not in hashed form" in message and "re-issue" in message
    assert PLAINTEXT_SECRET not in message


def test_mixed_map_counts_plaintext_entries(fresh_db, monkeypatch):
    from app import identity

    keys = dict(hash_key_map(TENANT_KEYS), **{PLAINTEXT_SECRET: "tenant-c"})
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(keys))
    problems = identity.validate_key_config()
    assert any("AI_FLEET_TENANT_API_KEYS: 1 of 3 entries" in p for p in problems)
    assert all(PLAINTEXT_SECRET not in p for p in problems)


def test_plaintext_entries_never_match_at_request_time(client, monkeypatch):
    """Even where the startup check did not run, a plaintext entry is inert."""
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", json.dumps(dict(TENANT_KEYS, **hash_key_map({"key-b": "tenant-b"}))))
    assert client.get("/tasks", headers=ADMIN).status_code == 401  # configured in plaintext
    assert client.get("/tasks", headers=ADMIN_B).status_code == 200  # configured hashed
    monkeypatch.setenv("AI_FLEET_OPERATOR_API_KEY", OPERATOR_KEY)
    assert client.get("/ops/audit", headers=OP).status_code == 401


def test_malformed_key_map_refused_at_startup(fresh_db, monkeypatch):
    from app import main

    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", "{not json")
    with pytest.raises(RuntimeError) as excinfo:
        main.startup()
    assert "AI_FLEET_AGENT_API_KEYS is not valid JSON" in str(excinfo.value)


# Backoff over HTTP.


def test_repeated_failures_get_429_with_retry_after(client):
    for _ in range(5):
        assert client.get("/tasks", headers=WRONG).status_code == 401
    r = client.get("/tasks", headers=WRONG)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "1"
    assert client.get("/tasks", headers=ADMIN).status_code == 200  # other keys unaffected


def test_backoff_is_progressive_and_lifts(client, clock):
    for _ in range(5):
        client.get("/tasks", headers=WRONG)
    assert client.get("/tasks", headers=WRONG).headers["Retry-After"] == "1"
    clock.t += 1
    assert client.get("/tasks", headers=WRONG).status_code == 401  # evaluated again, still wrong
    assert client.get("/tasks", headers=WRONG).headers["Retry-After"] == "2"
    clock.t += 2
    assert client.get("/tasks", headers=WRONG).status_code == 401
    assert client.get("/tasks", headers=WRONG).headers["Retry-After"] == "4"


def test_shared_proxy_attacker_cannot_lock_out_working_clients(client, clock, monkeypatch):
    """Every TestClient request comes from the same source ("testclient"),
    exactly like clients behind one reverse proxy."""
    from app import identity

    monkeypatch.setattr(identity.attempt_limiter, "per_source", 10)
    assert client.get("/tasks", headers=WORKER).status_code == 200  # WORKER and ADMIN are known good
    for i in range(10):
        assert client.get("/tasks", headers={"X-API-Key": f"guess-{i}"}).status_code == 401
    assert client.get("/tasks", headers={"X-API-Key": "guess-10"}).status_code == 429
    assert client.get("/tasks", headers=ADMIN).status_code == 200
    assert client.get("/tasks", headers=WORKER).status_code == 200
    # The documented cost: a key that has not authenticated yet waits out the
    # (bounded) source block like any guess, then works.
    r = client.get("/tasks", headers=ADMIN_B)
    assert r.status_code == 429 and r.headers["Retry-After"] == "1"
    clock.t += 1
    assert client.get("/tasks", headers=ADMIN_B).status_code == 200


def test_default_source_budget_keeps_denial_flood_semantics(client):
    """300 different wrong keys from one source stay 401 (test_denial_flood):
    the source scope is only a coarse ceiling by default."""
    for i in range(300):
        assert client.get("/tasks", headers={"X-API-Key": f"bad-key-{i}"}).status_code == 401
    assert client.get("/tasks", headers=ADMIN).status_code == 200


def test_missing_key_is_not_counted(client):
    for _ in range(20):
        assert client.get("/tasks").status_code == 401
        assert client.get("/ops/audit").status_code == 401
    assert client.get("/tasks", headers=ADMIN).status_code == 200


def test_operator_failures_do_not_back_off_tenant_endpoints(client):
    for _ in range(5):
        assert client.get("/ops/audit", headers=ADMIN).status_code == 401
    assert client.get("/ops/audit", headers=ADMIN).status_code == 429
    assert client.get("/tasks", headers=ADMIN).status_code == 200
    assert client.get("/ops/audit", headers=OP).status_code == 200


def test_operator_key_guessing_backs_off(client):
    guess = {"X-API-Key": OPERATOR_KEY[:-1] + "?"}
    for _ in range(5):
        assert client.get("/ops/audit/verify", headers=guess).status_code == 401
    assert client.get("/ops/audit/verify", headers=guess).status_code == 429
    assert client.get("/ops/audit/verify", headers=OP).status_code == 200


def test_429_is_audited_and_bounded_and_keys_never_recorded(client, fresh_db, monkeypatch):
    from app import identity, main

    monkeypatch.setattr(main.denial_limiter, "clock", FakeClock(1_000_000.0))  # one denial window

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture(level=logging.DEBUG)
    logging.getLogger("agent_fleet_hub").addHandler(handler)
    try:
        for _ in range(8):
            client.get("/tasks", headers=WRONG)
    finally:
        logging.getLogger("agent_fleet_hub").removeHandler(handler)
    main.shutdown()  # closes the denial window
    (summary,) = _rows(fresh_db, action="request.denied.summary")
    assert summary["actor"] == "sys:unauthenticated"
    assert summary["detail"]["statuses"] == {"401": 4, "429": 3}
    assert len(_rows(fresh_db, action="request.denied")) == 1  # DenialLimiter bound unchanged
    secret = WRONG["X-API-Key"]
    assert any("auth backoff" in m for m in records)
    assert all(secret not in m for m in records)
    assert secret not in json.dumps(_rows(fresh_db))
    tables = (identity.attempt_limiter._credentials, identity.attempt_limiter._known_good)
    assert all(secret not in k for t in tables for k in t)


# ---------------------------------------------------------------- demo keys


def _gen_keys():
    import importlib.util

    spec = importlib.util.spec_from_file_location("fleet_kit_gen_keys", ROOT / "scripts" / "gen_keys.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GUESSABLE = ("fleet-demo-tenant", "fleet-demo-other", "fleet-demo-orch", "fleet-demo-worker", "dev-key-change-me")


def test_no_guessable_demo_literals():
    for rel in ("mcp-host.example.json", "demo.sh", "app/config.py"):
        text = (ROOT / rel).read_text()
        assert not any(literal in text for literal in GUESSABLE), rel
    host = json.loads((ROOT / "mcp-host.example.json").read_text())
    values = [s["env"]["FLEET_AGENT_API_KEY"] for s in host["mcpServers"].values()]
    assert values == ["${FLEET_AGENT_A_KEY}", "${FLEET_AGENT_B_KEY}"]  # placeholders, not keys


def test_demo_keys_are_fresh_random_and_hub_accepts_them(fresh_db, monkeypatch):
    gk = _gen_keys()
    first, second = gk.demo_keys(env={}), gk.demo_keys(env={})
    assert set(first) == set(gk.DEMO_CLIENT_VARS)
    assert all(len(k) >= 43 for k in first.values())
    assert len(set(first.values())) == 4
    assert not set(first.values()) & set(second.values())  # every run is new

    server = gk.demo_server_env(first)
    for name, value in server.items():
        assert not any(k in value for k in first.values()), name  # hashes only
    monkeypatch.setenv("AI_FLEET_TENANT_API_KEYS", server["AI_FLEET_TENANT_API_KEYS"])
    monkeypatch.setenv("AI_FLEET_AGENT_API_KEYS", server["AI_FLEET_AGENT_API_KEYS"])
    monkeypatch.delenv("AI_FLEET_OPERATOR_API_KEY")
    import app.config as config

    monkeypatch.setattr(config, "API_KEY", server["AI_FLEET_API_KEY"])
    from app import identity
    from app.main import app

    assert identity.validate_key_config() == []
    c = TestClient(app)
    tenant = {"X-API-Key": first["FLEET_API_KEY"]}
    r = c.post("/agents", json={"agent_id": "orchestrator", "name": "o", "endpoint": "http://o"}, headers=tenant)
    assert r.status_code == 200 and r.json()["tenant_id"] == "demo-tenant"
    assert c.post("/agents/orchestrator/approve", headers=tenant).status_code == 200
    assert c.get("/tasks", headers={"X-API-Key": first["FLEET_AGENT_A_KEY"]}).status_code == 200
    assert c.get("/agents", headers={"X-API-Key": first["FLEET_OTHER_TENANT_KEY"]}).json() == []


def test_demo_mcp_host_is_rendered_with_the_agent_keys():
    gk = _gen_keys()
    keys = gk.demo_keys(env={})
    rendered = json.loads(gk.render_mcp_host(gk.MCP_HOST_TEMPLATE.read_text(), keys))
    env = {name: s["env"] for name, s in rendered["mcpServers"].items()}
    assert env["fleet-kit-orchestrator"]["FLEET_AGENT_API_KEY"] == keys["FLEET_AGENT_A_KEY"]
    assert env["fleet-kit-worker"]["FLEET_AGENT_API_KEY"] == keys["FLEET_AGENT_B_KEY"]
    assert "${" not in json.dumps(rendered) and "/ABS/PATH/TO" not in json.dumps(rendered)
    assert keys["FLEET_API_KEY"] not in json.dumps(rendered)  # tenant key never in a host config


def test_demo_refuses_weak_preset_and_matches_hub_hashing():
    gk = _gen_keys()
    with pytest.raises(SystemExit):
        gk.demo_keys(env={"FLEET_AGENT_A_KEY": "fleet-demo-orch"})
    kept = gk.demo_keys(env={"FLEET_AGENT_A_KEY": "a" * 43})
    assert kept["FLEET_AGENT_A_KEY"] == "a" * 43
    assert gk.keyhash.hash_key("some-key") == hash_key("some-key")
    assert gk.keyhash.hash_key("some-key", "p" * 32) == hash_key("some-key", "p" * 32)

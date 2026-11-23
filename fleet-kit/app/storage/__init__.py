"""Module 5 - Storage & Workspace (persistence).

Thin SQLite layer. Every table write goes through here so the PostgreSQL
adapter (planned, plan v0.2.6 §23) can replace this module without touching
the other five modules. No ORM: plain SQL, one connection per call.
"""
import json
import sqlite3
from pathlib import Path

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    capabilities TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'Pending',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'submitted',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    idempotency_key TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL,
    recipient_agent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_receipts (
    message_id TEXT NOT NULL,
    recipient_agent_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (message_id, recipient_agent_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""


def connect() -> sqlite3.Connection:
    path = Path(config.DATABASE_PATH)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def insert_agent(agent_id, name, endpoint, capabilities, status, created_at):
    with connect() as conn:
        conn.execute(
            "INSERT INTO agents VALUES (?,?,?,?,?,?)",
            (agent_id, name, endpoint, json.dumps(capabilities), status, created_at),
        )
        conn.commit()


def get_agent(agent_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
    return dict(row) if row else None


def list_agents():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def set_agent_status(agent_id, status):
    with connect() as conn:
        conn.execute(
            "UPDATE agents SET status = ? WHERE agent_id = ?", (status, agent_id)
        )
        conn.commit()


def insert_task(task_id, tenant_id, agent_id, title, status, created_at, updated_at):
    with connect() as conn:
        conn.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
            (task_id, tenant_id, agent_id, title, status, created_at, updated_at),
        )
        conn.commit()


def get_task(task_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
    return dict(row) if row else None


def set_task_status(task_id, status, updated_at):
    with connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, updated_at, task_id),
        )
        conn.commit()


def list_tasks(tenant_id=None):
    with connect() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE tenant_id = ? ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def insert_message(envelope):
    """Insert a message. Returns True if inserted, False if the
    idempotency key already exists (duplicate delivery)."""
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    envelope["idempotency_key"],
                    envelope["message_id"],
                    envelope["event_id"],
                    envelope["task_id"],
                    envelope["tenant_id"],
                    envelope["sender_agent_id"],
                    envelope["recipient_agent_id"],
                    envelope["event_type"],
                    json.dumps(envelope["payload"]),
                    envelope["created_at"],
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def get_message_by_key(idempotency_key):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def get_message_by_id(message_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    return d


def record_receipt(message_id, recipient_agent_id, tenant_id, received_at):
    """Record a delivery receipt. Returns True if recorded, False if this
    recipient already receipted the message (duplicate receipt is a no-op,
    same at-least-once rule as duplicate deliveries)."""
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO message_receipts VALUES (?,?,?,?)",
                (message_id, recipient_agent_id, tenant_id, received_at),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def get_receipt(message_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM message_receipts WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    return dict(row) if row else None


def list_messages(task_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        # Delivery receipt, if the recipient has confirmed receipt.
        d["receipt"] = get_receipt(d["message_id"])
        out.append(d)
    return out


def append_audit(prev_hash, entry_hash, created_at, actor, action, target, detail):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO audit_log (prev_hash, entry_hash, created_at, actor, action, target, detail) VALUES (?,?,?,?,?,?,?)",
            (prev_hash, entry_hash, created_at, actor, action, target, json.dumps(detail)),
        )
        conn.commit()
        return cur.lastrowid


def append_audit_transaction(build):
    """Append one audit entry atomically.

    build(prev_hash) returns the entry dict (entry_hash, created_at,
    actor, action, target, detail). The read of the previous hash and
    the insert run inside a single BEGIN IMMEDIATE transaction, so two
    concurrent appends can never read the same prev_hash and fork the
    chain (the race that broke verification under concurrent agent
    processes). Returns (seq, prev_hash, entry).
    """
    path = Path(config.DATABASE_PATH)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # manual transaction control
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["entry_hash"] if row else "GENESIS"
        entry = build(prev_hash)
        cur = conn.execute(
            "INSERT INTO audit_log (prev_hash, entry_hash, created_at, actor, action, target, detail) VALUES (?,?,?,?,?,?,?)",
            (
                prev_hash,
                entry["entry_hash"],
                entry["created_at"],
                entry["actor"],
                entry["action"],
                entry["target"],
                json.dumps(entry["detail"]),
            ),
        )
        seq = cur.lastrowid
        conn.execute("COMMIT")
        return seq, prev_hash, entry
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def last_audit_hash():
    with connect() as conn:
        row = conn.execute(
            "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    return row["entry_hash"] if row else "GENESIS"


def list_audit(limit=100, actor=None):
    with connect() as conn:
        if actor is not None:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE actor = ? ORDER BY seq DESC LIMIT ?",
                (actor, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"])
        out.append(d)
    return out

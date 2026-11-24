"""Module 6 - Observability (audit log).

Append-only log with a hash chain (plan v0.2.6): each entry records the hash
of the previous entry, so tampering with history breaks the chain. A hash
chain alone is not tamper-proof; it is evidence, not a guarantee.
"""
import hashlib
import json

from app import storage as db
from app.messaging import now_iso


def _entry_hash(prev_hash, created_at, actor, action, target, detail) -> str:
    raw = "|".join(
        [prev_hash, created_at, actor, action, target, json.dumps(detail, sort_keys=True)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record(actor: str, action: str, target: str, detail: dict | None = None) -> dict:
    detail = detail or {}

    def build(prev_hash):
        created_at = now_iso()
        return {
            "entry_hash": _entry_hash(prev_hash, created_at, actor, action, target, detail),
            "created_at": created_at,
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail,
        }

    # Atomic append: the previous-hash read and the insert are one
    # transaction, so concurrent agent processes cannot fork the chain.
    seq, prev_hash, entry = db.append_audit_transaction(build)
    return {
        "seq": seq,
        "prev_hash": prev_hash,
        "entry_hash": entry["entry_hash"],
        "created_at": entry["created_at"],
        "actor": actor,
        "action": action,
        "target": target,
        "detail": detail,
    }


def list_audit(limit: int = 100, actor: str | None = None) -> list:
    """List audit entries, newest first. When actor is given, only that
    tenant's entries are returned (T08: no cross-tenant audit reads)."""
    return db.list_audit(limit=limit, actor=actor)


def verify_chain(after_seq: int = 0, anchor_hash: str | None = None, batch: int = 1000) -> dict:
    """Recompute the hash chain from a trusted starting point up to the head.

    Default (after_seq=0): the whole chain, starting at GENESIS. Every entry
    is checked however long the log is; rows are read in batches, so memory
    stays flat. (The previous version checked only the first 1000 entries,
    so tampering with any later entry went undetected.)

    Incremental (after_seq=N > 0): the caller must pass anchor_hash, the
    head_hash of seq N that it kept from an earlier successful run. The
    anchor is never read from the log itself: a starting hash taken from
    the log would let a rewritten log vouch for itself.

    Returns {"ok", "checked", "head_seq", "head_hash"} on success, or
    {"ok": False, "broken_at_seq" | "reason", ...} on failure. Keep head_hash
    somewhere the Hub cannot write: a hash chain alone cannot detect the
    newest entries being cut off, comparing against a kept checkpoint can.
    """
    if after_seq < 0:
        raise ValueError("after_seq must be >= 0")
    if after_seq == 0:
        prev = "GENESIS"
    elif not anchor_hash:
        raise ValueError("anchor_hash is required when after_seq > 0")
    else:
        prev = anchor_hash
    head_seq, _ = db.audit_head()
    if after_seq > head_seq:
        return {
            "ok": False,
            "reason": "log ends before the checkpoint (entries removed)",
            "checkpoint_seq": after_seq,
            "head_seq": head_seq,
        }
    checked = 0
    last_seq = after_seq
    while True:
        entries = db.list_audit_from_seq(after_seq=last_seq, limit=batch)
        if not entries:
            break
        for e in entries:
            if e["prev_hash"] != prev:
                return {"ok": False, "broken_at_seq": e["seq"], "checked": checked}
            recomputed = _entry_hash(
                e["prev_hash"], e["created_at"], e["actor"], e["action"], e["target"], e["detail"]
            )
            if recomputed != e["entry_hash"]:
                return {"ok": False, "broken_at_seq": e["seq"], "checked": checked}
            prev = e["entry_hash"]
            last_seq = e["seq"]
            checked += 1
    return {"ok": True, "checked": checked, "head_seq": last_seq, "head_hash": prev}

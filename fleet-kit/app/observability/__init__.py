"""Module 6 - Observability (audit log).

Append-only log with a hash chain (plan v0.2.6): each entry records the hash
of the previous entry, so tampering with history breaks the chain. A hash
chain alone is not tamper-proof; it is evidence, not a guarantee.

T09 (audit record correlation): correlation fields live inside `detail`,
which is part of the entry hash, so they are integrity-protected like the
rest of the entry. `actor` stays the tenant id (it scopes GET /audit), or a
"sys:" system actor (sys:unauthenticated, sys:operator) that no tenant id
can equal; the acting credential is detail["principal"] = {"kind": "tenant"|"agent",
"agent_id"}. correlate_task() checks one task's messages, receipts and
status against its audit entries.
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
    the log would let a rewritten log vouch for itself. Row N itself must
    still exist, its stored entry_hash must equal the anchor, and its
    content must still hash to it; otherwise deleting (or rewriting) only
    the checkpoint row would go unnoticed, because row N+1 still points at
    the anchor.

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
    if after_seq > 0:
        anchor = db.get_audit_entry(after_seq)
        if anchor is None:
            return {
                "ok": False,
                "reason": "checkpoint entry missing (entries removed)",
                "checkpoint_seq": after_seq,
                "checked": 0,
            }
        recomputed = _entry_hash(
            anchor["prev_hash"], anchor["created_at"], anchor["actor"],
            anchor["action"], anchor["target"], anchor["detail"],
        )
        if anchor["entry_hash"] != prev or recomputed != prev:
            return {
                "ok": False,
                "reason": "checkpoint entry does not match anchor_hash",
                "broken_at_seq": after_seq,
                "checked": 0,
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


def principal(auth: dict) -> dict:
    """The acting credential as recorded in audit detail (never the key).
    kind: "tenant" | "agent" | "operator"."""
    return {"kind": auth.get("auth_kind"), "agent_id": auth.get("agent_id")}


def correlate_task(tenant_id: str, task_id: str) -> dict:
    """T09 check for one task: can every message, receipt and the current
    status be traced back to audit entries of this tenant?

    A message counts as attributed (plan v0.2.6 §21.3 B) when it has a
    task_id, message_id and trace_id AND a message.send audit entry with the
    same message_id and trace_id, recorded under a principal that matches
    its sender when the sender used an agent key. Receipts need a matching
    message.receipt entry by the recipient agent. The task needs a
    task.create entry whose trace_id equals the task's current trace_id,
    and its status must equal the end of the audited transition chain
    (submitted -> to -> ...). The chain is followed by "from"/"to" rather
    than taken from the newest entry, because two overlapping requests may
    append their audit entries in the opposite order of their
    compare-and-set writes. Caller must have checked that the task belongs
    to tenant_id.
    """
    task = db.get_task(task_id)
    if task is None:
        raise ValueError("task not found")
    messages = db.list_messages(task_id)
    entries = db.list_audit_for_target(actor=tenant_id, target=task_id)

    sends, receipts = {}, {}
    created = None  # detail of the first task.create entry
    last_status = None
    moves = {}  # from -> to, one per state (CAS: a state is left at most once)
    for e in entries:
        d, action = e["detail"], e["action"]
        if action == "task.create":
            if created is None:
                created = d
        elif action == "message.send":
            sends.setdefault(d.get("message_id"), d)
        elif action == "message.receipt":
            receipts.setdefault(d.get("message_id"), d)
        elif action == "task.status":
            last_status = d.get("to", d.get("status"))
            if d.get("from") is not None and last_status is not None:
                moves.setdefault(d["from"], last_status)

    problems = []
    if not task.get("trace_id"):
        problems.append({"task_id": task_id, "missing": ["task.trace_id"]})
    if created is None:
        problems.append({"task_id": task_id, "missing": ["audit.task.create"]})
    elif created.get("trace_id") != task.get("trace_id"):
        # The trace was changed after creation, around the API.
        problems.append({"task_id": task_id, "missing": ["audit.task.trace_id_mismatch"]})
    if moves:
        expected, seen = "submitted", set()
        while expected in moves and expected not in seen:
            seen.add(expected)
            expected = moves[expected]
    else:
        # Entries without "from" (older format): newest one wins.
        expected = last_status or "submitted"
    if task["status"] != expected:
        problems.append({
            "task_id": task_id,
            "missing": [f"audit.task.status (status {task['status']}, last audited {expected})"],
        })

    attributed = on_task_trace = receipts_total = receipts_audited = 0
    for m in messages:
        missing = [f"message.{f}" for f in ("task_id", "message_id", "trace_id") if not m.get(f)]
        sent = sends.get(m["message_id"])
        if sent is None:
            missing.append("audit.message.send")
        else:
            p = sent.get("principal")
            if not p:
                missing.append("audit.principal")
            elif p.get("kind") == "agent" and p.get("agent_id") != m["sender_agent_id"]:
                missing.append("audit.principal_is_not_sender")
            if sent.get("trace_id") != m.get("trace_id"):
                missing.append("audit.trace_id_mismatch")
        if not missing:
            attributed += 1
        if m.get("trace_id") and m.get("trace_id") == task.get("trace_id"):
            on_task_trace += 1
        receipt = m.get("receipt")
        if receipt:
            receipts_total += 1
            r = receipts.get(m["message_id"])
            if r is None:
                missing.append("audit.message.receipt")
            elif (r.get("principal") or {}).get("agent_id") != receipt["recipient_agent_id"]:
                missing.append("audit.receipt_principal_is_not_recipient")
            else:
                receipts_audited += 1
        if missing:
            problems.append({"message_id": m["message_id"], "missing": missing})

    return {
        "ok": not problems,
        "task_id": task_id,
        "trace_id": task.get("trace_id"),
        "status": task["status"],
        "messages_total": len(messages),
        "messages_attributed": attributed,
        "messages_on_task_trace": on_task_trace,
        "receipts_total": receipts_total,
        "receipts_audited": receipts_audited,
        "audit_entries": len(entries),
        "problems": problems,
    }

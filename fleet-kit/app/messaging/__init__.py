"""Module 4 - Communication Infrastructure (unified message envelope).

Envelope fields follow plan v0.2.6: message_id, event_id, task_id,
context_id, tenant_id, sender/recipient agent IDs, delegation_chain_id,
protocol + versions, event_type, created_at, correlation_id,
idempotency_key, payload_reference, trace_id, signature fields.

Platform idempotency key format (plan v0.2.6):
    tenant_id:sender_agent_id:task_id:event_type:message_id
With a caller-supplied key, the last component is that key instead of the
fresh message_id, so a retry maps to the same stored message.

Each component is percent-encoded before joining (see idempotency_key):
":" inside a component becomes "%3A" and "%" becomes "%25", so distinct
component tuples can never produce the same key (e.g. event_type "a:b" +
key "c" vs event_type "a" + key "b:c"). Letters, digits and "_.-~" are left
as they are, so keys built from ordinary ids are unchanged.
Keys stored before the encoding (legacy_idempotency_key: components joined
as they are) differ only when a component holds ":", "%" or another
character outside [A-Za-z0-9_.-~]; storage maps them with legacy_form_of_key,
so a retry inside the dedup window still finds the original message.
Dedup window is 7 days, persisted. Delivery is at-least-once; the receiver
is idempotent, so a duplicate delivery never creates a duplicate side effect.
"""
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, unquote


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def idempotency_key(*components: str) -> str:
    """Join components with ":" after percent-encoding each one, so the
    encoding is injective (no delimiter collisions between components)."""
    return ":".join(quote(str(c), safe="") for c in components)


def legacy_idempotency_key(*components: str) -> str:
    """Key format used before percent-encoding: components joined with ":"
    as they are (not injective). Only used to find rows stored that way."""
    return ":".join(str(c) for c in components)


def legacy_form_of_key(key: str) -> str:
    """Pre-encoding form of a key built by idempotency_key(): split on ":"
    (an encoded component never contains one) and unquote each part. Equal
    to key itself when every component is an ordinary id."""
    return ":".join(unquote(part) for part in key.split(":"))


def build_envelope(
    *,
    tenant_id: str,
    sender_agent_id: str,
    recipient_agent_id: str,
    task_id: str,
    event_type: str,
    payload: dict,
    context_id: str | None = None,
    correlation_id: str | None = None,
    protocol: str = "fleet",
    protocol_version: str = "1.0",
    client_idempotency_key: str | None = None,
    trace_id: str | None = None,
) -> dict:
    message_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    idem = idempotency_key(
        tenant_id, sender_agent_id, task_id, event_type,
        client_idempotency_key or message_id,
    )
    return {
        "message_id": message_id,
        "event_id": event_id,
        "task_id": task_id,
        "context_id": context_id or task_id,
        "tenant_id": tenant_id,
        "sender_agent_id": sender_agent_id,
        "recipient_agent_id": recipient_agent_id,
        "delegation_chain_id": None,
        "protocol": protocol,
        "protocol_version": protocol_version,
        "event_type": event_type,
        "created_at": now_iso(),
        "correlation_id": correlation_id or message_id,
        "idempotency_key": idem,
        "payload_reference": None,
        # T09: the caller passes the task's trace (or one it propagates);
        # a fresh id is only a fallback for callers without a task trace.
        "trace_id": trace_id or str(uuid.uuid4()),
        "signature": None,
        "payload": payload,
    }

"""Module 4 - Communication Infrastructure (unified message envelope).

Envelope fields follow plan v0.2.6: message_id, event_id, task_id,
context_id, tenant_id, sender/recipient agent IDs, delegation_chain_id,
protocol + versions, event_type, created_at, correlation_id,
idempotency_key, payload_reference, trace_id, signature fields.

Platform idempotency key format (plan v0.2.6):
    tenant_id:sender_agent_id:task_id:event_type:message_id
Dedup window is 7 days, persisted. Delivery is at-least-once; the receiver
is idempotent, so a duplicate delivery never creates a duplicate side effect.
"""
import uuid
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
) -> dict:
    message_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
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
        "idempotency_key": ":".join(
            [tenant_id, sender_agent_id, task_id, event_type, message_id]
        ),
        "payload_reference": None,
        "trace_id": str(uuid.uuid4()),
        "signature": None,
        "payload": payload,
    }

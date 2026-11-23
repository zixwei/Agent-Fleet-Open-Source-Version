"""Module 4 - Message Bus (in-process implementation).

Interface the Valkey-backed bus will implement. Delivery is at-least-once:
publish() may be called again with the same envelope (retry); the store
dedups on idempotency_key, so a duplicate delivery is a no-op that returns
the already-stored message. This is how principle 5 (no duplicate side
effects from duplicate messages) is enforced.
"""
from app import storage as db
from app.messaging import build_envelope


class MessageBus:
    def publish(self, envelope: dict) -> dict:
        """Store a message. Returns {"stored": bool, "message": dict}.
        stored=False means this exact delivery was already seen."""
        stored = db.insert_message(envelope)
        if stored:
            return {"stored": True, "message": envelope}
        return {"stored": False, "message": db.get_message_by_key(envelope["idempotency_key"])}

    def send(
        self,
        *,
        tenant_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        task_id: str,
        event_type: str,
        payload: dict,
    ) -> dict:
        envelope = build_envelope(
            tenant_id=tenant_id,
            sender_agent_id=sender_agent_id,
            recipient_agent_id=recipient_agent_id,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
        )
        return self.publish(envelope)

    def republish(self, envelope: dict) -> dict:
        """Retry a delivery with the same envelope (same idempotency key)."""
        return self.publish(envelope)

    def acknowledge(
        self,
        *,
        tenant_id: str,
        task_id: str,
        message_id: str,
        recipient_agent_id: str,
    ) -> dict:
        """Record a delivery receipt from the receiving agent.

        The receipt is what formal T01 checks: routing metadata alone only
        proves the hub stored the message, not that the recipient got it.
        Returns {"stored": bool, "receipt": dict}; stored=False means this
        recipient already receipted the message (duplicate receipt no-op).
        Raises ValueError("not found") when the message does not exist, is
        not on this task, or belongs to another tenant (all reported as
        404, so existence is never leaked); ValueError("recipient mismatch")
        when the caller is not the message's recipient.
        """
        from app.messaging import now_iso

        message = db.get_message_by_id(message_id)
        if (
            not message
            or message["task_id"] != task_id
            or message["tenant_id"] != tenant_id
        ):
            raise ValueError("not found")
        if message["recipient_agent_id"] != recipient_agent_id:
            raise ValueError("recipient mismatch")
        stored = db.record_receipt(
            message_id, recipient_agent_id, tenant_id, now_iso()
        )
        return {"stored": stored, "receipt": db.get_receipt(message_id)}

    def list_for_task(self, task_id: str):
        return db.list_messages(task_id)


bus = MessageBus()

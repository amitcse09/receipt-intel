"""Notifier: a SECOND consumer group on `receipts.validated`.

Fan-out: every consumer GROUP gets every event; within a group, events are split between members.
So validators, notifier, analytics… each read the same stream independently, at their own pace — the
producer doesn't know or care who consumes. (RabbitMQ: one queue per consumer type bound to an exchange;
Kafka: just another group id on the same topic, and it can even start from the beginning and replay history.)

Run: uv run python -m app.workers.notifier
"""

import asyncio

from aiokafka import AIOKafkaConsumer

from app.config import get_settings
from app.events.schemas import ReceiptValidated
from app.events.topics import ensure_topics
from app.workers.common import setup, stop_event

GROUP_ID = "member-notifier"

MESSAGES = {
    "validated": "✅ Receipt #{id} is approved — cashback is on its way.",
    "rejected": "❌ Receipt #{id} was rejected: {reason}. You can appeal within 14 days (form PB-APPEAL-7).",
    "needs_review": "🔎 Receipt #{id} needs a quick manual check — we'll update you within 3 business days.",
}


async def main():
    log, topics = setup("notifier")
    s = get_settings()
    await ensure_topics(s.kafka_bootstrap, topics)
    consumer = AIOKafkaConsumer(topics.validated, bootstrap_servers=s.kafka_bootstrap, group_id=GROUP_ID,
                                enable_auto_commit=False, auto_offset_reset="earliest")
    await consumer.start()
    stop = stop_event()
    log.info("listening on %s", topics.validated)
    try:
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=1000)
            for messages in batch.values():
                for msg in messages:
                    e = ReceiptValidated.model_validate_json(msg.value)
                    text = MESSAGES.get(e.status, "Receipt #{id}: {status}").format(
                        id=e.receipt_id, status=e.status, reason=(e.review_notes or ["see app"])[0])
                    # A real notifier would send email/push here — and dedupe on e.event_id.
                    log.info("→ notify %s: %s", e.member_id, text)
            if batch:
                await consumer.commit()
    finally:
        await consumer.stop()
        log.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())

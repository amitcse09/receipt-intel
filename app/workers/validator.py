"""Validator worker: a Kafka consumer group that runs LLM validation for submitted receipts.

Run one or more (each joins the same group and Kafka splits the partitions between them):
    uv run python -m app.workers.validator --name w1
    uv run python -m app.workers.validator --name w2

Delivery: at-least-once. We commit the offset only AFTER the receipt is processed, so a crash mid-way means
the message is delivered again (maybe to another worker). `handle()` is idempotent, so that's safe.
"""

import argparse
import asyncio
import json

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRebalanceListener, TopicPartition
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import extraction
from app.config import get_settings
from app.db import Receipt, SessionLocal, engine
from app.events.outbox import add_event
from app.events.schemas import ReceiptSubmitted, ReceiptValidated
from app.events.topics import Topics, ensure_topics
from app.llm import LLMClient, get_llm
from app.workers.common import setup, stop_event

GROUP_ID = "receipt-validator"
MAX_ATTEMPTS = 3  # transient failures (e.g. DB blip) before giving up and dead-lettering


class PoisonMessage(Exception):
    """A message that will NEVER succeed (bad payload, unknown receipt). Retrying is pointless → DLQ."""


async def handle(raw: bytes, sessions: async_sessionmaker[AsyncSession], llm: LLMClient, topics: Topics) -> str:
    """Process one `receipt.submitted` event. Idempotent: safe to run twice for the same event."""
    try:
        event = ReceiptSubmitted.model_validate_json(raw)
    except ValidationError as e:
        raise PoisonMessage(f"invalid payload: {e.errors()[0]['msg']}") from e

    async with sessions() as s:
        receipt = await s.get(Receipt, event.receipt_id)
        if receipt is None:
            raise PoisonMessage(f"receipt {event.receipt_id} does not exist")
        if receipt.status != "pending":
            return f"skip (already {receipt.status})"  # duplicate delivery → nothing to do
        args = (receipt.raw_text, receipt.store, receipt.total, receipt.purchased_on)

    # The slow LLM call happens OUTSIDE any DB transaction (never hold locks/connections during network I/O).
    status, notes = await extraction.validate(*args, llm)

    async with sessions() as s:
        # Conditional update = optimistic concurrency: only the FIRST worker to finish wins. If a duplicate
        # delivery raced us, rowcount is 0 and we don't emit a second "validated" event.
        result = await s.execute(
            update(Receipt).where(Receipt.id == event.receipt_id, Receipt.status == "pending")
            .values(status=status, review_notes=notes)
        )
        if result.rowcount == 0:
            await s.rollback()
            return "skip (another worker finished first)"
        # The follow-up event goes through the outbox too — same transaction as the status change.
        add_event(s, topics.validated, key=event.member_id, event=ReceiptValidated(
            receipt_id=event.receipt_id, member_id=event.member_id, status=status, review_notes=notes))
        await s.commit()
    return status


async def process(msg, sessions, llm, topics, dlq_producer, log) -> str:
    """Retry transient errors, dead-letter poison messages. Never raises → the consumer keeps going."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await handle(msg.value, sessions, llm, topics)
        except PoisonMessage as e:
            reason = str(e)
            break
        except Exception as e:  # transient? retry with backoff
            reason = f"{type(e).__name__}: {e}"
            log.warning("attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, reason)
            if attempt < MAX_ATTEMPTS:  # no point waiting after the last attempt
                await asyncio.sleep(0.5 * 2**attempt)  # 1s, 2s
    # Park it in the DLQ with context, so the partition isn't blocked and nothing is silently dropped.
    await dlq_producer.send_and_wait(topics.dlq, msg.value, key=msg.key, headers=[
        ("error", reason.encode()[:500]), ("source", f"{msg.topic}/{msg.partition}/{msg.offset}".encode())])
    return f"DEAD-LETTERED ({reason})"


class LogRebalance(ConsumerRebalanceListener):
    def __init__(self, log):
        self.log = log

    async def on_partitions_revoked(self, revoked):
        if revoked:
            self.log.info("partitions revoked: %s", sorted(tp.partition for tp in revoked))

    async def on_partitions_assigned(self, assigned):
        self.log.info("partitions assigned: %s", sorted(tp.partition for tp in assigned))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="validator")
    args = parser.parse_args()
    log, topics = setup(args.name)
    s = get_settings()
    await ensure_topics(s.kafka_bootstrap, topics)

    consumer = AIOKafkaConsumer(
        bootstrap_servers=s.kafka_bootstrap, group_id=GROUP_ID, client_id=args.name,
        enable_auto_commit=False,        # WE decide when a message counts as done
        auto_offset_reset="earliest",    # new group → start from the oldest retained message
        max_poll_interval_ms=300_000,    # a worker silent longer than this is considered dead → rebalance
    )
    consumer.subscribe([topics.submitted], listener=LogRebalance(log))
    dlq = AIOKafkaProducer(bootstrap_servers=s.kafka_bootstrap, acks="all", enable_idempotence=True)
    await consumer.start()
    await dlq.start()
    llm, stop = get_llm(), stop_event()
    log.info("joined group %r, waiting for receipts…", GROUP_ID)
    try:
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=1000, max_records=10)
            for tp, messages in batch.items():
                for msg in messages:
                    receipt = json.loads(msg.value).get("receipt_id", "?") if msg.value.startswith(b"{") else "?"
                    log.info("p%d@%d receipt %s → processing", msg.partition, msg.offset, receipt)
                    outcome = await process(msg, SessionLocal, llm, topics, dlq, log)
                    # Commit AFTER processing: offset+1 = "next message to read". Crash before this line →
                    # redelivered. That's at-least-once; handle() being idempotent makes it effectively-once.
                    await consumer.commit({TopicPartition(msg.topic, msg.partition): msg.offset + 1})
                    log.info("p%d@%d receipt %s → %s", msg.partition, msg.offset, receipt, outcome)
    finally:
        await consumer.stop()  # leaves the group → immediate rebalance instead of waiting for a timeout
        await dlq.stop()
        await engine.dispose()
        log.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())

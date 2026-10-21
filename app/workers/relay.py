"""Outbox relay: polls the outbox table and publishes new events to Kafka.

Run: uv run python -m app.workers.relay
(Alternative in production: CDC — Debezium tails the Postgres WAL and publishes outbox rows, no polling.)
"""

import asyncio

from aiokafka import AIOKafkaProducer

from app.config import get_settings
from app.db import SessionLocal, engine
from app.events.outbox import relay_once
from app.events.topics import ensure_topics
from app.workers.common import setup, stop_event

POLL_SECONDS = 0.5


async def main():
    log, topics = setup("relay")
    s = get_settings()
    await ensure_topics(s.kafka_bootstrap, topics)
    # acks="all": the broker confirms only after all in-sync replicas have the message.
    # enable_idempotence: broker de-duplicates producer retries (no duplicates from network blips).
    producer = AIOKafkaProducer(bootstrap_servers=s.kafka_bootstrap, acks="all", enable_idempotence=True)
    await producer.start()
    stop = stop_event()
    log.info("publishing outbox → Kafka every %.1fs", POLL_SECONDS)
    try:
        while not stop.is_set():
            try:
                sent = await relay_once(SessionLocal, producer)
                if sent:
                    log.info("published %d event(s)", sent)
            except Exception as e:  # Kafka/DB hiccup: events stay in the outbox, retry next tick
                log.warning("relay error, will retry: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        await producer.stop()
        await engine.dispose()
        log.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())

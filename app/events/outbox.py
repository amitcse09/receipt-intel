"""Transactional outbox: the fix for the "dual write" problem.

Problem: an endpoint that does `db.commit()` then `kafka.send()` can crash between the two → the receipt
exists but no event was sent (stuck in `pending` forever). Sending first and committing second has the
opposite bug (event for a receipt that doesn't exist). There is no transaction spanning Postgres AND Kafka.

Fix: write the event into an `outbox` table IN THE SAME DB TRANSACTION as the receipt. Either both are
saved or neither is. A separate relay process then publishes outbox rows to Kafka and marks them sent.
Guarantee: at-least-once delivery (a crash after send but before marking → the row is sent again), so
consumers must be idempotent.
"""

import json
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, String, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OutboxEvent(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)  # increasing → publish in commit order
    topic: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, default=None)


def add_event(session: AsyncSession, topic: str, key: str | int, event: BaseModel) -> None:
    """Stage an event in the caller's transaction. Nothing is sent until the relay picks it up after COMMIT."""
    session.add(OutboxEvent(topic=topic, key=str(key), payload=event.model_dump(mode="json")))


class Producer(Protocol):
    async def send_and_wait(self, topic: str, value: bytes, key: bytes): ...


async def relay_once(sessions: async_sessionmaker[AsyncSession], producer: Producer, batch: int = 100) -> int:
    """Publish up to `batch` unsent events, oldest first. Returns how many were published."""
    async with sessions() as s:
        rows = list(await s.scalars(
            select(OutboxEvent).where(OutboxEvent.published_at.is_(None)).order_by(OutboxEvent.id).limit(batch)
            # SKIP LOCKED: several relay replicas can run without sending the same row twice at the same time.
            .with_for_update(skip_locked=True)
        ))
        sent = 0
        try:
            for row in rows:
                # send_and_wait = wait for the broker's ack (acks=all) before marking the row as published.
                await producer.send_and_wait(row.topic, json.dumps(row.payload).encode(), key=row.key.encode())
                row.published_at = datetime.now(UTC)
                sent += 1
                # Crash right here → sent but not marked → re-sent next time (at-least-once, never lost).
        finally:
            await s.commit()  # keep progress even if a later send failed; the failed row stays unsent
        return sent

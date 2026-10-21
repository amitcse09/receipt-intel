"""Outbox, relay, idempotent consumer, DLQ — unit tests on SQLite + one real-Kafka integration test."""

import asyncio
import json
import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db import Base, Receipt
from app.events.outbox import OutboxEvent, add_event, relay_once
from app.events.schemas import ReceiptSubmitted
from app.events.topics import Topics
from app.llm.fake import FakeLLM
from app.workers import validator
from app.workers.validator import PoisonMessage, handle, process
from tests.conftest import HEADERS, RECEIPT_TEXT, good_extraction

TOPICS = Topics()


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_receipt(sessions, status="pending", rid=1) -> int:
    async with sessions() as s:
        s.add(Receipt(id=rid, member_id="alice", store="Big Bazaar", total=Decimal("499.50"),
                      purchased_on=date(2026, 10, 1), raw_text=RECEIPT_TEXT, status=status, review_notes=[]))
        await s.commit()
    return rid


def event_bytes(rid: int) -> bytes:
    return ReceiptSubmitted(receipt_id=rid, member_id="alice").model_dump_json().encode()


class FakeProducer:
    def __init__(self, fail_on: int | None = None):
        self.sent, self.fail_on = [], fail_on

    async def send_and_wait(self, topic, value, key=None, headers=None):
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise ConnectionError("broker unavailable")
        self.sent.append({"topic": topic, "value": value, "key": key, "headers": dict(headers or [])})


# ---------- the API writes receipt + event atomically ----------

async def test_kafka_mode_writes_outbox_row_instead_of_background_task(client, monkeypatch):
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, api_key="test-key", validation_mode="kafka")
    r = await client.post("/receipts", headers=HEADERS, json={
        "store": "Big Bazaar", "total": "499.50", "purchased_on": "2026-10-01", "raw_text": RECEIPT_TEXT})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    # still pending: nothing ran in-process — a worker will pick it up from Kafka
    r = await client.get(f"/receipts/{r.json()['id']}", headers=HEADERS)
    assert r.json()["status"] == "pending"
    import app.routers.receipts as receipts_router
    async with receipts_router.SessionLocal() as s:  # the test session factory (patched in conftest)
        rows = list(await s.scalars(select(OutboxEvent)))
    assert len(rows) == 1 and rows[0].topic == "receipts.submitted" and rows[0].published_at is None
    assert rows[0].payload["receipt_id"] == r.json()["id"] and rows[0].key == str(r.json()["id"])


async def test_outbox_row_and_receipt_roll_back_together(sessions):
    async with sessions() as s:
        s.add(Receipt(id=7, member_id="a", store="x", total=Decimal("1"), purchased_on=date(2026, 1, 1),
                      raw_text="x", status="pending", review_notes=[]))
        add_event(s, "t", 7, ReceiptSubmitted(receipt_id=7, member_id="a"))
        await s.rollback()  # e.g. a constraint failed: NEITHER row may survive
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Receipt)) == 0
        assert await s.scalar(select(func.count()).select_from(OutboxEvent)) == 0


# ---------- relay ----------

async def test_relay_publishes_in_order_once(sessions):
    async with sessions() as s:
        for rid in (1, 2, 3):
            add_event(s, "t", rid, ReceiptSubmitted(receipt_id=rid, member_id="a"))
        await s.commit()
    producer = FakeProducer()
    assert await relay_once(sessions, producer) == 3
    assert [json.loads(m["value"])["receipt_id"] for m in producer.sent] == [1, 2, 3]
    assert [m["key"] for m in producer.sent] == [b"1", b"2", b"3"]
    assert await relay_once(sessions, producer) == 0  # already marked published


async def test_relay_keeps_progress_when_broker_fails_mid_batch(sessions):
    async with sessions() as s:
        for rid in (1, 2, 3):
            add_event(s, "t", rid, ReceiptSubmitted(receipt_id=rid, member_id="a"))
        await s.commit()
    with pytest.raises(ConnectionError):
        await relay_once(sessions, FakeProducer(fail_on=1))  # 1st ok, 2nd fails
    async with sessions() as s:
        pending = list(await s.scalars(select(OutboxEvent.key).where(OutboxEvent.published_at.is_(None))))
    assert pending == ["2", "3"]  # 1 is marked sent; 2 and 3 will go next time — nothing lost
    later = FakeProducer()
    assert await relay_once(sessions, later) == 2


# ---------- validator: idempotent handler ----------

async def test_handle_validates_and_emits_followup_event(sessions):
    rid = await make_receipt(sessions)
    outcome = await handle(event_bytes(rid), sessions, FakeLLM(response=good_extraction()), TOPICS)
    assert outcome == "validated"
    async with sessions() as s:
        assert (await s.get(Receipt, rid)).status == "validated"
        events = list(await s.scalars(select(OutboxEvent)))
    assert len(events) == 1 and events[0].topic == "receipts.validated" and events[0].key == "alice"


async def test_duplicate_delivery_is_skipped(sessions):
    rid = await make_receipt(sessions)
    llm = FakeLLM(response=good_extraction())
    await handle(event_bytes(rid), sessions, llm, TOPICS)
    assert await handle(event_bytes(rid), sessions, llm, TOPICS) == "skip (already validated)"
    assert len(llm.calls) == 1  # the LLM (cost!) was not called again
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(OutboxEvent)) == 1  # one result event only


async def test_race_between_two_workers_produces_one_result(sessions):
    """Worker B finishes while worker A is still waiting on the LLM. A's conditional update must lose."""
    rid = await make_receipt(sessions)

    class SlowLLM(FakeLLM):
        async def generate_structured(self, system, prompt, schema):
            async with sessions() as s:  # meanwhile, "worker B" completes the same receipt
                await s.execute(update(Receipt).where(Receipt.id == rid).values(status="rejected"))
                await s.commit()
            return await super().generate_structured(system, prompt, schema)

    outcome = await handle(event_bytes(rid), sessions, SlowLLM(response=good_extraction()), TOPICS)
    assert outcome == "skip (another worker finished first)"
    async with sessions() as s:
        assert (await s.get(Receipt, rid)).status == "rejected"  # B's result kept, not overwritten
        assert await s.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.parametrize("raw", [b"not json", b'{"receipt_id": "x"}'])
async def test_malformed_payload_is_poison(sessions, raw):
    with pytest.raises(PoisonMessage):
        await handle(raw, sessions, FakeLLM(), TOPICS)


async def test_unknown_receipt_is_poison(sessions):
    with pytest.raises(PoisonMessage, match="does not exist"):
        await handle(event_bytes(404), sessions, FakeLLM(), TOPICS)


# ---------- validator: retries and dead-lettering ----------

def kafka_msg(value: bytes):
    return SimpleNamespace(value=value, key=b"k", topic="receipts.submitted", partition=2, offset=41)


async def test_poison_goes_straight_to_dlq_without_retries(sessions):
    dlq = FakeProducer()
    outcome = await process(kafka_msg(b"garbage"), sessions, FakeLLM(), TOPICS, dlq, validator_log())
    assert outcome.startswith("DEAD-LETTERED")
    [m] = dlq.sent
    assert m["topic"] == "receipts.submitted.dlq" and m["value"] == b"garbage"
    assert m["headers"]["source"] == b"receipts.submitted/2/41" and b"invalid payload" in m["headers"]["error"]


async def test_transient_errors_are_retried_then_dead_lettered(sessions, monkeypatch):
    calls = []

    async def flaky(*a):
        calls.append(1)
        raise RuntimeError("db connection reset")

    monkeypatch.setattr(validator, "handle", flaky)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(validator.asyncio, "sleep", lambda s: real_sleep(0))  # no real backoff in tests
    dlq = FakeProducer()
    outcome = await process(kafka_msg(b"{}"), sessions, FakeLLM(), TOPICS, dlq, validator_log())
    assert len(calls) == validator.MAX_ATTEMPTS and outcome.startswith("DEAD-LETTERED") and len(dlq.sent) == 1


async def test_success_does_not_touch_dlq(sessions):
    rid = await make_receipt(sessions)
    dlq = FakeProducer()
    outcome = await process(kafka_msg(event_bytes(rid)), sessions, FakeLLM(response=good_extraction()), TOPICS, dlq,
                            validator_log())
    assert outcome == "validated" and dlq.sent == []


def validator_log():
    import logging
    return logging.getLogger("test-validator")


# ---------- integration: real Kafka (skipped if not running) ----------

async def test_end_to_end_through_real_kafka(sessions):
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
    from aiokafka.errors import KafkaConnectionError

    from app.events.topics import delete_topics, ensure_topics

    bootstrap = "localhost:9092"
    topics = Topics(prefix=f"test-{uuid.uuid4().hex[:8]}.")  # isolated topics: never touches real ones
    try:
        await asyncio.wait_for(ensure_topics(bootstrap, topics), 5)
    except (KafkaConnectionError, OSError, TimeoutError) as e:
        pytest.skip(f"Kafka not available ({type(e).__name__}); run `docker compose up -d`")
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, acks="all", enable_idempotence=True)
    consumer = AIOKafkaConsumer(topics.submitted, bootstrap_servers=bootstrap, group_id=f"g-{uuid.uuid4().hex[:6]}",
                                auto_offset_reset="earliest", enable_auto_commit=False)
    await producer.start()
    await consumer.start()
    try:
        rid = await make_receipt(sessions)
        async with sessions() as s:
            add_event(s, topics.submitted, rid, ReceiptSubmitted(receipt_id=rid, member_id="alice"))
            await s.commit()
        assert await relay_once(sessions, producer) == 1                     # outbox → Kafka
        msg = await asyncio.wait_for(consumer.getone(), 15)                  # Kafka → consumer
        assert msg.key == str(rid).encode()
        assert await handle(msg.value, sessions, FakeLLM(response=good_extraction()), topics) == "validated"
        async with sessions() as s:
            assert (await s.get(Receipt, rid)).status == "validated"
    finally:
        await consumer.stop()
        await producer.stop()
        await delete_topics(bootstrap, topics)

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.db import Base, get_session
from app.extraction import ExtractedReceipt, LineItem
from app.llm import get_llm
from app.llm.fake import FakeLLM
from app.main import app

HEADERS = {"X-API-Key": "test-key"}

RECEIPT_TEXT = """BIG BAZAAR - Koramangala
Date: 01/10/2026
Rice 5kg        320.00
Milk 1L          60.00
Bread           119.50
TOTAL INR       499.50"""


def good_extraction(**overrides) -> ExtractedReceipt:
    data = dict(
        store_name="Big Bazaar",
        total=499.50,
        currency="INR",
        purchased_on="2026-10-01",
        line_items=[LineItem(name="Rice 5kg", amount=320), LineItem(name="Milk 1L", amount=60), LineItem(name="Bread", amount=119.5)],
        looks_tampered=False,
        tamper_signals=[],
        confidence=0.95,
    )
    return ExtractedReceipt(**(data | overrides))


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM(response=good_extraction())


@pytest.fixture
async def client(monkeypatch, fake_llm):
    # Fresh in-memory DB per test, wired in via dependency_overrides — the main reason DI is great for testing.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    TestSession = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.routers.receipts.SessionLocal", TestSession)  # background task uses this

    async def override_session():
        async with TestSession() as s:
            yield s

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, api_key="test-key")  # ignore dev .env
    app.dependency_overrides[get_llm] = lambda: fake_llm  # no network, no API key, deterministic
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()

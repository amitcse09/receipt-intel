import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import text

from app.db import Base, engine
from app.events import outbox  # noqa: F401 — registers the outbox table with Base.metadata
from app.rag.store import RagBase
from app.routers import assistant, policies, receipts

logging.basicConfig(level=logging.INFO)


async def init_rag_schema() -> None:
    """pgvector tables only exist on Postgres; on SQLite the RAG endpoints return 503."""
    if engine.dialect.name != "postgresql":
        return
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(RagBase.metadata.create_all)


async def migrate_dev_schema() -> None:
    """Poor man's migration for this learning project: create_all() never ALTERs existing tables.
    Real projects use Alembic (≈ Django migrations) — versioned, reviewed, reversible."""
    if engine.dialect.name != "postgresql":
        return
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE receipts ADD COLUMN IF NOT EXISTS member_id VARCHAR(64) NOT NULL DEFAULT 'demo-member'"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_receipts_member_id ON receipts (member_id)"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: runs once before serving traffic (create pools, warm caches, connect Kafka producer...).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)  # real projects use Alembic migrations instead
    await migrate_dev_schema()
    await init_rag_schema()
    yield
    # Shutdown: release resources cleanly.
    await engine.dispose()


app = FastAPI(title="Receipt Intelligence Service", lifespan=lifespan)
app.include_router(receipts.router)
app.include_router(policies.router)
app.include_router(assistant.router)


@app.middleware("http")
async def timing_header(request: Request, call_next):
    """≈ Django middleware. OpenTelemetry (Module 4) will replace this with real tracing."""
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time-ms"] = f"{(time.perf_counter() - start) * 1000:.1f}"
    return response


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}

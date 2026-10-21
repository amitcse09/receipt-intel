"""RAG tests: pure logic (always run) + real pgvector integration (skipped if Postgres isn't up)."""

import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.llm.fake import FakeLLM
from app.rag.answer import LLMAnswer, answer_question, finalize
from app.rag.chunking import chunk_markdown
from app.rag.embeddings import FakeEmbedder
from app.rag.store import Hit, RagBase, hybrid_search, ingest, rrf

POLICIES = Path(__file__).resolve().parent.parent / "data" / "policies"


# ---------- chunking ----------

def test_chunks_follow_headings_and_carry_context():
    chunks = chunk_markdown("claims", (POLICIES / "claims.md").read_text())
    assert [c.section for c in chunks] == ["Submission window", "Receipt requirements", "Limits", "Processing time"]
    assert chunks[0].content.startswith("PaisaBack Rewards — Submitting Claims > Submission window\n")


def test_long_section_split_on_sentences():
    body = " ".join(f"Rule number {i} applies here." for i in range(100))
    chunks = chunk_markdown("d", f"# T\n## S\n{body}", max_chars=300)
    assert len(chunks) > 1
    assert all(len(c.content) <= 300 for c in chunks)
    assert all(c.content.rstrip().endswith(".") for c in chunks)  # never cut mid-sentence


# ---------- fusion ----------

def test_rrf_rewards_agreement_between_retrievers():
    scores = rrf([[1, 2, 3], [3, 4, 1]])
    assert max(scores, key=scores.get) in (1, 3)  # found by both lists
    assert scores[1] > scores[2] and scores[3] > scores[4]


# ---------- answer guardrails ----------

def _hit(i: int) -> Hit:
    return Hit(id=i, doc=f"doc{i}", section="s", content=f"content {i}", score=1.0, vector_rank=i, keyword_rank=None)


def test_finalize_drops_hallucinated_citations():
    out = finalize(LLMAnswer(answer="x", citations=[1, 7], answerable=True), [_hit(1), _hit(2)])
    assert [s.n for s in out.sources] == [1] and out.answerable


def test_finalize_downgrades_answer_without_valid_citations():
    out = finalize(LLMAnswer(answer="Cashback is 50%", citations=[9], answerable=True), [_hit(1)])
    assert not out.answerable and "50%" not in out.answer


async def test_no_hits_means_no_llm_call():
    llm = FakeLLM(fail=True)
    out = await answer_question("anything", [], llm)
    assert not out.answerable and llm.calls == []


# ---------- integration: real Postgres + pgvector ----------

PG_URL = os.getenv("TEST_PG_URL", "postgresql+asyncpg://receipts:receipts@localhost:5433/receipts")


@pytest.fixture
async def pg_session():
    # Isolated schema so tests never touch the real policy_chunks table. schema_translate_map rewrites every
    # table reference to rag_test. (A search_path alone is NOT enough: SQLAlchemy's "table exists?" check
    # finds public.policy_chunks through the path, skips creating the test table, and the test writes to prod.)
    engine = create_async_engine(PG_URL).execution_options(schema_translate_map={None: "rag_test"})
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(text("DROP SCHEMA IF EXISTS rag_test CASCADE"))
            await conn.execute(text("CREATE SCHEMA rag_test"))
            await conn.run_sync(RagBase.metadata.create_all)
    except Exception as e:  # Postgres not running → skip, don't fail
        await engine.dispose()
        pytest.skip(f"Postgres not available ({type(e).__name__}); run `docker compose up -d`")
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA rag_test CASCADE"))
    await engine.dispose()


async def test_ingest_is_idempotent_and_search_finds_exact_codes(pg_session):
    emb = FakeEmbedder()
    chunks = chunk_markdown("disputes-and-fraud", (POLICIES / "disputes-and-fraud.md").read_text())
    first = await ingest(pg_session, "disputes-and-fraud", chunks, emb)
    second = await ingest(pg_session, "disputes-and-fraud", chunks, emb)
    assert first["added"] == len(chunks) and second == {"added": 0, "deleted": 0, "unchanged": len(chunks)}

    hits = await hybrid_search(pg_session, "PB-APPEAL-7", emb, k=1)
    assert hits[0].section == "Appealing a rejected claim"
    assert hits[0].keyword_rank == 1  # the exact code is a keyword match


async def test_edited_doc_replaces_stale_chunks(pg_session):
    emb = FakeEmbedder()
    await ingest(pg_session, "d", chunk_markdown("d", "# T\n## A\nOld rule.\n## B\nOther rule."), emb)
    stats = await ingest(pg_session, "d", chunk_markdown("d", "# T\n## A\nNew rule.\n## B\nOther rule."), emb)
    assert stats == {"added": 1, "deleted": 1, "unchanged": 1}

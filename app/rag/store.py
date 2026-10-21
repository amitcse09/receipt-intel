"""pgvector storage + hybrid retrieval (vector similarity + Postgres full-text, merged with RRF)."""

from dataclasses import dataclass

from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, String, Text, delete, func, select, text
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.rag.chunking import Chunk
from app.rag.embeddings import Embedder

EMBED_DIM = 768


class RagBase(DeclarativeBase):
    """Separate metadata: these tables need Postgres + pgvector, the receipts tables don't."""


class PolicyChunk(RagBase):
    __tablename__ = "policy_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc: Mapped[str] = mapped_column(String(200), index=True)
    section: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    embedding_model: Mapped[str] = mapped_column(String(100))
    embedding = mapped_column(Vector(EMBED_DIM))

    __table_args__ = (
        # HNSW = approximate nearest-neighbour graph index; cosine to match how we query.
        Index("ix_policy_chunks_embedding", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        # GIN index over the full-text vector for keyword search.
        Index("ix_policy_chunks_fts", text("to_tsvector('english', content)"), postgresql_using="gin"),
    )


@dataclass
class Hit:
    id: int
    doc: str
    section: str
    content: str
    score: float  # RRF score (higher = better)
    vector_rank: int | None
    keyword_rank: int | None


async def ingest(session: AsyncSession, doc: str, chunks: list[Chunk], embedder: Embedder) -> dict[str, int]:
    """Idempotent: unchanged chunks are skipped (no re-embedding cost), removed ones deleted."""
    existing = dict((await session.execute(
        select(PolicyChunk.content_hash, PolicyChunk.embedding_model).where(PolicyChunk.doc == doc)
    )).all())
    wanted = {c.content_hash: c for c in chunks}
    # Re-embed if content changed OR the embedding model changed (different vector space).
    to_add = [c for h, c in wanted.items() if existing.get(h) != embedder.model]
    stale = [h for h in existing if h not in wanted or existing[h] != embedder.model]

    if stale:
        await session.execute(delete(PolicyChunk).where(PolicyChunk.content_hash.in_(stale)))
    if to_add:
        vectors = await embedder.embed([c.content for c in to_add], kind="document")
        session.add_all(
            PolicyChunk(doc=c.doc, section=c.section, content=c.content, content_hash=c.content_hash,
                        embedding_model=embedder.model, embedding=v)
            for c, v in zip(to_add, vectors, strict=True)
        )
    await session.commit()
    return {"added": len(to_add), "deleted": len(stale), "unchanged": len(wanted) - len(to_add)}


async def vector_search(session: AsyncSession, query_vec: list[float], k: int) -> list[int]:
    dist = PolicyChunk.embedding.cosine_distance(query_vec)
    rows = await session.scalars(select(PolicyChunk.id).order_by(dist).limit(k))
    return list(rows)


async def keyword_search(session: AsyncSession, query: str, k: int) -> list[int]:
    # plainto_tsquery ANDs every word — too strict for questions. Swap & for | to OR them; ranking
    # still rewards chunks that match more (and rarer) words.
    tsq = func.replace(func.plainto_tsquery("english", query).cast(Text), "&", "|").cast(TSQUERY)
    tsv = func.to_tsvector("english", PolicyChunk.content)
    rows = await session.scalars(
        select(PolicyChunk.id).where(tsv.op("@@")(tsq)).order_by(func.ts_rank_cd(tsv, tsq).desc()).limit(k)
    )
    return list(rows)


def rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion: score = Σ 1/(k + rank). Uses ranks only, so no need to normalise scores
    from very different systems (cosine distance vs ts_rank)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


async def hybrid_search(session: AsyncSession, query: str, embedder: Embedder, k: int = 4, pool: int = 10) -> list[Hit]:
    [query_vec] = await embedder.embed([query], kind="query")
    vec_ids = await vector_search(session, query_vec, pool)
    kw_ids = await keyword_search(session, query, pool)
    scores = rrf([vec_ids, kw_ids])
    top = sorted(scores, key=scores.get, reverse=True)[:k]
    rows = {c.id: c for c in await session.scalars(select(PolicyChunk).where(PolicyChunk.id.in_(top)))}
    return [
        Hit(id=i, doc=rows[i].doc, section=rows[i].section, content=rows[i].content, score=scores[i],
            vector_rank=vec_ids.index(i) + 1 if i in vec_ids else None,
            keyword_rank=kw_ids.index(i) + 1 if i in kw_ids else None)
        for i in top
    ]

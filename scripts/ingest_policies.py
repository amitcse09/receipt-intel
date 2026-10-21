"""Chunk + embed + store every doc in data/policies. Safe to re-run (only changed chunks are re-embedded).

Usage: uv run python -m scripts.ingest_policies
"""

import asyncio
from pathlib import Path

from sqlalchemy import delete

from app.db import SessionLocal, engine
from app.main import init_rag_schema
from app.rag.chunking import chunk_markdown
from app.rag.embeddings import get_embedder
from app.rag.store import PolicyChunk, ingest

POLICY_DIR = Path(__file__).resolve().parent.parent / "data" / "policies"


async def main():
    await init_rag_schema()
    embedder = get_embedder()
    print(f"embedding model: {embedder.model} ({embedder.dim} dims)")
    paths = sorted(POLICY_DIR.glob("*.md"))
    async with SessionLocal() as session:
        # A deleted source file must also leave the index, or the bot keeps quoting a retired policy.
        gone = await session.execute(delete(PolicyChunk).where(PolicyChunk.doc.not_in([p.stem for p in paths])))
        await session.commit()
        if gone.rowcount:
            print(f"removed {gone.rowcount} chunks from deleted docs")
        for path in paths:
            chunks = chunk_markdown(path.stem, path.read_text())
            stats = await ingest(session, path.stem, chunks, embedder)
            print(f"{path.name:28} {len(chunks)} chunks  {stats}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

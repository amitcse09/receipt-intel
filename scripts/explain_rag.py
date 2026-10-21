"""Print every RAG stage for one question, so you can SEE what each step does.

Usage: uv run python -m scripts.explain_rag ["your question"]
"""

import asyncio
import sys

from sqlalchemy import func, select

from app.db import SessionLocal, engine
from app.llm import get_llm
from app.rag.answer import SYSTEM_PROMPT, LLMAnswer, build_prompt, finalize
from app.rag.embeddings import get_embedder
from app.rag.store import PolicyChunk, keyword_search, rrf, vector_search


async def main():
    question = sys.argv[1] if len(sys.argv) > 1 else "Can I get cashback on beer?"
    embedder, llm = get_embedder(), get_llm()
    async with SessionLocal() as session:
        print(f"QUESTION: {question}\n")

        # 1. The question becomes a vector
        [qvec] = await embedder.embed([question], kind="query")
        print(f"1) EMBED  {embedder.model}: {len(qvec)} numbers, first 5 = {[round(x, 3) for x in qvec[:5]]}\n")

        # 2. Vector search: cosine similarity (1 - distance) against EVERY chunk, best first
        sim = (1 - PolicyChunk.embedding.cosine_distance(qvec)).label("sim")
        rows = (await session.execute(select(PolicyChunk.id, PolicyChunk.doc, PolicyChunk.section, sim).order_by(sim.desc()))).all()
        print("2) VECTOR SEARCH  cosine similarity to all chunks (1.0 = same meaning):")
        for i, r in enumerate(rows, 1):
            print(f"   v{i:<2} {r.sim:.3f}  {r.doc} > {r.section}")

        # 3. Keyword search: what Postgres actually searches for
        tsq = await session.scalar(select(func.replace(func.plainto_tsquery("english", question).cast(PolicyChunk.content.type), "&", "|")))
        kw_ids = await keyword_search(session, question, 10)
        names = {r.id: f"{r.doc} > {r.section}" for r in rows}
        print(f"\n3) KEYWORD SEARCH  tsquery = {tsq}   (stop words dropped, words stemmed)")
        for i, cid in enumerate(kw_ids, 1):
            print(f"   k{i:<2} {names[cid]}")

        # 4. Fusion
        vec_ids = await vector_search(session, qvec, 10)
        scores = rrf([vec_ids, kw_ids])
        top = sorted(scores, key=scores.get, reverse=True)[:4]
        print("\n4) RRF FUSION  score = 1/(60+vector_rank) + 1/(60+keyword_rank) → top 4:")
        for cid in top:
            v = vec_ids.index(cid) + 1 if cid in vec_ids else None
            k = kw_ids.index(cid) + 1 if cid in kw_ids else None
            parts = " + ".join(p for p in [f"1/(60+{v})" if v else "", f"1/(60+{k})" if k else ""] if p)
            print(f"   {scores[cid]:.4f} = {parts:22} {names[cid]}")

        # 5. Prompt actually sent to the LLM
        chunks = {c.id: c for c in await session.scalars(select(PolicyChunk).where(PolicyChunk.id.in_(top)))}
        from app.rag.store import Hit
        hits = [Hit(id=c, doc=chunks[c].doc, section=chunks[c].section, content=chunks[c].content,
                    score=scores[c], vector_rank=None, keyword_rank=None) for c in top]
        prompt = build_prompt(question, hits)
        print(f"\n5) PROMPT  system ({len(SYSTEM_PROMPT)} chars) + user ({len(prompt)} chars). User part:\n")
        print("   " + prompt.replace("\n", "\n   "))

        # 6. Raw model output, then code verification
        raw = await llm.generate_structured(SYSTEM_PROMPT, prompt, LLMAnswer)
        print(f"\n6) RAW LLM OUTPUT  {raw.model_dump()}")
        final = finalize(raw, hits)
        print(f"7) AFTER finalize()  answerable={final.answerable}, cited={[f'{s.doc} > {s.section}' for s in final.sources]}")
        print(f"   ANSWER: {final.answer}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

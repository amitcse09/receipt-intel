from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.deps import LLMDep, SessionDep, require_api_key
from app.llm import LLMError
from app.rag.answer import PolicyAnswer, answer_question
from app.rag.embeddings import Embedder, get_embedder
from app.rag.store import hybrid_search

router = APIRouter(prefix="/policies", tags=["policies (RAG)"], dependencies=[Depends(require_api_key)])

EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    k: int = Field(default=4, ge=1, le=10)


class AskOut(PolicyAnswer):
    retrieved: list[dict]  # debug view: what retrieval found and why (vector vs keyword rank)


@router.post("/ask", response_model=AskOut)
async def ask(payload: AskIn, session: SessionDep, embedder: EmbedderDep, llm: LLMDep):
    if session.bind.dialect.name != "postgresql":
        raise HTTPException(503, "RAG needs Postgres + pgvector: `docker compose up -d` and set DATABASE_URL")
    try:
        hits = await hybrid_search(session, payload.question, embedder, k=payload.k)
        result = await answer_question(payload.question, hits, llm)
    except LLMError as e:
        raise HTTPException(502, str(e))
    return AskOut(
        **result.model_dump(),
        retrieved=[{"doc": h.doc, "section": h.section, "rrf": round(h.score, 4),
                    "vector_rank": h.vector_rank, "keyword_rank": h.keyword_rank} for h in hits],
    )

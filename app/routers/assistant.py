from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent.llm import AgentLLM
from app.agent.loop import get_agent_llm, run_agent
from app.agent.tools import ToolContext
from app.db import SessionLocal
from app.deps import MemberDep, require_api_key
from app.llm import LLMError
from app.rag.embeddings import Embedder, get_embedder

router = APIRouter(prefix="/assistant", tags=["assistant (tool calling)"], dependencies=[Depends(require_api_key)])

AgentLLMDep = Annotated[AgentLLM, Depends(get_agent_llm)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


def get_session_factory():
    """Tools open their own short sessions (one per tool call). Overridable in tests."""
    return SessionLocal


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class StepOut(BaseModel):
    tool: str
    args: dict
    result: dict
    ms: int


class ChatOut(BaseModel):
    answer: str
    steps: list[StepOut]  # every tool call: what the model asked for, what our code returned
    stopped_early: bool


@router.post("/chat", response_model=ChatOut)
async def chat(payload: ChatIn, member: MemberDep, llm: AgentLLMDep, embedder: EmbedderDep,
               sessions=Depends(get_session_factory)):
    ctx = ToolContext(member_id=member, sessions=sessions, embedder=embedder)  # identity from auth, not the model
    try:
        result = await run_agent(payload.message, ctx, llm)
    except LLMError as e:
        raise HTTPException(502, str(e))
    return ChatOut(answer=result.answer, steps=[StepOut(**vars(s)) for s in result.steps],
                   stopped_early=result.stopped_early)

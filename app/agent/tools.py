"""The tools an LLM may call — defined ONCE, used by both the in-app agent and the MCP server.

Rules every tool here follows (they're the interview answer to "how do you make tool calling safe?"):
1. Identity comes from `ToolContext.member_id` (set by the server from the authenticated caller) — it is
   NEVER a tool argument, so the model can't be talked into reading another member's data.
2. Arguments are validated with Pydantic before anything runs; bad args return an error the model can read.
3. Tools return small, structured results — no raw receipt text (untrusted, could carry prompt injection).
4. All tools here are read-only. A write tool (e.g. "file an appeal") would need human confirmation.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import cashback
from app.db import Receipt
from app.rag.embeddings import Embedder
from app.rag.store import hybrid_search


@dataclass
class ToolContext:
    member_id: str  # WHO is asking — from auth, never from the model
    sessions: async_sessionmaker[AsyncSession]
    embedder: Embedder | None = None


# ---------- argument schemas (also become the JSON schema the model sees) ----------

class ListReceiptsArgs(BaseModel):
    status: Literal["pending", "validated", "rejected", "needs_review"] | None = Field(
        None, description="Only receipts with this status. Omit for all.")
    limit: int = Field(5, ge=1, le=20, description="Max receipts to return, newest first.")


class ReceiptIdArgs(BaseModel):
    receipt_id: int = Field(description="The receipt's numeric id, e.g. 42.")


class SearchPoliciesArgs(BaseModel):
    query: str = Field(min_length=3, max_length=300, description="What to look up in the program rules.")


class CashbackArgs(BaseModel):
    amount: float = Field(gt=0, description="Eligible receipt amount in rupees.")  # plain JSON type for the model
    tier: cashback.Tier = Field(description="Member tier: silver, gold or platinum.")
    store: str = Field(min_length=1, description="Store name as printed on the receipt.")


# ---------- handlers ----------

def _receipt_summary(r: Receipt) -> dict:
    return {"id": r.id, "store": r.store, "total": str(r.total), "purchased_on": r.purchased_on.isoformat(),
            "status": r.status}


async def list_my_receipts(ctx: ToolContext, args: ListReceiptsArgs) -> dict:
    async with ctx.sessions() as s:
        q = select(Receipt).where(Receipt.member_id == ctx.member_id)  # scoped to the caller, always
        if args.status:
            q = q.where(Receipt.status == args.status)
        rows = await s.scalars(q.order_by(Receipt.id.desc()).limit(args.limit))
        return {"receipts": [_receipt_summary(r) for r in rows]}


async def get_receipt_status(ctx: ToolContext, args: ReceiptIdArgs) -> dict:
    async with ctx.sessions() as s:
        r = await s.get(Receipt, args.receipt_id)
        if r is None or r.member_id != ctx.member_id:
            return {"error": f"receipt {args.receipt_id} not found"}  # same answer either way: no IDOR leak
        return _receipt_summary(r) | {"review_notes": r.review_notes}


async def search_policies(ctx: ToolContext, args: SearchPoliciesArgs) -> dict:
    if ctx.embedder is None:
        return {"error": "policy search unavailable"}
    async with ctx.sessions() as s:
        hits = await hybrid_search(s, args.query, ctx.embedder, k=3)
    return {"results": [{"source": f"{h.doc} > {h.section}", "text": h.content} for h in hits]}


async def calculate_cashback(ctx: ToolContext, args: CashbackArgs) -> dict:
    return cashback.calculate(Decimal(str(args.amount)).quantize(Decimal("0.01")), args.tier, args.store)  # money maths in Decimal


# ---------- registry ----------

@dataclass
class Tool:
    name: str
    description: str  # the model chooses tools by reading this — write it like API docs for a new teammate
    args_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Awaitable[dict]]
    json_schema: dict = field(init=False)

    def __post_init__(self):
        self.json_schema = self.args_model.model_json_schema()


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("list_my_receipts",
         "List the current member's submitted receipts (id, store, total, date, status), newest first. "
         "Use when the member asks about 'my receipts/claims' without giving an id.",
         ListReceiptsArgs, list_my_receipts),
    Tool("get_receipt_status",
         "Get one of the current member's receipts by id, including why it was rejected or sent for review "
         "(review_notes).",
         ReceiptIdArgs, get_receipt_status),
    Tool("search_policies",
         "Search the PaisaBack Rewards program rules (eligibility, deadlines, limits, appeals, returns, fraud). "
         "Use for any question about what the rules say. Returns passages with their source.",
         SearchPoliciesArgs, search_policies),
    Tool("calculate_cashback",
         "Compute the exact cashback for an amount, tier and store, applying partner bonus and caps. "
         "ALWAYS use this instead of doing cashback arithmetic yourself.",
         CashbackArgs, calculate_cashback),
]}


MAX_RESULT_CHARS = 4000  # keep tool output from flooding the context window


async def execute(name: str, raw_args: dict, ctx: ToolContext) -> dict:
    """Run one tool call from the model. Never raises: errors become results the model can read and react to."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}; available: {sorted(TOOLS)}"}
    try:
        args = tool.args_model.model_validate(raw_args)
    except ValidationError as e:
        return {"error": "invalid arguments", "details": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]}
    try:
        result = await tool.handler(ctx, args)
    except Exception as e:  # a failing tool must not crash the conversation
        return {"error": f"{name} failed: {type(e).__name__}"}
    text = str(result)
    if len(text) > MAX_RESULT_CHARS:
        return {"error": "result too large", "preview": text[:MAX_RESULT_CHARS]}
    return result

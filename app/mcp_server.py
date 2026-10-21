"""MCP server: exposes the SAME tools as the in-app assistant (app/agent/tools.py) to any MCP client —
Claude Code, Claude Desktop, Cursor, …  Each wrapper below is thin: real logic lives in tools.execute().

Run (stdio, which is how local MCP clients launch it):
    uv run python -m app.mcp_server
Identity: a local stdio server runs as ONE member, configured by the person who installs it
(RECEIPT_INTEL_MEMBER_ID). The calling model can't change it. A remote (HTTP) MCP server would instead
authenticate each client with OAuth and derive the member from the token.

Note: stdio servers talk JSON-RPC over stdout — never print() to stdout here; logs go to stderr.
"""

import os
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from app.agent.tools import TOOLS, ToolContext, execute
from app.db import SessionLocal
from app.rag.embeddings import get_embedder

MEMBER_ID = os.getenv("RECEIPT_INTEL_MEMBER_ID", "demo-member")

mcp = MCPServer(
    "receipt-intel",
    instructions="PaisaBack Rewards: look up the configured member's receipts, search program rules, "
                 "and compute exact cashback. All tools are read-only.",
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def _ctx() -> ToolContext:
    return ToolContext(member_id=MEMBER_ID, sessions=SessionLocal, embedder=get_embedder())


@mcp.tool(description=TOOLS["list_my_receipts"].description, annotations=READ_ONLY)
async def list_my_receipts(
    status: Literal["pending", "validated", "rejected", "needs_review"] | None = None, limit: int = 5
) -> dict:
    return await execute("list_my_receipts", {"status": status, "limit": limit}, _ctx())


@mcp.tool(description=TOOLS["get_receipt_status"].description, annotations=READ_ONLY)
async def get_receipt_status(receipt_id: int) -> dict:
    return await execute("get_receipt_status", {"receipt_id": receipt_id}, _ctx())


@mcp.tool(description=TOOLS["search_policies"].description, annotations=READ_ONLY)
async def search_policies(query: str) -> dict:
    return await execute("search_policies", {"query": query}, _ctx())


@mcp.tool(description=TOOLS["calculate_cashback"].description, annotations=READ_ONLY)
async def calculate_cashback(amount: float, tier: Literal["silver", "gold", "platinum"], store: str) -> dict:
    return await execute("calculate_cashback", {"amount": amount, "tier": tier, "store": store}, _ctx())


if __name__ == "__main__":
    mcp.run("stdio")

"""Talk to our MCP server exactly like Claude Code does: launch it as a subprocess, speak JSON-RPC over stdio.

Usage: uv run python -m scripts.try_mcp
"""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main():
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(ROOT),
        env={**os.environ, "RECEIPT_INTEL_MEMBER_ID": "demo-member"},
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()  # handshake: protocol version + capabilities
        print(f"connected to {init.server_info.name} (protocol {init.protocol_version})")
        print("instructions:", init.instructions)

        tools = (await session.list_tools()).tools
        print("\ntools:", [t.name for t in tools])

        for name, args in [
            ("list_my_receipts", {"status": "rejected"}),
            ("get_receipt_status", {"receipt_id": 5}),  # someone else's receipt
            ("calculate_cashback", {"amount": 3000, "tier": "gold", "store": "FreshKart"}),
            ("search_policies", {"query": "appeal deadline"}),
        ]:
            r = await session.call_tool(name, args)
            text = " ".join(c.text for c in r.content if hasattr(c, "text")).replace("\n", " ")
            print(f"\n{name}({args})\n  → {' '.join(text.split())[:230]}")


if __name__ == "__main__":
    asyncio.run(main())

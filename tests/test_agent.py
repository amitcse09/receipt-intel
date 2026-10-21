"""Tool calling + MCP tests: real tools on an in-memory DB, a scripted model, no network."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import cashback
from app.agent.llm import ScriptedAgentLLM, ToolCall, Turn
from app.agent.loop import get_agent_llm, run_agent
from app.agent.tools import TOOLS, ToolContext, execute
from app.db import Base, Receipt
from app.routers.assistant import get_session_factory
from tests.conftest import HEADERS


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add_all([
            Receipt(id=1, member_id="alice", store="Big Bazaar", total=Decimal("4995.00"), purchased_on=date(2026, 10, 1),
                    raw_text="x", status="rejected", review_notes=["total: claimed 4995.00, receipt shows 499.5"]),
            Receipt(id=2, member_id="alice", store="FreshKart", total=Decimal("1250.00"), purchased_on=date(2026, 10, 12),
                    raw_text="x", status="validated", review_notes=[]),
            Receipt(id=3, member_id="bob", store="Nilgiri Mart", total=Decimal("8000.00"), purchased_on=date(2026, 10, 3),
                    raw_text="SECRET", status="validated", review_notes=[]),
        ])
        await s.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def alice(sessions) -> ToolContext:
    return ToolContext(member_id="alice", sessions=sessions)


# ---------- cashback: deterministic money maths ----------

@pytest.mark.parametrize("amount,tier,store,expected", [
    ("3000", "gold", "FreshKart", "120.00"),             # 2% + 2% partner
    ("3000", "gold", "Big Bazaar", "60.00"),             # not a partner
    ("20000", "platinum", "Nilgiri Mart", "500.00"),     # 5% = 1000 → capped
    ("150", "platinum", "FreshKart", "0.00"),            # under ₹200 minimum
])
def test_cashback(amount, tier, store, expected):
    assert cashback.calculate(Decimal(amount), tier, store)["cashback"] == expected


# ---------- tools: authorization, validation, failure handling ----------

async def test_list_is_scoped_to_the_caller(alice):
    out = await execute("list_my_receipts", {}, alice)
    assert {r["id"] for r in out["receipts"]} == {1, 2}  # never bob's receipt 3


async def test_other_members_receipt_looks_missing(alice):
    out = await execute("get_receipt_status", {"receipt_id": 3}, alice)
    assert out == {"error": "receipt 3 not found"}  # identical to a truly missing id
    assert await execute("get_receipt_status", {"receipt_id": 999}, alice) == {"error": "receipt 999 not found"}


async def test_tools_never_return_raw_receipt_text(sessions):
    out = await execute("get_receipt_status", {"receipt_id": 3}, ToolContext("bob", sessions))
    assert "raw_text" not in out and "SECRET" not in str(out)


async def test_member_id_is_not_a_tool_argument():
    """The model can't even express 'show me bob's data' — identity isn't in any schema."""
    identity_words = ("member", "user", "owner", "account", "customer")
    for tool in TOOLS.values():
        for arg in tool.json_schema["properties"]:
            assert not any(w in arg.lower() for w in identity_words), f"{tool.name}.{arg}"


async def test_bad_arguments_become_readable_errors(alice):
    out = await execute("get_receipt_status", {"receipt_id": "abc"}, alice)
    assert out["error"] == "invalid arguments" and "receipt_id" in out["details"][0]
    out = await execute("calculate_cashback", {"amount": 100, "tier": "diamond", "store": "x"}, alice)
    assert out["error"] == "invalid arguments"


async def test_unknown_tool_and_crashing_tool(alice, monkeypatch):
    assert "unknown tool" in (await execute("delete_everything", {}, alice))["error"]

    async def boom(ctx, args):
        raise RuntimeError("db down")
    monkeypatch.setattr(TOOLS["list_my_receipts"], "handler", boom)
    assert (await execute("list_my_receipts", {}, alice)) == {"error": "list_my_receipts failed: RuntimeError"}


async def test_search_without_embedder_is_an_error_not_a_crash(alice):
    assert "error" in await execute("search_policies", {"query": "appeal deadline"}, alice)


# ---------- the agent loop (scripted model) ----------

async def test_loop_runs_tools_and_feeds_results_back(alice):
    llm = ScriptedAgentLLM([
        Turn(text=None, calls=[ToolCall("list_my_receipts", {"status": "rejected"})]),
        Turn(text=None, calls=[ToolCall("get_receipt_status", {"receipt_id": 1})]),
        Turn(text="Receipt 1 was rejected: the total didn't match."),
    ])
    result = await run_agent("why was my receipt rejected?", alice, llm)
    assert result.answer.startswith("Receipt 1 was rejected") and not result.stopped_early
    assert [s.tool for s in result.steps] == ["list_my_receipts", "get_receipt_status"]
    # the model really received our tool output
    kind, results = llm.conversation.sent[2]
    assert kind == "tool_results" and results[0][1]["review_notes"] == ["total: claimed 4995.00, receipt shows 499.5"]
    assert set(llm.started_with[1]) == set(TOOLS)


async def test_parallel_tool_calls_in_one_turn(alice):
    llm = ScriptedAgentLLM([
        Turn(text=None, calls=[ToolCall("get_receipt_status", {"receipt_id": 1}),
                               ToolCall("get_receipt_status", {"receipt_id": 2})]),
        Turn(text="done"),
    ])
    result = await run_agent("status of 1 and 2?", alice, llm)
    assert len(result.steps) == 2 and len(llm.conversation.sent[1][1]) == 2


async def test_loop_stops_runaway_tool_calls(alice):
    endless = [Turn(text=None, calls=[ToolCall("list_my_receipts", {})]) for _ in range(10)]
    result = await run_agent("loop forever", alice, ScriptedAgentLLM(endless), max_steps=3)
    assert result.stopped_early and len(result.steps) == 3


# ---------- the HTTP endpoint ----------

async def test_chat_endpoint_uses_the_authenticated_member(client, sessions):
    from app.main import app

    llm = ScriptedAgentLLM([Turn(text=None, calls=[ToolCall("list_my_receipts", {})]), Turn(text="You have 1 receipt.")])
    app.dependency_overrides[get_agent_llm] = lambda: llm
    app.dependency_overrides[get_session_factory] = lambda: sessions
    r = await client.post("/assistant/chat", json={"message": "my receipts?"}, headers=HEADERS | {"X-Member-Id": "bob"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "You have 1 receipt."
    assert [x["id"] for x in body["steps"][0]["result"]["receipts"]] == [3]  # bob's only


# ---------- MCP server: same tools, same rules ----------

async def test_mcp_exposes_the_same_tools_with_matching_schemas():
    from app.mcp_server import mcp

    listed = {t.name: t for t in await mcp.list_tools()}
    assert set(listed) == set(TOOLS)
    for name, tool in TOOLS.items():
        assert set(listed[name].input_schema["properties"]) == set(tool.json_schema["properties"]), name
        assert listed[name].annotations.read_only_hint is True


async def test_mcp_call_is_scoped_to_configured_member(sessions, monkeypatch):
    import app.mcp_server as server

    monkeypatch.setattr(server, "SessionLocal", sessions)
    monkeypatch.setattr(server, "MEMBER_ID", "alice")
    monkeypatch.setattr(server, "get_embedder", lambda: None)
    mine = await server.mcp.call_tool("get_receipt_status", {"receipt_id": 1})
    theirs = await server.mcp.call_tool("get_receipt_status", {"receipt_id": 3})
    assert '"rejected"' in mine.content[0].text
    assert "not found" in theirs.content[0].text

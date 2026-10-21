"""Provider adapter for tool calling. The loop (loop.py) only sees `Conversation` / `Turn` / `ToolCall`,
so swapping Gemini for another provider means writing one class here — the loop doesn't change.

Each provider has its own wire format for tools and tool results; the adapter keeps that history internally.
"""

from dataclasses import dataclass, field
from typing import Protocol

import httpx
from google import genai
from google.genai import types

from app.agent.tools import Tool
from app.llm.base import LLMError


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class Turn:
    """One model response: either a final answer (text) or tool calls to run — or both."""
    text: str | None
    calls: list[ToolCall] = field(default_factory=list)


class Conversation(Protocol):
    async def send(self, user_text: str) -> Turn: ...
    async def send_tool_results(self, results: list[tuple[ToolCall, dict]]) -> Turn: ...


class AgentLLM(Protocol):
    def start(self, system: str, tools: list[Tool]) -> Conversation: ...


# ---------------------------------------------------------------- Gemini

class GeminiConversation:
    def __init__(self, client: genai.Client, model: str, system: str, tools: list[Tool]):
        self._client, self._model = client, model
        self._history: list[types.Content] = []
        self._config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.json_schema)
                for t in tools
            ])],
            # We run the loop ourselves (auth, validation, limits, logging) — don't let the SDK auto-call.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def _generate(self) -> Turn:
        try:
            resp = await self._client.aio.models.generate_content(
                model=self._model, contents=self._history, config=self._config)
        except (genai.errors.APIError, httpx.HTTPError) as e:
            raise LLMError(f"gemini: {e}") from e
        if not resp.candidates or resp.candidates[0].content is None:
            raise LLMError("gemini: empty response")
        self._history.append(resp.candidates[0].content)  # the model's turn, incl. its tool calls
        calls = [ToolCall(fc.name, dict(fc.args or {})) for fc in (resp.function_calls or [])]
        text = None if calls else (resp.text or "")
        return Turn(text=text, calls=calls)

    async def send(self, user_text: str) -> Turn:
        self._history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
        return await self._generate()

    async def send_tool_results(self, results: list[tuple[ToolCall, dict]]) -> Turn:
        self._history.append(types.Content(role="user", parts=[
            types.Part.from_function_response(name=call.name, response=result) for call, result in results
        ]))
        return await self._generate()


class GeminiAgentLLM:
    def __init__(self, api_key: str, model: str):
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30_000))
        self.model = model

    def start(self, system: str, tools: list[Tool]) -> GeminiConversation:
        return GeminiConversation(self._client, self.model, system, tools)


# ---------------------------------------------------------------- fake (tests)

class ScriptedConversation:
    """Plays back a fixed list of turns and records what it was sent."""

    def __init__(self, turns: list[Turn]):
        self.turns, self.sent = list(turns), []

    async def send(self, user_text: str) -> Turn:
        self.sent.append(("user", user_text))
        return self.turns.pop(0)

    async def send_tool_results(self, results):
        self.sent.append(("tool_results", results))
        return self.turns.pop(0)


class ScriptedAgentLLM:
    def __init__(self, turns: list[Turn]):
        self.conversation = ScriptedConversation(turns)
        self.started_with: tuple | None = None

    def start(self, system: str, tools: list[Tool]) -> ScriptedConversation:
        self.started_with = (system, [t.name for t in tools])
        return self.conversation

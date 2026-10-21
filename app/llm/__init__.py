import logging
from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMClient, LLMError, T

log = logging.getLogger(__name__)


class FallbackLLM:
    """LLM gateway: try providers in order; on failure move to the next. Same interface as one client."""

    def __init__(self, clients: list[LLMClient]):
        if not clients:
            raise ValueError("at least one LLM provider is required")
        self.clients = clients
        self.name = "+".join(c.name for c in clients)

    async def generate_structured(self, system: str, prompt: str, schema: type[T]) -> T:
        errors = []
        for client in self.clients:
            try:
                return await client.generate_structured(system, prompt, schema)
            except LLMError as e:
                log.warning("LLM provider %s failed, trying next: %s", client.name, e)
                errors.append(str(e))
        raise LLMError("all providers failed: " + " | ".join(errors))


@lru_cache
def get_llm() -> LLMClient:
    """FastAPI dependency. Built once per process (clients hold connection pools)."""
    s = get_settings()
    clients: list[LLMClient] = []
    for name in (p.strip() for p in s.llm_providers.split(",") if p.strip()):
        if name == "gemini":
            from app.llm.gemini import GeminiClient

            clients.append(GeminiClient(s.gemini_api_key, s.gemini_model))
        elif name == "ollama":
            from app.llm.ollama import OllamaClient

            clients.append(OllamaClient(s.ollama_url, s.ollama_model))
        elif name == "fake":
            from app.llm.fake import FakeLLM

            clients.append(FakeLLM())
        else:
            raise ValueError(f"unknown LLM provider: {name}")
    return FallbackLLM(clients)


__all__ = ["FallbackLLM", "LLMClient", "LLMError", "get_llm"]

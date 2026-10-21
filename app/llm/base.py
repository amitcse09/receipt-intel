from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Any provider failure (timeout, rate limit, bad JSON). The gateway falls back on this."""


class LLMClient(Protocol):
    """The only interface the app depends on. Swap providers without touching business logic."""

    name: str

    async def generate_structured(self, system: str, prompt: str, schema: type[T]) -> T:
        """Return the model's answer parsed and validated into `schema` (a Pydantic model)."""
        ...

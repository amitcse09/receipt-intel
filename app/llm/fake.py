from app.llm.base import LLMError, T


class FakeLLM:
    """Deterministic stand-in for tests and offline dev. Returns canned answers, or fails on demand."""

    name = "fake"

    def __init__(self, response=None, fail: bool = False):
        self.response = response  # a Pydantic object to return
        self.fail = fail
        self.calls: list[str] = []

    async def generate_structured(self, system: str, prompt: str, schema: type[T]) -> T:
        self.calls.append(prompt)
        if self.fail or self.response is None:
            raise LLMError("fake: configured to fail")
        return schema.model_validate(self.response.model_dump())

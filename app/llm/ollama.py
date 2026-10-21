import httpx
from pydantic import ValidationError

from app.llm.base import LLMError, T


class OllamaClient:
    """Local model via Ollama's REST API. `format` = JSON schema gives structured output, same idea as Gemini."""

    name = "ollama"

    def __init__(self, url: str, model: str, timeout: float = 60):
        self._http = httpx.AsyncClient(base_url=url, timeout=timeout)
        self._model = model

    async def generate_structured(self, system: str, prompt: str, schema: type[T]) -> T:
        try:
            r = await self._http.post(
                "/api/chat",
                json={
                    "model": self._model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    "format": schema.model_json_schema(),
                    "stream": False,
                    "options": {"temperature": 0},
                },
            )
            r.raise_for_status()
            return schema.model_validate_json(r.json()["message"]["content"])
        except (httpx.HTTPError, KeyError, ValidationError) as e:
            raise LLMError(f"ollama: {e}") from e

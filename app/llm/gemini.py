import httpx
from google import genai
from google.genai import types
from pydantic import ValidationError

from app.llm.base import LLMError, T


class GeminiClient:
    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout_s: float = 30):
        # Always set a timeout: a hung LLM call would otherwise hold the worker forever.
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        self._model = model

    async def generate_structured(self, system: str, prompt: str, schema: type[T]) -> T:
        try:
            resp = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0,  # extraction should be deterministic, not creative
                    response_mime_type="application/json",
                    response_schema=schema,  # constrained decoding: model must emit this JSON shape
                ),
            )
            return schema.model_validate_json(resp.text)  # validate anyway — never trust model output
        # httpx.HTTPError covers network failures/timeouts — without it they'd bypass the fallback.
        except (genai.errors.APIError, httpx.HTTPError, ValidationError, ValueError) as e:
            raise LLMError(f"gemini: {e}") from e

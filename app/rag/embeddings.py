"""Embedding providers.

Unlike chat LLMs there is deliberately NO fallback here: vectors from different models live in different
spaces, so a query embedded by model B can't be compared with documents embedded by model A. Changing the
embedding model means re-embedding the whole corpus.
"""

import hashlib
import math
import re
from functools import lru_cache
from typing import Literal, Protocol

import httpx
from google import genai
from google.genai import types

from app.config import get_settings
from app.llm.base import LLMError

Kind = Literal["document", "query"]


class Embedder(Protocol):
    model: str
    dim: int

    async def embed(self, texts: list[str], kind: Kind) -> list[list[float]]: ...


class GeminiEmbedder:
    BATCH = 50

    def __init__(self, api_key: str, model: str, dim: int):
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=30_000))
        self.model, self.dim = model, dim

    async def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        # Asymmetric retrieval: documents and questions are embedded with different task types.
        task = "RETRIEVAL_DOCUMENT" if kind == "document" else "RETRIEVAL_QUERY"
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), self.BATCH):
                batch = texts[i : i + self.BATCH]
                resp = await self._client.aio.models.embed_content(
                    model=self.model,
                    contents=batch,
                    config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dim),
                )
                vectors = [e.values for e in resp.embeddings]
                # Guard: some models silently merge a list into one embedding. Never index misaligned vectors.
                if len(vectors) != len(batch) or any(len(v) != self.dim for v in vectors):
                    raise LLMError(f"embedding shape mismatch: sent {len(batch)}, got {len(vectors)}")
                out.extend(vectors)
        except (genai.errors.APIError, httpx.HTTPError) as e:
            raise LLMError(f"gemini embed: {e}") from e
        return out


class FakeEmbedder:
    """Hashing-trick bag of words: texts sharing words get similar vectors. Deterministic, offline, for tests."""

    model, dim = "fake-bow", 768

    async def embed(self, texts: list[str], kind: Kind) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim] += 1
        norm = math.sqrt(sum(x * x for x in v)) or 1
        return [x / norm for x in v]


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedding_provider == "fake":
        return FakeEmbedder()
    return GeminiEmbedder(s.gemini_api_key, s.embedding_model, s.embedding_dim)

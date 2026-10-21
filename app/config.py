from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Reads from env vars / .env — the FastAPI equivalent of Django settings.py."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./receipts.db"
    api_key: str = "dev-secret"

    # LLM gateway: comma-separated, tried in order (first = primary, rest = fallbacks).
    llm_providers: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"  # right-sized: ~1-2s vs ~35s for 3.8-flash on free tier
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # RAG embeddings: single provider on purpose (vectors from different models aren't comparable).
    embedding_provider: str = "gemini"  # gemini | fake
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768  # must match the Vector(...) column in app/rag/store.py

    # Async processing: "background" = FastAPI BackgroundTasks (in-process, lost on crash);
    # "kafka" = transactional outbox → relay → Kafka → validator workers (durable).
    validation_mode: str = "background"
    kafka_bootstrap: str = "localhost:9092"
    kafka_topic_prefix: str = ""  # tests use a unique prefix so they never touch real topics


@lru_cache  # build once, reuse — and easy to override in tests
def get_settings() -> Settings:
    return Settings()

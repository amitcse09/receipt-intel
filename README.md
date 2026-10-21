# Receipt Intelligence Service

An API that accepts receipts, extracts and validates them with an LLM (Gemini, with a local Ollama fallback), answers policy questions with RAG on pgvector, exposes its tools to agents over MCP, and processes validation through Kafka (transactional outbox, consumer groups, DLQ). Quality is measured with an LLM eval suite.

Built module by module as a portfolio project; each module has notes with design decisions and interview Q&A in `notes/`. Next: OpenTelemetry tracing, Terraform, GitHub Actions CI (see the status table below).

## Run
```bash
uv sync                               # installs Python 3.12 + deps
docker compose up -d                  # Postgres + pgvector (:5433) and Kafka (:9092)
uv run python -m scripts.ingest_policies   # embed policy docs (re-runnable)
uv run python -m scripts.seed_demo         # demo receipts for the assistant / MCP server
uv run fastapi dev app/main.py        # http://127.0.0.1:8000/docs  (header X-API-Key: dev-secret)
uv run pytest -q
uv run python -m scripts.try_llm       # live LLM check (needs GEMINI_API_KEY in .env)
uv run python -m scripts.try_rag       # live RAG questions
uv run python -m scripts.try_agent     # live tool-calling agent
uv run python -m scripts.try_mcp       # MCP server over stdio, like Claude Code
./scripts/dev_stack.sh                 # API in kafka mode + relay + 2 validators + notifier
uv run python -m evals.run extraction              # quality evals on real models (also: retrieval, rag)
uv run python -m evals.run rag --judge --repeat 2  # options: --repeat N, --judge, --k N, --model NAME
```

Settings live in `.env`: `LLM_PROVIDERS` (e.g. `gemini,ollama`), `GEMINI_API_KEY`, `GEMINI_MODEL`.

## Curriculum
| # | Module | Notes | Status |
|---|---|---|---|
| 1 | FastAPI | [notes/01-fastapi.md](notes/01-fastapi.md) | ✅ |
| 2 | GenAI engineering (LLM APIs, structured output, RAG, pgvector, evals, MCP) | [02-genai](notes/02-genai.md), [02b-rag](notes/02b-rag.md), [02c-evals](notes/02c-evals.md), [02d-tools-mcp](notes/02d-tools-mcp.md) | ✅ |
| 3 | Kafka (outbox, consumer groups, idempotency, DLQ) | [03-kafka](notes/03-kafka.md) | ✅ |
| 4 | OpenTelemetry & distributed tracing | — | next |
| 5 | Terraform (IaC) | — | |
| 6 | Lead extras: system design, SLOs, GitHub Actions | — | |
| 7 | Resume update | — | |

## Layout
```
app/
  main.py        app, lifespan, middleware
  config.py      settings from env
  db.py          async SQLAlchemy models + session
  deps.py        dependencies (DB session, auth, LLM)
  extraction.py  LLM extraction + deterministic validation rules
  llm/           provider-agnostic LLM gateway (gemini, ollama, fake) with fallback
  rag/           chunking, embeddings, pgvector hybrid search, grounded answers
  agent/         tools (one registry), tool-calling loop, cashback calculator
  mcp_server.py  the same tools over MCP (stdio) for Claude Code / any MCP client
  events/        topics, event schemas, transactional outbox + relay logic
  workers/       relay, validator (consumer group), notifier (second group)
  schemas.py     Pydantic models
  routers/       endpoints
data/policies/   RAG knowledge base (fictional cashback program)
tests/           API + unit tests (fake LLM, no network) + pgvector integration tests
scripts/         live checks against real providers
evals/           golden datasets, metrics, LLM judge, eval runner + saved results
notes/           concept summaries + interview Q&A per module
```

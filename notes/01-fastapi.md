# Module 1 — FastAPI (interview-focused)

Code for everything below lives in `app/`. Run `uv run pytest` and `uv run fastapi dev app/main.py`.

## Mental model in 30 seconds
- FastAPI = **Starlette** (ASGI, async HTTP) + **Pydantic** (validation from type hints).
- Type hints drive: request parsing, validation, response filtering, and OpenAPI docs (`/docs`).
- Served by an ASGI server (**uvicorn**), usually several worker processes (`uvicorn --workers 4` or gunicorn + uvicorn workers).

## DRF → FastAPI cheat sheet
| DRF | FastAPI | In this repo |
|---|---|---|
| Serializer | Pydantic `BaseModel` | `app/schemas.py` |
| ModelSerializer | `ConfigDict(from_attributes=True)` | `ReceiptOut` |
| ViewSet / router | `APIRouter` | `app/routers/receipts.py` |
| permission_classes | `Depends(...)` on route/router | `require_api_key` in `app/deps.py` |
| settings.py | `pydantic-settings` | `app/config.py` |
| Middleware | `@app.middleware("http")` | `timing_header` in `app/main.py` |
| AppConfig.ready / signals | `lifespan` context manager | `app/main.py` |
| Django ORM | SQLAlchemy 2.0 async | `app/db.py` |
| APIClient tests | `httpx.AsyncClient` + `dependency_overrides` | `tests/` |

---

## Interview questions (with answers)

### 1. `async def` vs `def` endpoints — what's the difference?
- `async def` runs **on the event loop**. Any blocking call (`requests.get`, `time.sleep`, psycopg2, heavy CPU) **blocks every request** in that worker.
- Plain `def` runs in a **thread pool** (default ~40 threads), so blocking code is OK there.
- Rule: use `async def` only if everything inside is awaitable (httpx, asyncpg, aiokafka). Otherwise use `def`, or wrap with `await run_in_threadpool(fn)` / `asyncio.to_thread(fn)`.
- **Follow-up: "Our p99 latency spikes under load, CPU is low."** → Classic sign of a blocking call inside `async def`.

### 2. Is FastAPI faster than Django?
For **I/O-bound** work with high concurrency, yes — one worker can wait on thousands of sockets. For **CPU-bound** work, no — the GIL still applies; scale with processes or offload to workers. Raw framework speed rarely matters; DB queries, N+1 problems and network calls dominate. (Your 5s → 600ms query work is the real lever.)

### 3. Explain dependency injection in FastAPI.
`Depends(fn)` makes FastAPI call `fn` per request and inject the result. Dependencies can depend on others (a DAG), are **cached per request** (same dependency used twice = called once), and `yield` dependencies give setup/teardown (DB session open → close). Benefits: reuse (auth, pagination, DB), and **testability** via `app.dependency_overrides` — see `tests/test_receipts.py`.

### 4. How do you handle auth?
`OAuth2PasswordBearer` / `HTTPBearer` security dependency → decode and verify JWT (signature, `exp`, `aud`, `iss`) → return current user → chain a `require_role("admin")` dependency. Apply at router level for a whole group. For an external IdP (Azure AD B2C — you've done this), validate against the IdP's JWKS keys, cached.

### 5. BackgroundTasks vs Celery vs Kafka?
| | BackgroundTasks | Celery | Kafka consumer |
|---|---|---|---|
| Runs | Same process, after response | Separate workers | Separate consumers |
| Survives crash/deploy | **No** — lost | Yes (broker) | Yes (log retained) |
| Retries | Manual | Built-in | Manual / retry topics |
| Use for | Emails, cache warm, cheap fire-and-forget | Jobs that must run, scheduled tasks | Event streams, many consumers, replay |
Key line: *"BackgroundTasks is not a queue — if the pod dies, the task is gone."*

### 6. How does Pydantic v2 differ from v1?
Core rewritten in Rust (**5–50× faster**). `.dict()` → `.model_dump()`, `parse_obj` → `model_validate`, `orm_mode` → `from_attributes`, `@validator` → `@field_validator` / `@model_validator`. Strict mode available.

### 7. Why `response_model`?
Validates and **filters** output — fields not in the model (password hashes, internal flags) never leak. Also documents the response in OpenAPI. Security answer, not just a typing answer.

### 8. How do you manage DB sessions with async SQLAlchemy?
One `AsyncSession` per request via a `yield` dependency; `async_sessionmaker(expire_on_commit=False)` so objects are usable after commit; never share a session across concurrent tasks (not thread/task-safe); configure pool size (`pool_size`, `max_overflow`) against Postgres `max_connections` × number of workers/pods — common production outage cause. Background work opens its **own** session (see `validate_receipt`).

### 9. What's `lifespan` for?
Startup/shutdown hooks: create DB pools, HTTP clients (one shared `httpx.AsyncClient`, not one per request), Kafka producers, load ML models. Replaces deprecated `@app.on_event`.

### 10. How do you deploy and scale FastAPI?
Docker image → uvicorn with N workers (≈ CPU cores) or 1 worker per container and scale pods horizontally in Kubernetes (preferred; let K8s do process management). Health/readiness endpoints, graceful shutdown, behind Nginx/ALB. Stateless app; state in Postgres/Redis.

### 11. How would you version an API?
URL prefix routers (`/v1`, `/v2` via `include_router(prefix=...)`) — simplest and most common. Keep old version until clients migrate; deprecate in OpenAPI (`deprecated=True`).

### 12. How do you handle errors consistently?
`HTTPException` for expected errors; `@app.exception_handler(MyDomainError)` to map domain exceptions → HTTP responses in one place; override `RequestValidationError` handler for a custom 422 shape.

### 13. Rate limiting / idempotency?
Rate limit at the gateway (Nginx, API Gateway, APIM — you've used these) or Redis token bucket via middleware/`slowapi`. Idempotency: client sends `Idempotency-Key` header; store key → response in Redis/DB with TTL; return stored response on repeat. You've done this pattern with Stripe webhooks — say so.

### 14. Testing strategy?
`httpx.AsyncClient(transport=ASGITransport(app))` for in-process tests; `dependency_overrides` to swap DB/auth/external clients; testcontainers for real Postgres/Kafka in integration tests; contract tests from the OpenAPI schema.

---

## "Tell me about a time" hook
> "When I evaluated FastAPI vs Django for a new async-heavy service, I chose FastAPI because ~80% of the request time was waiting on external APIs. With async I/O, one worker handled the concurrency that previously needed many sync workers…"
Only use this if it's true — otherwise frame it as this project.

## Exercises (15 min)
1. Add `PATCH /receipts/{id}` with a `ReceiptUpdate` model where all fields are optional (`model_dump(exclude_unset=True)`).
2. Add a `Pagination` dependency class returning `limit`/`offset` and reuse it.
3. Put `time.sleep(2)` inside `get_receipt`, hit it 5 times in parallel (`seq 5 | xargs -P5 -I{} curl ...`), and watch every request wait — then change it to `await asyncio.sleep(2)`. This is interview Q1, proven.

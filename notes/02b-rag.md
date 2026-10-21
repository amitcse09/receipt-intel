# Module 2b — RAG: what, why, when, and how our code does it

**How to read this file**
- **Part 1** explains the idea, with no code: what RAG is, why it exists, when to use it, and how it compares with the alternatives.
- **Part 2** walks through our code file by file, in reading order. Every step links to the line that does it, like [00-end-to-end-flow.md](00-end-to-end-flow.md).
- **Parts 3–6** cover design decisions, the real bugs we hit, interview Q&A and a self-check.

To see every stage live for any question:
```bash
uv run python -m scripts.explain_rag "Can I get cashback on beer?"
```
All the numbers in this file come from that script, run against our real index.

---

# Part 1 — The idea

## 1.1 The problem: what an LLM *can't* do on its own

Ask Gemini "Can I get cashback on beer under PaisaBack Rewards?" with no context, and you hit four walls:

| Limitation | What it means here |
|---|---|
| **It doesn't know your private data** | PaisaBack's rules were never in its training data. It has never seen them. |
| **Its knowledge is frozen** | Even public facts stop at the training cutoff. If the policy changes tomorrow, the model won't know. |
| **It guesses confidently** | Instead of saying "I don't know", it will often invent a plausible rule ("alcohol usually earns 1%…"). This is *hallucination*. |
| **It can't show its source** | Even when it's right, you can't audit *why*. That matters for support, finance and legal answers. |

## 1.2 What RAG is: an open-book exam

**RAG = Retrieval-Augmented Generation.** Before asking the model, you **look up** the relevant pages yourself and hand them over with the question:

> "Here are 4 paragraphs from our policy. Using **only these**, answer: can I get cashback on beer? Cite which paragraph you used. If they don't say, answer 'I don't know'."

| Step | Name | In plain words | Our code |
|---|---|---|---|
| 1 | **Retrieve** | Find the few paragraphs most relevant to the question | [store.py `hybrid_search`](../app/rag/store.py#L102) |
| 2 | **Augment** | Put those paragraphs into the prompt, next to the question | [answer.py `build_prompt`](../app/rag/answer.py#L36) |
| 3 | **Generate** | The LLM writes the answer from those paragraphs, with citations | [answer.py `answer_question`](../app/rag/answer.py#L58) |

The model goes from a *closed-book* exam (memory only, so it guesses) to an *open-book* exam (it reads the right page, then answers). All the hard engineering is in step 1: **finding the right page**.

## 1.3 How "finding the right page" works: embeddings

A keyword search for "beer" finds nothing, because our documents never use that word. They say "alcohol". So how did RAG find the rule?

An **embedding model** turns any text into a list of numbers (a *vector*, 768 numbers in our case) so that **texts with similar meaning get similar numbers**. Think of each text as a point on a map where "beer", "alcohol", "wine" and "liquor" all sit in the same neighbourhood.

Real output from our index for *"Can I get cashback on beer?"*:
```
1) EMBED  gemini-embedding-001: 768 numbers, first 5 = [0.01, -0.007, 0.008, -0.063, -0.009]

2) VECTOR SEARCH  cosine similarity to all chunks (1.0 = same meaning):
   v1  0.742  eligibility > Excluded categories        ← mentions "alcohol": closest in meaning
   v2  0.677  eligibility > Eligible purchases
   v3  0.670  disputes-and-fraud > Returns and refunds
   ...
   v15 0.584  disputes-and-fraud > Fraud               ← least related
```
**Cosine similarity** measures how closely two vectors point in the same direction (1.0 = identical meaning). The question never says "alcohol", yet the "Excluded categories" chunk comes out on top. That's **semantic search**.

Two things to notice, because they come up in interviews:
- **All the scores are close together (0.58–0.74).** Every chunk is "a bit similar", since they're all about cashback. There's no natural cut-off between relevant and irrelevant, so vector search **always returns something**, even for a question the documents can't answer. That's why the "I don't know" logic in step 3 is essential.
- **Vector search alone misses exact tokens** like `PB-APPEAL-7` or SKU codes. Embeddings capture *meaning* and blur exact strings. So we also run a classic **keyword search** and merge the two lists. This is *hybrid search*, covered in Part 2.

## 1.4 When to use RAG, and when not to

**Use RAG when all of these are true:**
1. The answer depends on **your own knowledge**: policies, docs, tickets, contracts, product catalogue, past incidents.
2. That knowledge is **too big to put in every prompt**, or **changes often**.
3. You need answers that are **traceable to a source** (support, compliance, internal help desks).

**Don't use RAG when:**
| Situation | Use instead |
|---|---|
| The knowledge is small and stable (a few pages) | Put it straight in the system prompt. **Our whole policy set is 827 tokens**, so honestly, stuffing it in the prompt would work fine here. We use RAG to learn the pattern, which becomes necessary at hundreds or thousands of documents. |
| The answer is in a **database**, not text ("how much cashback did I earn this month?") | **Tool calling**: the LLM calls an API or SQL query that returns exact numbers (Lesson 2d). RAG over table rows is a classic mistake. |
| You need **exact calculations or decisions** | Plain code (like `decide()` in 2a). The LLM can read and explain, but code should compute. |
| You want the model to change **style or format** (tone, a JSON shape, domain jargon) | Prompting first; **fine-tuning** if prompting isn't enough. |
| The task is just transforming the text you already have (extract, summarise, translate) | A direct LLM call. Our receipt extraction in 2a is **not** RAG: the receipt is already in the prompt, so there's nothing to retrieve. |

## 1.5 RAG vs the alternatives

| Approach | What it is | Strong at | Weak at | In this project |
|---|---|---|---|---|
| **Plain prompt** | Just ask the model | General knowledge, writing, reasoning | Your private data; hallucinates rules | ❌ would invent PaisaBack rules |
| **Prompt stuffing / long context** | Paste *all* the documents into every prompt | Small corpora; simple; no retrieval errors | Cost and latency grow with every doc, on every request; quality drops in very long contexts ("lost in the middle"); hard to filter by user permission | ✅ would work for our 827 tokens; ❌ at 5,000 policy PDFs |
| **RAG** | Retrieve the relevant pieces, then answer from them | Large or changing knowledge; citations; per-user access control; cheap per request | Only as good as retrieval: if the right chunk isn't found, the answer is wrong or "don't know"; needs an ingestion pipeline | ✅ `/policies/ask` |
| **Fine-tuning** | Further-train the model on your examples | Style, format, narrow skills, shorter prompts | **Not a reliable way to add facts**: no citations, slow and expensive to update, can still hallucinate | ❌ policy changes would need retraining |
| **Classic search** (Elasticsearch / Postgres full-text) | Return matching documents; no LLM | Exact terms, codes, filters; fast, cheap, predictable | No synonyms ("beer" ≠ "alcohol"); returns documents, not answers | ✅ used *inside* our RAG as the keyword half |
| **Tool / function calling** | The LLM calls your API or SQL, and the code returns exact data | Live, structured, per-user data ("my balance", "status of claim 42") | Only what the APIs expose; needs safe tool design | Lesson 2d |
| **Agentic RAG** | The LLM decides *when and what* to search, possibly several times | Multi-step questions ("compare Gold vs Platinum for my last 3 receipts") | Slower, costlier, harder to test | Lesson 2d |
| **GraphRAG / knowledge graph** | Retrieve over entities and relationships, not just text chunks | "How are X and Y related?" across many documents | Expensive to build and maintain | Not needed here |

**One-sentence version for interviews:** *"Use RAG to give the model knowledge, fine-tuning to give it behaviour, tools to give it live data, and code to make decisions."*

## 1.6 Decision guide

```
Does the answer need knowledge the model doesn't have?
 ├─ No ─────────────────────────────────────────────► plain prompt
 └─ Yes: where does that knowledge live?
      ├─ In a database / API (numbers, per-user state) ─► tool calling
      └─ In text (docs, policies, tickets)
           ├─ Small and stable (fits easily in the prompt) ─► prompt stuffing
           └─ Large, changing, needs citations or access control ─► RAG
                └─ Questions need several lookups or reasoning steps ─► agentic RAG
And separately: wrong tone or format, even with good prompts? ─► fine-tuning
```

---

# Part 2 — Code walkthrough (read the files in this order)

RAG has **two separate flows**, and they run at different times:
- **Ingest (offline):** turn documents into searchable chunks. Run it whenever the documents change.
- **Query (per request):** answer a user's question using those chunks.

```
INGEST  scripts/ingest_policies.py                         QUERY  POST /policies/ask
  data/policies/*.md                                          "Can I get cashback on beer?"
     │ chunk_markdown()        Part 2.2                           │ embed(kind="query")       2.6
     ▼                                                            ▼
  15 chunks ─ embed(kind="document") 2.3 ─► policy_chunks ◄── vector_search  (meaning)      2.6
            skip unchanged (hash)    2.5    (pgvector)    ◄── keyword_search (exact words)  2.6
                                                              │ rrf() merge → top 4          2.6
                                                              ▼
                                                 build_prompt → Gemini → finalize()          2.7
```

## 2.1 The knowledge base: [data/policies/](../data/policies/)

Start by reading the 4 source files. Everything else exists to answer questions about them:
- [eligibility.md](../data/policies/eligibility.md): what earns cashback, the excluded categories (alcohol…), the ₹200 minimum
- [claims.md](../data/policies/claims.md): the 30-day window (45 for Diwali), weekly limits
- [cashback-rates.md](../data/policies/cashback-rates.md): tier rates, partner bonus, caps
- [disputes-and-fraud.md](../data/policies/disputes-and-fraud.md): appeals (`PB-APPEAL-7`), returns, fraud

Note the structure: one `#` title, then `##` sections, **one rule per section**. Good RAG starts with well-structured source documents.

## 2.2 Chunking: [app/rag/chunking.py](../app/rag/chunking.py)

**Why chunk at all?** Embedding a whole document gives one blurry "average" vector, and the LLM would receive far more text than it needs. We want small pieces that each answer one kind of question.

| Lines | What happens | Why |
|---|---|---|
| [12](../app/rag/chunking.py#L12) | `MAX_CHARS = 1200` (~300 tokens) | Big enough to hold a whole rule, small enough to stay precise |
| [15-23](../app/rag/chunking.py#L15) | `Chunk` dataclass + `content_hash` (sha256 of the text) | The hash is the chunk's identity: same text gives the same hash, so it can be skipped on re-ingest (2.5) |
| [29-37](../app/rag/chunking.py#L29) | Walk the lines: `#` sets the title, `##` starts a new section, other lines go into the current section | **Structure-aware**: we split where the author split, not at random character counts |
| [41](../app/rag/chunking.py#L41) | `header = "PaisaBack Rewards — Purchase Eligibility > Excluded categories"` | **Contextual header**: once cut out, a chunk still says what it's about. Without it, "The following items never earn cashback…" loses which program and which topic |
| [43-44](../app/rag/chunking.py#L43) | `_split()` only if a section is too long | Most of our sections are short, so one section becomes one chunk |
| [48-61](../app/rag/chunking.py#L48) | Greedy sentence packing | Never cuts in the middle of a sentence. A half-sentence rule is a wrong rule |

Result: 4 files → **15 chunks**. Test: [tests/test_rag.py:21](../tests/test_rag.py#L21).

## 2.3 Embeddings: [app/rag/embeddings.py](../app/rag/embeddings.py)

| Lines | What happens | Why |
|---|---|---|
| [24-28](../app/rag/embeddings.py#L24) | `Embedder` Protocol: `embed(texts, kind) -> vectors` | Same idea as `LLMClient` in 2a: the code depends on an interface, so tests can plug in a fake |
| [3-5](../app/rag/embeddings.py#L3) | Docstring: **no fallback provider** | Vectors from model A and model B live in different "maps". You can't search model-A documents with a model-B question |
| [40](../app/rag/embeddings.py#L40) | `RETRIEVAL_DOCUMENT` vs `RETRIEVAL_QUERY` | **Asymmetric** embedding: a short question doesn't look like the paragraph that answers it, so the model encodes each one appropriately |
| [43-49](../app/rag/embeddings.py#L43) | Batches of 50, `output_dimensionality=768` | Fewer API calls; 768 dims is 4× smaller than the model's full 3072, with slightly lower quality |
| [51-53](../app/rag/embeddings.py#L51) | Check that we got one vector per input, of the right size | `gemini-embedding-2` returned **1 vector for 2 texts**. Without this check we'd store the wrong vector against the wrong chunk, with no error |
| [60-73](../app/rag/embeddings.py#L60) | `FakeEmbedder`: hashed bag of words | Tests run offline and for free. It matches shared *words*, not meaning, which is good enough to test the plumbing |
| [76-81](../app/rag/embeddings.py#L76) | `get_embedder()`, cached | Settings: [app/config.py:21-24](../app/config.py#L21) |

## 2.4 The table and indexes: [app/rag/store.py:17-38](../app/rag/store.py#L17)

```
policy_chunks
 id | doc         | section             | content (header + text) | content_hash | embedding_model      | embedding (vector 768)
 7  | eligibility | Excluded categories | PaisaBack Rewards — …   | 3f9a…        | gemini-embedding-001 | [0.01, -0.007, …]
```

| Lines | What | Why |
|---|---|---|
| [17-18](../app/rag/store.py#L17) | Separate `RagBase` metadata | These tables need Postgres + pgvector. Receipts tests run on SQLite and must not try to create them |
| [28](../app/rag/store.py#L28) | `content_hash` UNIQUE | Same chunk text can't be stored twice |
| [29](../app/rag/store.py#L29) | `embedding_model` on **every row** | Provenance: which "map" this vector belongs to. It saved us in bug #1 (Part 4) |
| [30](../app/rag/store.py#L30) | `Vector(768)` column | pgvector's type. Must match `embedding_dim` in config |
| [34-35](../app/rag/store.py#L34) | **HNSW** index with `vector_cosine_ops` | Approximate nearest-neighbour search: fast without comparing against every row. The ops class must match the operator we query with (cosine), or the index isn't used |
| [37](../app/rag/store.py#L37) | **GIN** index on `to_tsvector('english', content)` | Makes the keyword search fast |

Tables are created at app startup by [app/main.py:15-21](../app/main.py#L15) (`init_rag_schema`), only when the database is Postgres.

## 2.5 Ingest: [scripts/ingest_policies.py](../scripts/ingest_policies.py) → [store.py `ingest`](../app/rag/store.py#L52)

Run with `uv run python -m scripts.ingest_policies`.

| Step | Where | What happens |
|---|---|---|
| 1 | [ingest_policies.py:21](../scripts/ingest_policies.py#L21) | Make sure the extension and tables exist |
| 2 | [ingest_policies.py:27](../scripts/ingest_policies.py#L27) | **Delete chunks whose source file no longer exists**, or the bot keeps quoting a retired policy |
| 3 | [ingest_policies.py:32](../scripts/ingest_policies.py#L32) | Chunk each file (2.2) |
| 4 | [store.py:54-57](../app/rag/store.py#L54) | Load existing `hash → model` for this doc; build the `wanted` hashes from the new chunks |
| 5 | [store.py:59-60](../app/rag/store.py#L59) | **Diff**: `to_add` = new/changed text *or* embedded by a different model; `stale` = in the DB but no longer wanted |
| 6 | [store.py:62-63](../app/rag/store.py#L62) | Delete stale rows |
| 7 | [store.py:64-70](../app/rag/store.py#L64) | Embed **only** `to_add` (this is what costs money), then insert. `zip(strict=True)` raises if the counts differ |
| 8 | [store.py:72](../app/rag/store.py#L72) | Return stats: `{'added': 0, 'deleted': 0, 'unchanged': 4}` on a re-run means nothing was re-embedded |

This is **idempotent**: running it 10 times gives the same result as running it once, and costs nothing after the first run.

## 2.6 Retrieval: [store.py `hybrid_search`](../app/rag/store.py#L102)

Entry point from the API: [app/routers/policies.py:31](../app/routers/policies.py#L31).

| Step | Where | What happens (real numbers for "Can I get cashback on beer?") |
|---|---|---|
| 1 | [store.py:103](../app/rag/store.py#L103) | Embed the **question** as a *query* → 768 numbers |
| 2 | [store.py:104](../app/rag/store.py#L104) → [75-78](../app/rag/store.py#L75) | **Vector search**: `ORDER BY embedding <=> question_vector LIMIT 10`. `<=>` is cosine distance. Top: *Excluded categories* (0.742) |
| 3 | [store.py:105](../app/rag/store.py#L105) → [81-89](../app/rag/store.py#L81) | **Keyword search**: Postgres turns the question into `'get' \| 'cashback' \| 'beer'`. Stop words ("can", "I", "on") are dropped and words are stemmed. We swap AND for OR ([line 84](../app/rag/store.py#L84)), because requiring *every* word of a question is too strict. Ranked by `ts_rank_cd` |
| 4 | [store.py:106](../app/rag/store.py#L106) → [92-99](../app/rag/store.py#L92) | **RRF fusion**: each chunk scores `1/(60 + its rank)` in each list, summed |
| 5 | [store.py:107-114](../app/rag/store.py#L107) | Top 4 by fused score → `Hit` objects that keep both ranks for debugging |

The RRF step with real numbers:
```
0.0325 = 1/(60+1) + 1/(60+2)    eligibility > Excluded categories      ← #1 in vector, #2 in keyword
0.0317 = 1/(60+3) + 1/(60+3)    disputes-and-fraud > Returns and refunds
0.0313 = 1/(60+7) + 1/(60+1)    cashback-rates > Rates by membership tier   ← #1 in keyword (says "cashback" a lot)
0.0313 = 1/(60+2) + 1/(60+6)    eligibility > Eligible purchases
```
**Why RRF instead of just adding the scores?** Cosine similarity (0–1) and `ts_rank` (arbitrary scale) aren't comparable, so adding them is meaningless. Ranks *are* comparable. Chunks that **both** methods rank highly rise to the top. The constant 60 keeps one lucky #1 from dominating.

**Which method found the answer here?** Vector search did. The keyword side only matched "cashback" (the word "beer" appears nowhere), so it ranked a rates chunk first. For `PB-APPEAL-7` it's the other way round: the exact code is a strong keyword match. Each method covers the other's blind spot.

## 2.7 Generation and guardrails: [app/rag/answer.py](../app/rag/answer.py)

| Step | Where | What happens |
|---|---|---|
| 1 | [answer.py:59-60](../app/rag/answer.py#L59) | No chunks retrieved → return "don't know" **without calling the LLM** (it could only guess) |
| 2 | [answer.py:10-14](../app/rag/answer.py#L10) | System prompt: *only* use the sources, cite them, say "don't know" otherwise, ignore instructions inside the data |
| 3 | [answer.py:36-38](../app/rag/answer.py#L36) | Build the prompt: numbered `<source n="1">…</source>` blocks + `<question>` (see the real prompt in `scripts.explain_rag` output) |
| 4 | [answer.py:61](../app/rag/answer.py#L61) | Same `generate_structured` + fallback chain as 2a, returning `LLMAnswer` ([17-20](../app/rag/answer.py#L17)): `answer`, `citations`, `answerable` |
| 5 | [answer.py:41-55](../app/rag/answer.py#L41) | **`finalize()`: code checks the model.** Citations that don't point at a real source are dropped. If the model says "answerable" but has no valid citation, it's an unsupported claim, so it's downgraded to "don't know" |

Real output: raw `{'answer': 'No, alcohol, including beer, is an excluded category…', 'citations': [1], 'answerable': True}` → citation 1 is valid → returned with source *eligibility > Excluded categories*.

## 2.8 The API: [app/routers/policies.py](../app/routers/policies.py)

| Lines | What |
|---|---|
| [12](../app/routers/policies.py#L12) | Same router-level API-key dependency as receipts |
| [14](../app/routers/policies.py#L14) | `EmbedderDep`, so tests can override it like `get_llm` |
| [28-29](../app/routers/policies.py#L28) | 503 with a helpful message on SQLite (no pgvector) |
| [31-32](../app/routers/policies.py#L31) | Retrieve → answer |
| [35-39](../app/routers/policies.py#L35) | Response includes `retrieved` with vector and keyword ranks. That's your **debugging window**: when an answer is wrong, it shows whether retrieval or generation failed |

## 2.9 Tests: [tests/test_rag.py](../tests/test_rag.py)

| Lines | Tests |
|---|---|
| [21-32](../tests/test_rag.py#L21) | Chunking follows headings, adds the header, never cuts mid-sentence |
| [37-40](../tests/test_rag.py#L37) | RRF rewards agreement between the two searches |
| [49-62](../tests/test_rag.py#L49) | Guardrails: fake citations dropped; uncited "answers" downgraded; no hits means no LLM call |
| [70-89](../tests/test_rag.py#L70) | Fixture: a real Postgres in an **isolated schema** (skipped when Docker is off). Read the comment, because it's bug #1 |
| [92-108](../tests/test_rag.py#L92) | Integration: ingest is idempotent, the exact code `PB-APPEAL-7` is found by keyword, an edited doc replaces stale chunks |

---

# Part 3 — Design decisions (and the alternatives we didn't pick)

| Decision | Chose | Alternative | Why |
|---|---|---|---|
| Vector store | **pgvector** in our existing Postgres | Pinecone, Qdrant, Weaviate | One database: transactions, SQL filters, joins with business data, existing backups. Fine up to millions of vectors |
| Index | **HNSW** | IVFFlat, none | Good recall and latency; works on an empty table. IVFFlat must be built after the data is loaded |
| Retrieval | **Hybrid** (vector + keyword, RRF) | Vector only | Vectors miss exact codes; keywords miss synonyms |
| Chunking | **By heading**, sentence-packed, with a header | Fixed 500-char windows with overlap | Our docs are structured, so a rule stays intact |
| Ingestion | **Hash-based diff** + model provenance | Wipe and re-embed everything | No cost when nothing changed; safe model upgrades |
| Answer trust | **Code-verified citations** | Trust the model | Same principle as 2a: the LLM reads, code decides |
| Embedding fallback | **None** | A second provider | Different vector spaces; degrade to keyword-only instead |

---

# Part 4 — Two real bugs we hit (great interview stories)

**1. A test silently corrupted the production index.**
The test isolated itself with `search_path=rag_test,public`. SQLAlchemy's "does the table exist?" check found the **real** `public.policy_chunks` through that path, so it skipped creating the test table. The test then ran `ingest()` with the fake embedder against the real table. Seeing a different model name, the ingest logic replaced 3 real Gemini vectors with fake ones. The symptom appeared later and looked like poor retrieval: a returns question answered "I don't know".
- **Fix:** `schema_translate_map` ([tests/test_rag.py:72](../tests/test_rag.py#L72)) forces every statement into `rag_test`. Verified by counting rows in the real table before and after test runs.
- **Recovery was automatic** because of the `embedding_model` column: re-ingest found the 3 rows from the wrong model and re-embedded them.
- **Lessons:** enforce test isolation, don't assume it. Store provenance with every vector. A RAG quality drop can be a *data* bug, not a prompt bug.

**2. An embedding model silently merged inputs.** `gemini-embedding-2` returned **1 vector for 2 texts**. Without the check at [embeddings.py:51-53](../app/rag/embeddings.py#L51), we'd have stored wrong vectors without any error.

---

# Part 5 — Interview Q&A

### 1. What is RAG, and why would you use it?
An LLM doesn't know your private or recent data, and when it doesn't know, it guesses. RAG retrieves the relevant passages from your own knowledge base at question time and makes the model answer only from them, with citations. You get answers grounded in **your** data, updated as soon as documents change, and auditable.

### 2. RAG vs fine-tuning?
RAG adds **knowledge**; fine-tuning changes **behaviour** (style, format, narrow skills). Fine-tuning is a poor way to add facts: it's expensive, slow to update, can't cite, and still hallucinates. Policies change every month; with RAG that means re-ingesting, with fine-tuning it means retraining. They're often combined: a fine-tuned model for tone, RAG for facts.

### 3. Models now have 1M-token context windows. Why not paste all the documents in?
For a small corpus, do exactly that. Ours is 827 tokens, and stuffing would work. At scale, RAG wins: cost and latency grow with prompt size **on every request**; quality drops in very long contexts ("lost in the middle"); and you can't easily enforce per-user document permissions on one giant prompt. RAG sends ~4 relevant chunks instead of thousands of pages. Prompt caching narrows the cost gap for a stable corpus, so it's a real trade-off, not a rule.

### 4. RAG vs tool calling?
RAG is for **unstructured text** (policies, docs). Tool calling is for **structured or live data** (balances, order status, anything in a database), where the model calls an API or SQL query and gets exact values. Asking RAG "how much cashback did I earn in March?" is wrong; that's a SQL query exposed as a tool.

### 5. Walk me through your RAG pipeline.
Ingest: chunk by heading with a context header → embed as documents → store in pgvector with content hash and model name → HNSW + GIN indexes, with only changed chunks re-embedded. Query: embed the question → vector top-10 + keyword top-10 → RRF → top-4 → numbered sources in the prompt → structured answer → citations verified in code, so an uncited answer becomes "I don't know".

### 6. How do embeddings and vector search work?
An embedding model maps text to a vector so that similar meaning gives nearby vectors. We store document vectors; at query time we embed the question and find the closest vectors by cosine similarity. An ANN index (HNSW) avoids comparing against every row. Example: "beer" retrieves the "alcohol is excluded" rule (similarity 0.742) with no shared word.

### 7. Why hybrid search? What's RRF?
Vector search handles meaning but blurs exact tokens (`PB-APPEAL-7`, SKUs, error codes). Keyword search is the reverse. RRF merges the two ranked lists by rank, `Σ 1/(60 + rank)`, because their raw scores aren't comparable. Items both methods rank highly win.

### 8. How do you choose chunk size?
Follow the document's structure (headings, Q&A pairs, functions), cap the size (~200–500 tokens), split on sentence boundaries, and add a context header to each chunk. Then **measure** retrieval hit-rate on real questions at different sizes (Lesson 2c) instead of guessing.

### 9. The bot gave a wrong answer. How do you debug it?
First decide: **retrieval or generation?** Was the right chunk in `retrieved`? If not, look at chunking, k, the embedding model, the keyword query, **and the data** (our corrupted vectors looked exactly like a retrieval-quality problem). If it was retrieved but the answer is wrong, look at the prompt, conflicting sources, the model, and the citation checks. Then add the case to the eval set.

### 10. How do you stop RAG from hallucinating?
Tell the model to use the sources only and that "don't know" is allowed; require citations and **verify them in code**; skip the LLM when nothing is retrieved; use temperature 0. Vector search always returns *something* (our scores were all between 0.58 and 0.74), so abstaining is essential. Optionally add a reranker or relevance threshold.

### 11. How do you keep the index in sync with changing documents?
Hash each chunk and re-embed only what changed; delete chunks from removed documents; store the embedding model per row, because changing models means re-embedding everything. Trigger ingestion from CI on doc changes, or from change events (Kafka, Module 3).

### 12. pgvector or a dedicated vector database?
Start with pgvector: one system, SQL filters and joins, transactions, existing ops. It handles millions of vectors. Move to a dedicated store at billions of vectors, very high QPS, or when you need built-in sharding. Avoiding an extra moving part is a Lead-level answer.

### 13. Multi-tenant documents?
Store `tenant_id` or ACL columns on each chunk and filter **inside the same SQL query** as the vector search. Never filter after retrieval: the top-k could all belong to someone else, giving empty results or a data leak.

### 14. What would you add next?
A **reranker** (rescores the top 20 → top 4 for precision), **query rewriting** for follow-up questions ("and for Gold?"), parent-child chunks, metadata filters (effective date), and **evals** to measure all of it (Lesson 2c).

---

# Part 6 — Self-check (answer before opening)

<details><summary>1. Our receipt extraction in Lesson 2a uses an LLM. Is it RAG? Why or why not?</summary>
No. The receipt text is already in the prompt; nothing is retrieved from a knowledge base. It's direct structured extraction. RAG is when you have to <em>find</em> relevant knowledge first.
</details>

<details><summary>2. "How much cashback did I earn last month?" — RAG, tool calling, or fine-tuning?</summary>
Tool calling. It's per-user, structured, live data in a database. The model should call an API or SQL query that returns the exact number. RAG over text can't answer it, and fine-tuning definitely can't.
</details>

<details><summary>3. Why did "beer" find the alcohol rule, even though keyword search ranked a different chunk first?</summary>
Vector search ranked "Excluded categories" #1 (0.742) because the embeddings of "beer" and "alcohol" are close in meaning. Keyword search matched only the word "cashback" and ranked it #2. RRF put it first overall because it was near the top of <em>both</em> lists: 1/61 + 1/62.
</details>

<details><summary>4. A user asks about "travel insurance" (not in the docs). Does vector search return zero results?</summary>
No. It always returns the k nearest chunks, even if they're only loosely related (all our similarities were 0.58–0.74). The protection comes later: the system prompt allows "don't know", and <code>finalize()</code> downgrades any answer without valid citations.
</details>

<details><summary>5. We switch from gemini-embedding-001 to a new embedding model. What must happen, and what does our code do automatically?</summary>
Every chunk must be re-embedded, because the old and new vectors are in different spaces. Our ingest compares each row's <code>embedding_model</code> with the current model, treats mismatches as stale, and re-embeds them on the next run.
</details>

<details><summary>6. Why is there no fallback embedding provider, when the chat LLM has one?</summary>
A query embedded with model B can't be compared with documents embedded with model A. For chat, any model can read the same text prompt; for vectors, the "map" must match. During an outage you'd fall back to keyword-only search instead.
</details>

<details><summary>7. Our corpus is 827 tokens. Defend or attack using RAG for it.</summary>
Attack: it fits easily in every prompt, so stuffing would be simpler, with no retrieval failures, and the cost is negligible. Defend: we're building the pattern for when the corpus grows to thousands of documents, and RAG gives citations and per-document access control. A strong answer names the trade-off and the point at which you'd switch.
</details>

<details><summary>8. A question needs two chunks (Gold rate + partner bonus). What setting could break it, and how would you notice?</summary>
<code>k</code> too small. The partner-bonus chunk ranked 3rd, so with k=2 the answer would say 2% instead of 4%. You'd notice from <code>retrieved</code> in the API response, and you'd catch it systematically with an eval set (Lesson 2c).
</details>

---

## Exercises (20–30 min)
1. Run `uv run python -m scripts.explain_rag "What is form PB-APPEAL-7 used for?"` and compare it with the beer output. Which search method did the work this time?
2. In [scripts/try_rag.py](../scripts/try_rag.py), change `k=4` to `k=2`. Which question breaks, and why?
3. Make [`keyword_search`](../app/rag/store.py#L81) return `[]`, ask about `PB-APPEAL-7`; then restore it, make [`vector_search`](../app/rag/store.py#L75) return `[]`, and ask about beer. That shows why hybrid matters.
4. Add `data/policies/referrals.md` ("# PaisaBack Rewards — Referrals" / "## Referral bonus" / "Earn ₹50 when a friend's first claim is validated."). Run ingest and check that only the new doc was embedded. Ask about it. Then delete the file, re-ingest, and confirm the bot says it doesn't know.
5. Answer self-check Q7 out loud in under a minute. That's a real interview question.

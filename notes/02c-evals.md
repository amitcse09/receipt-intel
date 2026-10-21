# Module 2c — Evals: measuring whether an LLM feature is actually good

**How to read this file:** Part 1 is the idea (no code). Part 2 walks through the code with clickable links. Part 3 is the real story of what our evals found. Parts 4–5 are interview Q&A and a self-check.

Run them yourself (options are typed as-is, no square brackets: zsh treats `[...]` as a file pattern):
```bash
docker compose up -d                                   # RAG suites need pgvector
uv run python -m evals.run retrieval                   # ~2s, embeddings only: vector vs keyword vs hybrid
uv run python -m evals.run extraction                 # 13 receipts → field accuracy + decisions
uv run python -m evals.run extraction --repeat 3      # same, 3 times → flaky cases
uv run python -m evals.run rag --judge                # 19 questions → facts, abstention, citations, faithfulness
uv run python -m evals.run rag --k 2                  # change retrieval depth
uv run python -m evals.run extraction --model gemini-3.1-flash-lite   # another model for this run
```

---

# Part 1 — The idea

## 1.1 Why normal tests aren't enough

The tests in `tests/` check **plumbing**: "if the LLM returns X, does our code do Y?" They use a `FakeLLM` precisely so they're fast and deterministic. They can't tell you whether the **real model**, with **our prompt**, on **realistic inputs**, gives good answers.

| | Unit tests (`tests/`) | Evals (`evals/`) |
|---|---|---|
| Question answered | Does the code work? | Is the AI output good? |
| Calls the real model? | No (fake) | **Yes** |
| Result | pass / fail | a **score** (0.0–1.0) per metric |
| Deterministic? | Yes | **No**: the same run can differ (we measured this) |
| Speed and cost | ms, free | minutes, uses API quota |
| When to run | Every commit | Every **prompt / model / chunking / retrieval** change, plus nightly |

An LLM feature without evals is like a database query with no `EXPLAIN`: you're guessing.

## 1.2 What an eval is

Three ingredients:
1. **A golden dataset**: inputs with **known correct answers**, labelled by a human. *This is the most valuable part.* Our [extraction.jsonl](../evals/datasets/extraction.jsonl) (13 receipts) and [rag.jsonl](../evals/datasets/rag.jsonl) (19 questions).
2. **Metrics**: functions that compare output with the expected answer and return a number ([evals/metrics.py](../evals/metrics.py)).
3. **A runner**: runs the real system over the dataset, then reports, saves, compares with the previous run, and gates ([evals/run.py](../evals/run.py)).

## 1.3 The three kinds of grading

| Grader | How | Strengths | Weaknesses | Ours |
|---|---|---|---|---|
| **Deterministic (code)** | Exact or fuzzy match, "contains 45", money within ₹0.01, valid JSON | Free, fast, repeatable, no bias | Can't judge meaning; can be too strict ("2% + 2%" ≠ "4%") | Field accuracy, decision accuracy, `must_include`, hit@k |
| **LLM-as-judge** | A second LLM grades against a rubric | Judges meaning: faithfulness, relevance, tone | Costs calls; **can be wrong**; biased toward long answers and its own style | `--judge` faithfulness |
| **Human** | People label or review outputs | The ground truth | Slow, expensive, inconsistent between reviewers | You wrote the golden data; you spot-check the judge |

**Rule of thumb:** use deterministic checks wherever possible, an LLM judge only for what code can't check, and humans to build the dataset and calibrate the judge.

## 1.4 What to measure for each kind of feature

**Extraction (structured output)**
- *Per-field accuracy*: store, total, date, tamper flag. This tells you **which** field breaks.
- *Decision accuracy* plus a **confusion matrix**: are we wrongly rejecting real users, or wrongly approving fraud? Those two errors have very different costs.

**Retrieval** (no LLM needed, so it's cheap)
- **hit@k**: is *any* relevant chunk in the top k? If not, the LLM never had a chance.
- **recall@k**: what *fraction* of the needed chunks are in the top k? This matters for multi-chunk answers (Gold + partner bonus).
- **MRR** (mean reciprocal rank): how high the first relevant chunk ranks (1.0 = always first).

**RAG generation**
- **Fact accuracy**: does the answer contain the required facts (`45`, `4%`, `₹500`)?
- **Abstention accuracy**: does it say "don't know" exactly when it should? This tests hallucination resistance.
- **Citation precision**: are the cited sources actually relevant?
- **Faithfulness** (judge): is every claim supported by the retrieved sources?

**Always:** latency p50/p95, and errors counted **separately** from wrong answers (Part 3 shows why).

## 1.5 When to run evals

```
change a prompt / model / chunking / k / retrieval  ──► run the relevant suite, compare with the previous run
                                                         regression? ──► don't ship
nightly / weekly                                    ──► catch provider-side model drift
a user reports a bad answer                         ──► add it to the golden dataset (it becomes a permanent regression test)
production                                          ──► online metrics: needs_review rate, human overrides,
                                                         thumbs-down, "I don't know" rate
```
**Offline evals** (this lesson) run on a fixed dataset before shipping. **Online evals** watch real traffic after shipping. You need both.

## 1.6 How to build a golden dataset

- **Start small** (15–50 cases) and grow it. A small, hand-checked dataset beats a big sloppy one.
- **Cover the categories**, not just the happy path: our tags include `ocr`, `tax`, `discount`, `fraud`, `injection`, `synonym`, `exact-token`, `multi-chunk`, `unanswerable`.
- **Include distractors**: "Amount tendered 1000 / Change 101" next to the real total of 899.
- **Every production bug becomes a case**, so a fixed bug stays fixed.
- **Label the desired behaviour, even when the current code fails it.** Our `missing-date` case was *expected to fail*, and it pointed to a real bug.
- **Validate the dataset itself**: a test checks every `relevant` section exists ([tests/test_evals.py:59](../tests/test_evals.py#L59)). A typo in a label silently corrupts a metric.

---

# Part 2 — Code walkthrough (read in this order)

## 2.1 Datasets: [evals/datasets/](../evals/datasets/)

**[extraction.jsonl](../evals/datasets/extraction.jsonl)** has one receipt per line:
```json
{"id": "tendered-vs-total",
 "raw_text": "Sahyadri Electronics\n...Total 899.00\nAmount tendered 1000.00\nChange 101.00",
 "claim":    {"store": "Sahyadri Electronics", "total": "899.00", "purchased_on": "2026-10-02"},
 "expected": {"store_name": "Sahyadri Electronics", "total": 899.00, "purchased_on": "2026-10-02", "looks_tampered": false},
 "expected_status": "validated", "tags": ["distractor-amounts"]}
```
- `claim` is what the user says they bought; `expected` is what's really on the receipt; `expected_status` is the correct final decision.
- 7 genuine receipts (tax, discount, ₹ symbol, OCR noise, rounding…), 3 fraud cases that should be rejected, and 3 that should go to review.

**[rag.jsonl](../evals/datasets/rag.jsonl)** has one question per line:
```json
{"id": "platinum-cap",
 "question": "I'm a Platinum member and spent ₹20,000 at Nilgiri Mart on one receipt. How much cashback will I get?",
 "relevant": ["cashback-rates > Rates by membership tier", "cashback-rates > Partner store bonus", "cashback-rates > Caps"],
 "must_include": ["500"], "answerable": true, "tags": ["multi-chunk", "reasoning", "hard"]}
```
- `relevant` uses `doc > section` names, not database ids, so labels survive a re-ingest.
- `must_include` is the minimum fact a correct answer must state. Keep it **specific** (`"500"`, not `"cashback"`).
- 16 answerable questions (synonyms, exact codes, numbers, multi-chunk) and 3 that should **not** be answered (2 unanswerable, 1 injection, which uses `must_not_include: ["50%"]`).

## 2.2 Metrics: [evals/metrics.py](../evals/metrics.py)

Each metric is a small pure function, so its maths is easy to check and unit-test ([tests/test_evals.py](../tests/test_evals.py)).

| Lines | Function | Answers |
|---|---|---|
| [9](../evals/metrics.py#L9) | `hit_at_k` | Was *any* relevant chunk in the top k? |
| [14](../evals/metrics.py#L14) | `recall_at_k` | What fraction of the needed chunks are in the top k? |
| [19](../evals/metrics.py#L19) | `reciprocal_rank` | 1/rank of the first relevant chunk |
| [30](../evals/metrics.py#L30) | `contains_all` | Does the answer contain every required fact? |
| [36](../evals/metrics.py#L36) | `contains_none` | …and none of the forbidden ones (injection) |
| [41](../evals/metrics.py#L41) | `citation_precision` | Were the cited sources relevant? |
| [49](../evals/metrics.py#L49), [56](../evals/metrics.py#L56) | `store_match`, `money_match` | Fuzzy store name, money within ₹0.01 |
| [69](../evals/metrics.py#L69) | `p50_p95` | Latency percentiles. p95 is what your slowest users feel |
| [77](../evals/metrics.py#L77) | `confusion` | Expected vs predicted decision counts |

## 2.3 The judge: [evals/judge.py](../evals/judge.py)

| Lines | What | Why |
|---|---|---|
| [1-7](../evals/judge.py#L1) | Docstring: known judge biases | Read it. These are interview answers |
| [12](../evals/judge.py#L12) | `JUDGE_VERSION` | The judge prompt is versioned like any other prompt; changing it changes the scores |
| [14-19](../evals/judge.py#L14) | Rubric: every claim must be supported by the sources; correct arithmetic counts; "don't know" is faithful | A **binary** verdict, not a 1–10 score: easier to agree on, less noisy |
| [22-24](../evals/judge.py#L22) | `Verdict(faithful, unsupported_claims)` | Requires **evidence** (the exact unsupported phrases), so you can audit the judge |

## 2.4 The runner: [evals/run.py](../evals/run.py)

**Shared infrastructure**

| Lines | What | Why |
|---|---|---|
| [38](../evals/run.py#L38) | `THRESHOLDS`: the **quality gate** per suite | "We don't ship if decision accuracy drops below 0.85" |
| [45-63](../evals/run.py#L45) | `RPM = 14` + `RateLimiter` | Free tier = **15 requests/min per model**. Without this, the eval measured our quota, not our model (Part 3) |
| [70-84](../evals/run.py#L70) | `with_retry` | Throttle → call → retry on 429/503, **waiting as long as the server says** ([line 83](../evals/run.py#L83): "Please retry in 8.38s"). Returns the call's own latency, so time spent queuing isn't counted as model latency |
| [87](../evals/run.py#L87) | `bounded` | At most 3 concurrent calls |

**Suite 1: extraction** ([line 99](../evals/run.py#L99))
1. For each receipt: real `extraction.extract()` → real `extraction.decide()`. The same code as production, which is the point.
2. [Line 107](../evals/run.py#L107): an `LLMError` after retries is recorded as an **infra error**, never as a wrong answer.
3. [Line 125](../evals/run.py#L125) onward: per-field accuracy, decision accuracy, confusion matrix and latency, over the error-free rows only.

**Suite 2: retrieval** ([line 158](../evals/run.py#L158))
1. Only answerable questions (unanswerable ones have no "right chunk").
2. [Line 164](../evals/run.py#L164): all questions embedded in **one batched call**, which is cheap.
3. For each question, rank chunks three ways (vector, keyword, [hybrid via RRF](../evals/run.py#L169)), then compute hit@2/4, recall@2/4 and MRR for each. **Same questions, three systems**: a controlled experiment.

**Suite 3: RAG end to end** ([line 205](../evals/run.py#L205))
1. Real `hybrid_search` → real `answer_question` (production code again).
2. [Lines 229-232](../evals/run.py#L229): fact check for answerable cases; forbidden-content check for injection cases.
3. [Line 233](../evals/run.py#L233): optional judge, only for answers that claim to be answerable.

**Reporting and gating** ([`main`, line 292](../evals/run.py#L292))

| Lines | What |
|---|---|
| [275](../evals/run.py#L275) | `config_for`: records **model, embedding model, prompt version, k, judge version** with every result. A score without its config is meaningless |
| [287](../evals/run.py#L287) | `previous_summary`: loads the last saved run, so the report shows **Δ** for each metric |
| [324-327](../evals/run.py#L324) | Gate: any metric below its threshold → FAIL. **Any infra error → INVALID RUN** |
| [333](../evals/run.py#L333) | Saves everything to `evals/results/<suite>-<timestamp>.json`, so history is auditable |
| [335](../evals/run.py#L335) | Exit code 1 on failure → usable as a **CI gate** (Module 6) |
| [345](../evals/run.py#L345), [360](../evals/run.py#L360) | `--repeat N`: average the scores, keep the **minimum**, and list **flaky** cases (passed in some runs, failed in others) |

## 2.5 Tests for the eval code: [tests/test_evals.py](../tests/test_evals.py)

| Lines | Tests |
|---|---|
| [13-40](../tests/test_evals.py#L13) | Each metric on hand-computed examples |
| [42](../tests/test_evals.py#L42) | Flaky detection ignores errored runs, and **still reports a case that errored in the last run**. This test found a real bug in the runner |
| [51](../tests/test_evals.py#L51) | The rate limiter really spaces out calls |
| [59](../tests/test_evals.py#L59) | **Dataset validation**: statuses are valid, every `relevant` section exists in the policy docs, ids are unique |

---

# Part 3 — What our evals actually found (the real story)

This is the most valuable part of the lesson. Every point below happened while building it.

### Finding 1: a real bug, in our code rather than the model
First extraction run: **every field 100% correct**, decision accuracy 0.923. The one failure was `missing-date`: a receipt with no date was **rejected as fraud**. The LLM correctly returned `purchased_on: null`; it was **our** `decide()` that treated "missing" like "contradicting".
- **Fix:** [app/extraction.py:67-80](../app/extraction.py#L67). Unreadable means `needs_review`, contradicting means `rejected`. Two unit tests were added.
- **Lesson:** evals test the **whole system**. A failure isn't always the model's fault, and the confusion matrix shows the *direction* of the error.

### Finding 2: "flakiness" that was really rate limiting
`--repeat 3` reported 5 flaky cases, all using the same receipt. Probing showed `429 RESOURCE_EXHAUSTED … limit: 15` per minute. We'd sent ~39 calls in 97s; the retry gave up after 7s while the server asked for up to 60s, and the failed calls were **scored as wrong answers**.
- **Fix:** a client-side rate limiter, retries that honour the server's "retry in Xs", and infra errors counted separately (an error now makes the run **INVALID**).
- **Re-run:** 12 of 13 cases passed 3 out of 3. Only `ocr-noise` was genuinely unstable.
- **Lesson:** **separate infrastructure failures from quality failures**, or your metric measures your quota. Always check *why* a case failed before acting on it.

### Finding 3: real non-determinism, then fixed with a prompt change
`ocr-noise` passed **1 of 3 runs** at `temperature=0`. In the failing runs the model called `B1G BAZAAR` / `Mi1k` "leetspeak tampering".
- **Fix:** prompt `extract-v2` ([app/extraction.py:25](../app/extraction.py#L25)): "OCR scanning errors (O/0, l/1/I…) are normal noise, NOT tampering."
- **The real test was what *didn't* break:** v2 × 3 runs = **39/39 passed**, and the fraud cases (`prompt-injection`, `items-dont-add-up`) were still caught every time. Without a regression suite, "be lenient about weird characters" could quietly have let tampered receipts through.
- **Lesson:** one run can't tell you whether a case is reliable; use `--repeat`. And a prompt fix needs the **whole** suite re-run, not just the case you were fixing.

### Finding 4: hybrid search wasn't better, here
| mode | hit@2 | hit@4 | recall@2 | recall@4 | MRR |
|---|---|---|---|---|---|
| vector | 1.00 | 1.00 | **0.98** | 1.00 | 1.00 |
| keyword | 0.94 | 1.00 | 0.89 | 0.95 | 0.80 |
| hybrid | 1.00 | 1.00 | 0.95 | 1.00 | 1.00 |

Vector-only matched or beat hybrid. Two honest conclusions:
- **The eval is too easy.** 15 chunks is a tiny haystack, so almost everything scores 1.0, and a metric that's always 1.0 can't tell good changes from bad ones. To measure retrieval properly you need a bigger corpus and harder cases (many similar documents, codes that appear only once).
- **"Hybrid is better" is a hypothesis, not a law.** It usually wins on large corpora full of codes and names. Here we **measured instead of assuming**. In an interview, "we A/B tested it and kept the simpler option" beats "we used hybrid because blogs said so".

### Finding 5: the judge was wrong once, and the string check was too strict once
RAG run: fact accuracy 0.947, abstention 1.0, citation precision 1.0, faithfulness (judge) 0.938. Neither failure was a real hallucination:
- `gold-partner`: the answer said "2% … plus an additional 2%" but **didn't state the 4% total**. That's an incomplete answer (the user asked "how much"), and earlier runs did say "totaling 4%". It's partly a prompt issue (see exercise 1) and partly a reminder that string checks are strict.
- `synonym-cigarettes`: the judge flagged "such as cigarettes" as unsupported, but cigarettes *are* tobacco, and the user asked about cigarettes. **Judge false positive.**
- And the hardest case worked: `platinum-cap` chained 3 chunks, 3% + 2% = 5% → ₹1,000 → capped at **₹500**.
- **Lesson:** **read the failures, don't just read the score.** 0.938 could mean "hallucinates 6% of the time"; here it meant "the judge is a bit pedantic". Calibrate the judge against your own labels before trusting its number.

---

# Part 4 — Interview Q&A

### 1. How do you know your LLM feature works?
A golden dataset of realistic and adversarial cases with known answers, scored by metrics that match the task (field accuracy, confusion matrix, hit@k, faithfulness), run on every prompt, model or retrieval change and compared with the previous run, with a CI gate. In production, online signals: human-review rate, overrides, user feedback, and every bad answer added to the dataset.

### 2. What's the difference between tests and evals?
Tests check that the code is correct, using a fake model: deterministic, pass/fail, run on every commit. Evals check that the output is good, using the real model: scored, non-deterministic, run on AI-relevant changes. Our project has both: `tests/` (32 tests, no network) and `evals/` (real Gemini).

### 3. How do you build an eval dataset with no production data yet?
Hand-write 20–50 cases covering the categories that matter: happy paths, edge cases (tax, OCR, discounts), adversarial cases (fraud, injection) and "should refuse" cases. Use tags so you can see which category fails. Have someone check the labels. Optionally use an LLM to *propose* variants, but a human approves them. Once live, sample real traffic and add every reported failure.

### 4. Which metrics for RAG?
Split retrieval from generation. Retrieval: hit@k, recall@k, MRR (cheap, no LLM). Generation: fact accuracy (deterministic where possible), abstention accuracy (says "don't know" when it should), citation precision, faithfulness (judge). Plus latency p50/p95 and cost per query. Splitting them tells you *which half* to fix.

### 5. What is LLM-as-judge, and what are its pitfalls?
A second LLM grades outputs against a rubric, for things code can't check (faithfulness, relevance, tone). Pitfalls: it can be wrong (ours flagged a correct "such as cigarettes"); it favours longer answers and its own model's style; it's sensitive to its own prompt; it costs calls. Mitigations: binary rubrics, ask for the evidence, version the judge prompt, use a different or stronger model than the one being judged where possible, and **measure agreement with human labels** before trusting its scores.

### 6. LLM outputs vary between runs. How do you evaluate that?
Run each case several times (`--repeat`) and report the mean, the **minimum** and per-case pass rates; treat "passes 1/3" as a failure to fix, not a pass. Use temperature 0 and structured output to reduce variance. But first **check that the variance isn't infrastructure**: ours was mostly rate-limit errors scored as failures.

### 7. You changed a prompt to fix one case. How do you know you didn't break others?
Re-run the **whole** suite, several times, and compare every metric with the baseline. Our OCR prompt fix said "tolerate odd characters", which could have weakened fraud detection, so the evidence that it was safe was the injection and arithmetic cases still passing 3 out of 3.

### 8. How do evals fit into CI/CD?
Unit tests on every commit (fast, free). Evals on PRs that touch prompts, models, chunking or retrieval, or nightly (slow, costly). Thresholds as a gate (exit code 1). Results saved with their config (model, prompt version) so a regression can be traced to the change that caused it. Rate-limited, and with infra errors failing the run as *invalid* rather than as a quality drop.

### 9. Your eval scores are all 1.0. Good news?
Usually not: the eval has stopped being informative. Either the cases are too easy or the dataset is too small to distinguish between options (our retrieval suite on 15 chunks). Add harder cases, a bigger corpus, more distractors, until scores have room to move.

### 10. What's a confusion matrix, and why use one here?
Rows = expected decision, columns = predicted. Accuracy alone hides **which** mistakes you make. Approving fraud (expected `rejected`, got `validated`) costs money; rejecting a genuine user costs trust; `needs_review` costs reviewer time. The matrix shows each separately, so you can tune for the costs that matter.

### 11. Offline vs online evaluation?
Offline: a fixed dataset before release, which is repeatable and catches regressions. Online: real traffic after release, which catches what your dataset didn't imagine: drift, new user behaviour, provider-side model updates. Online signals for us would be the needs_review rate, human override rate, "I don't know" rate and user thumbs-down. Feed online failures back into the offline set.

### 12. How do you choose between two models?
Same dataset, same prompt, both models, several repeats. Compare quality metrics, p50/p95 latency and cost per 1,000 requests. In 2a we did this by eye (3.8-flash at ~35s vs 3.5-flash-lite at ~1.5s); now it's one command per model, with numbers.

---

# Part 5 — Self-check (answer before opening)

<details><summary>1. Why can't our unit tests in tests/ tell us whether extraction is good?</summary>
They use <code>FakeLLM</code>, which returns whatever we tell it. They prove our code handles a given output correctly, not that the real model produces good output. Only an eval with the real model can measure that.
</details>

<details><summary>2. The extraction eval shows 100% field accuracy but 92% decision accuracy. Where's the bug: model or code?</summary>
Code. If every extracted field matches the label, the model read the receipt correctly, so a wrong decision must come from the decision logic. That's how we found the <code>missing-date</code> bug in <code>decide()</code>.
</details>

<details><summary>3. A case fails in 1 of 3 runs. What do you check first?</summary>
Whether it was an infra error (quota, timeout) or a real wrong answer. Ours looked flaky because of 429 rate limits. Only after ruling that out do you treat it as model non-determinism and fix the prompt, schema or model.
</details>

<details><summary>4. Why does `relevant` use "doc > section" names instead of chunk ids?</summary>
Ids change every time chunks are deleted and re-inserted (re-ingest, model change). Names are stable, so the golden labels stay valid. A dataset test also checks that every name exists.
</details>

<details><summary>5. Retrieval scores: vector-only ≈ hybrid. Should we delete keyword search?</summary>
Not on this evidence alone: the eval is saturated (15 chunks, nearly everything 1.0), so it can't tell them apart. Keyword search protects exact-token queries (codes, SKUs) that will matter at scale. The right move is a harder retrieval set, then decide. It's fine to say "measured, no difference yet, kept it for exact-match safety".
</details>

<details><summary>6. The judge says faithfulness is 0.938. What do you do before reporting "6% hallucination"?</summary>
Read the flagged cases. Ours was a judge false positive ("such as cigarettes"). Measure judge agreement with human labels on a sample; if it's unreliable, fix the rubric (e.g. allow terms the user's question introduced) and version it.
</details>

<details><summary>7. Why does an infra error make the whole run INVALID instead of just lowering the score?</summary>
A lower score would be misread as a quality regression and might block a good change; ignoring the errors would hide missing data. Marking the run invalid says "this measurement didn't happen, run it again".
</details>

---

## Exercises (30–40 min, about 1 minute of quota per run)
1. **Fix `gold-partner` with a prompt change.** In [app/rag/answer.py](../app/rag/answer.py#L10), add a rule: "When asked for an amount or rate, state the final combined figure." Bump `PROMPT_VERSION` to `rag-answer-v2`. Run `evals.run rag --repeat 2` and compare. Did fact accuracy rise? Did abstention stay at 1.0?
2. **Fix the judge.** In [evals/judge.py](../evals/judge.py#L14), allow restating terms from the question ("cigarettes" → tobacco). Bump `JUDGE_VERSION`. Re-run `rag --judge`. Did faithfulness change *without* the answers changing? That's why judge versions are recorded.
3. **Run `evals.run rag --k 2`.** Which case breaks, and does `retrieval_recall@k` show why?
4. **Make retrieval harder.** Add 3 questions to `rag.jsonl` that vector search should struggle with (e.g. just `"PB-APPEAL-7"` with no other words). Re-run `retrieval`. Do the modes separate now?
5. **Compare models.** Set `GEMINI_MODEL=gemini-3.1-flash-lite` in `.env`, run `extraction --repeat 2`, compare with the saved results, then set it back.

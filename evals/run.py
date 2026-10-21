"""Run an eval suite against the REAL models, print a report, save results, gate on thresholds.

  uv run python -m evals.run extraction
  uv run python -m evals.run extraction --repeat 3              # expose flaky cases
  uv run python -m evals.run extraction --model gemini-3.1-flash-lite   # try another model
  uv run python -m evals.run retrieval                          # embeddings only — cheap
  uv run python -m evals.run rag --judge --k 4                  # + LLM-as-judge faithfulness

Exit code 1 if a gated metric is below its threshold → usable as a CI quality gate (Module 6).
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from datetime import date

from pydantic import BaseModel

from sqlalchemy import select

from app import extraction
from app.config import get_settings
from app.db import SessionLocal, engine
from app.llm import get_llm
from app.llm.base import LLMError
from app.rag import answer as rag_answer
from app.rag.embeddings import get_embedder
from app.rag.store import PolicyChunk, hybrid_search, keyword_search, rrf, vector_search
from evals import metrics as m
from evals.judge import JUDGE_VERSION, judge_faithfulness

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

# Quality gates: the minimum we accept before shipping a prompt/model/chunking change.
THRESHOLDS = {
    "extraction": {"decision_accuracy": 0.85, "total_accuracy": 0.9},
    "retrieval": {"hybrid.hit@4": 0.9},
    "rag": {"fact_accuracy": 0.8, "abstention_accuracy": 0.9},
}

CONCURRENCY = 3  # a few calls in flight at once
RPM = 14  # Gemini free tier: 15 requests/minute PER MODEL — stay just under it


class RateLimiter:
    """Client-side throttle: at most `rpm` call starts per minute (evenly spaced)."""

    def __init__(self, rpm: int):
        self.interval, self._next, self._lock = 60 / rpm, 0.0, asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)


LIMITER = RateLimiter(RPM)


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (ROOT / "datasets" / f"{name}.jsonl").read_text().splitlines() if line.strip()]


async def with_retry(fn, attempts: int = 5) -> tuple[object, float]:
    """Throttle, call, and retry transient errors. Returns (result, seconds the successful call took) so
    latency metrics measure the model — not time spent waiting in our own rate limiter."""
    for i in range(attempts):
        await LIMITER.wait()
        t = time.perf_counter()
        try:
            return await fn(), time.perf_counter() - t
        except LLMError as e:
            msg = str(e)
            if i == attempts - 1 or not any(s in msg for s in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")):
                raise
            # Honour the server's hint ("Please retry in 8.38s") instead of guessing a backoff.
            hint = re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
            wait = float(hint.group(1)) + 1 if hint else 2 ** (i + 1)
            code = "429 quota" if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) else "503 overloaded"
            log(f"    ↻ {code} — retrying in {wait:.0f}s (attempt {i + 2}/{attempts})")
            await asyncio.sleep(wait)


def log(msg: str) -> None:
    """Progress goes to stderr so it never mixes with the report (and shows the run isn't frozen)."""
    print(msg, file=sys.stderr, flush=True)


class _Ping(BaseModel):
    ok: bool


async def preflight() -> None:
    """One tiny call before a long run: if the model is down, stop now with a clear message
    instead of burning minutes of retries and then producing an INVALID run."""
    llm, model = get_llm(), get_settings().gemini_model
    log(f"preflight: checking {model} …")
    try:
        await with_retry(lambda: llm.generate_structured("Reply ok=true.", "ping", _Ping), attempts=3)
    except LLMError as e:
        log(f"\n✗ {model} is unavailable right now: {str(e)[:160]}")
        log("  Free-tier capacity varies by model and time of day. Options:")
        log("   • wait a few minutes and rerun")
        log("   • try another model for this run:  --model gemini-3.1-flash-lite")
        log("     (results record the model, so they won't be confused with your baseline)")
        sys.exit(2)
    log("preflight: ok\n")


async def bounded(items, worker):
    sem = asyncio.Semaphore(CONCURRENCY)

    async def run(item):  # noqa: F811 — semaphore-bounded worker
        async with sem:
            return await worker(item)

    done = 0

    async def run_and_report(item):
        nonlocal done
        row = await run(item)
        done += 1
        status = "error" if "error" in row else "done"
        log(f"  [{done}/{len(items)}] {row['id']} {status}" + (f" ({row['latency_s']}s)" if "latency_s" in row else ""))
        return row

    return await asyncio.gather(*(run_and_report(i) for i in items))


# ---------------------------------------------------------------- extraction

async def eval_extraction(args) -> tuple[dict, list[dict]]:
    llm, cases = get_llm(), load("extraction")

    async def one(c):
        claim = c["claim"]
        try:
            ex, latency = await with_retry(lambda: extraction.extract(c["raw_text"], llm))
        except LLMError as e:
            # Infra failure (quota, outage) ≠ wrong answer. Record it separately; never score it as quality.
            return {"id": c["id"], "tags": c.get("tags", []), "error": str(e)[:300], "decision_ok": None}
        status, notes = extraction.decide(
            claim["store"], Decimal(claim["total"]), date.fromisoformat(claim["purchased_on"]), ex
        )
        got = ex.model_dump()
        exp = c["expected"]
        return {
            "id": c["id"], "tags": c.get("tags", []), "latency_s": round(latency, 2),
            "store_ok": m.store_match(exp["store_name"], got.get("store_name")),
            "total_ok": m.money_match(exp["total"], got.get("total")),
            "date_ok": exp["purchased_on"] == got.get("purchased_on"),
            "tamper_ok": exp["looks_tampered"] == got.get("looks_tampered"),
            "expected_status": c["expected_status"], "status": status,
            "decision_ok": status == c["expected_status"],
            "got": got, "notes": notes, "note": c.get("note"),
        }

    all_rows = await bounded(cases, one)
    rows = [r for r in all_rows if "error" not in r]
    p50, p95 = m.p50_p95([r["latency_s"] for r in rows])
    labels = ["validated", "rejected", "needs_review"]
    summary = {
        "infra_errors": len(all_rows) - len(rows),
        "decision_accuracy": m.rate([r["decision_ok"] for r in rows]),
        "store_accuracy": m.rate([r["store_ok"] for r in rows]),
        "total_accuracy": m.rate([r["total_ok"] for r in rows]),
        "date_accuracy": m.rate([r["date_ok"] for r in rows]),
        "tamper_accuracy": m.rate([r["tamper_ok"] for r in rows]),
        "latency_p50_s": p50, "latency_p95_s": p95,
        "confusion": {"labels": labels, "matrix": m.confusion([(r["expected_status"], r["status"]) for r in rows], labels)},
    }
    return summary, rows


def print_extraction(summary, rows):
    print(f"\n{'case':22} {'store':5} {'total':5} {'date':5} {'tamper':6} {'expected':13} {'got':13}")
    for r in rows:
        ok = lambda b: " ✓  " if b else " ✗  "  # noqa: E731
        flag = "" if r["decision_ok"] else "  ← " + ("known gap" if r["note"] else "FAIL")
        print(f"{r['id']:22} {ok(r['store_ok'])}  {ok(r['total_ok'])}  {ok(r['date_ok'])}  {ok(r['tamper_ok'])}   "
              f"{r['expected_status']:13} {r['status']:13}{flag}")
    c = summary["confusion"]
    print("\nconfusion (rows = expected, cols = predicted):")
    print(" " * 14 + "".join(f"{lab:>14}" for lab in c["labels"]))
    for lab, row in zip(c["labels"], c["matrix"]):
        print(f"{lab:>14}" + "".join(f"{v:>14}" for v in row))


# ---------------------------------------------------------------- retrieval

async def eval_retrieval(args) -> tuple[dict, list[dict]]:
    """Only the answerable cases; compares the three retrieval modes on the SAME questions."""
    cases = [c for c in load("rag") if c["relevant"]]
    embedder = get_embedder()
    async with SessionLocal() as session:
        names = {c.id: f"{c.doc} > {c.section}" for c in await session.scalars(select(PolicyChunk))}
        qvecs = await embedder.embed([c["question"] for c in cases], kind="query")  # one batched call
        rows = []
        for c, qv in zip(cases, qvecs, strict=True):
            vec = await vector_search(session, qv, 10)
            kw = await keyword_search(session, c["question"], 10)
            fused = rrf([vec, kw])
            ranked = {
                "vector": [names[i] for i in vec],
                "keyword": [names[i] for i in kw],
                "hybrid": [names[i] for i in sorted(fused, key=fused.get, reverse=True)],
            }
            rel = set(c["relevant"])
            row = {"id": c["id"], "tags": c.get("tags", []), "relevant": c["relevant"]}
            for mode, lst in ranked.items():
                row[mode] = {
                    "hit@2": m.hit_at_k(lst, rel, 2), "hit@4": m.hit_at_k(lst, rel, 4),
                    "recall@2": m.recall_at_k(lst, rel, 2), "recall@4": m.recall_at_k(lst, rel, 4),
                    "mrr": m.reciprocal_rank(lst, rel), "top4": lst[:4],
                }
            rows.append(row)
    summary = {"infra_errors": 0} | {
        f"{mode}.{metric}": m.rate([r[mode][metric] for r in rows])
        for mode in ("vector", "keyword", "hybrid") for metric in ("hit@2", "hit@4", "recall@2", "recall@4", "mrr")
    }
    return summary, rows


def print_retrieval(summary, rows):
    print(f"\n{'mode':8} {'hit@2':>6} {'hit@4':>6} {'recall@2':>9} {'recall@4':>9} {'MRR':>6}")
    for mode in ("vector", "keyword", "hybrid"):
        print(f"{mode:8} " + " ".join(f"{summary[f'{mode}.{k}']:>{w}.2f}" for k, w in
                                     (("hit@2", 6), ("hit@4", 6), ("recall@2", 9), ("recall@4", 9), ("mrr", 6))))
    print("\nper question — rank of first relevant chunk (– = not in top 10):")
    print(f"{'case':22} {'vector':>7} {'keyword':>8} {'hybrid':>7}")
    for r in rows:
        rk = lambda mode: f"{round(1 / r[mode]['mrr'])}" if r[mode]["mrr"] else "–"  # noqa: E731
        print(f"{r['id']:22} {rk('vector'):>7} {rk('keyword'):>8} {rk('hybrid'):>7}")


# ---------------------------------------------------------------- rag (end to end)

async def eval_rag(args) -> tuple[dict, list[dict]]:
    cases, embedder, llm = load("rag"), get_embedder(), get_llm()

    async def one(c):
        t = time.perf_counter()
        async with SessionLocal() as session:  # one session per concurrent task
            hits = await hybrid_search(session, c["question"], embedder, k=args.k)
        retrieval_s = time.perf_counter() - t
        try:
            res, gen_s = await with_retry(lambda: rag_answer.answer_question(c["question"], hits, llm))
        except LLMError as e:
            return {"id": c["id"], "tags": c.get("tags", []), "error": str(e)[:300], "fact_ok": None}
        answer, answerable = res.answer, res.answerable
        cited = {f"{s.doc} > {s.section}" for s in res.sources}
        latency = retrieval_s + gen_s
        rel = set(c["relevant"])
        row = {
            "id": c["id"], "tags": c.get("tags", []), "question": c["question"], "answer": answer,
            "latency_s": round(latency, 2), "retrieved": [f"{h.doc} > {h.section}" for h in hits],
            "cited": sorted(cited),
            "abstention_ok": answerable == c["answerable"],
            "retrieval_recall": m.recall_at_k([f"{h.doc} > {h.section}" for h in hits], rel, args.k),
        }
        if c["answerable"]:
            row["fact_ok"] = bool(answerable) and m.contains_all(answer, c["must_include"])
            row["citation_precision"] = m.citation_precision(cited, rel)
        else:
            row["fact_ok"] = m.contains_none(answer, c.get("must_not_include", []))
        if args.judge and answerable:
            try:
                v, _ = await with_retry(lambda: judge_faithfulness([h.content for h in hits], answer, llm))
                row["faithful"], row["unsupported"] = v.faithful, v.unsupported_claims
            except LLMError:
                row["judge_error"] = True
        return row

    all_rows = await bounded(cases, one)
    rows = [r for r in all_rows if "error" not in r]
    answerable_rows = [r for r in rows if "citation_precision" in r]
    p50, p95 = m.p50_p95([r["latency_s"] for r in rows])
    summary = {
        "infra_errors": len(all_rows) - len(rows),
        "fact_accuracy": m.rate([r["fact_ok"] for r in rows]),
        "abstention_accuracy": m.rate([r["abstention_ok"] for r in rows]),
        "citation_precision": m.rate([r["citation_precision"] for r in answerable_rows]),
        "retrieval_recall@k": m.rate([r["retrieval_recall"] for r in answerable_rows]),
        "latency_p50_s": p50, "latency_p95_s": p95,
    }
    if args.judge:
        judged = [r for r in rows if "faithful" in r]
        summary["faithfulness (judge)"] = m.rate([r["faithful"] for r in judged])
    return summary, rows


def print_rag(summary, rows):
    print(f"\n{'case':24} {'fact':5} {'abst':5} {'recall':6}  answer")
    for r in rows:
        ok = lambda b: " ✓ " if b else " ✗ "  # noqa: E731
        extra = "" if r.get("faithful", True) else f"   ⚠ judge: unsupported {r['unsupported']}"
        print(f"{r['id']:24} {ok(r['fact_ok'])}   {ok(r['abstention_ok'])}  {r['retrieval_recall']:>5.2f}   "
              f"{r['answer'][:90]}{extra}")


# ---------------------------------------------------------------- report / save / compare / gate

SUITES = {"extraction": (eval_extraction, print_extraction),
          "retrieval": (eval_retrieval, print_retrieval),
          "rag": (eval_rag, print_rag)}


def config_for(suite: str, args) -> dict:
    s = get_settings()
    cfg = {"suite": suite, "llm": f"{s.llm_providers}:{s.gemini_model}", "embedding_model": s.embedding_model}
    if suite == "extraction":
        cfg["prompt_version"] = extraction.PROMPT_VERSION
    if suite == "rag":
        cfg |= {"prompt_version": rag_answer.PROMPT_VERSION, "k": args.k}
        if args.judge:
            cfg["judge"] = JUDGE_VERSION
    return cfg


def previous_summary(suite: str, cfg: dict) -> dict | None:
    """Latest saved run with the SAME model + prompt version — otherwise Δ would compare apples to oranges."""
    same = ("llm", "prompt_version", "embedding_model", "k")
    for f in sorted(RESULTS.glob(f"{suite}-*.json"), reverse=True):
        saved = json.loads(f.read_text())
        if all(saved["config"].get(key) == cfg.get(key) for key in same):
            return saved["summary"]
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("suite", choices=SUITES)
    p.add_argument("--k", type=int, default=4)
    p.add_argument("--judge", action="store_true", help="add LLM-as-judge faithfulness (rag suite)")
    p.add_argument("--repeat", type=int, default=1, help="run the suite N times to expose flaky (non-deterministic) cases")
    p.add_argument("--model", help="override GEMINI_MODEL for this run (recorded in the results)")
    p.add_argument("--no-save", action="store_true")
    args = p.parse_args()
    if args.model:
        os.environ["GEMINI_MODEL"] = args.model
        get_settings.cache_clear()  # settings and the LLM client are cached; rebuild them with the override
        get_llm.cache_clear()

    run, show = SUITES[args.suite]
    t = time.perf_counter()
    runs = asyncio.run(_run_many(run, args))
    summary, rows = runs[-1]
    if args.repeat > 1:
        summary = average_summaries([s for s, _ in runs])
        rows = mark_flaky(runs)
    cfg = config_for(args.suite, args) | ({"repeat": args.repeat} if args.repeat > 1 else {})
    baseline = previous_summary(args.suite, cfg)

    print(f"\n=== {args.suite} eval · {len(rows)} cases · {time.perf_counter() - t:.0f}s · {cfg}")
    show(summary, rows)

    print("\nsummary" + ("   (Δ vs previous run)" if baseline else ""))
    for key, val in summary.items():
        if isinstance(val, dict):
            continue
        delta = ""
        if baseline and isinstance(baseline.get(key), (int, float)):
            d = val - baseline[key]
            delta = f"   {'+' if d >= 0 else ''}{d:.3f}" if abs(d) > 1e-9 else "   ="
        print(f"  {key:26} {val}{delta}")

    failed = [f"{k} {summary[k]} < {v}" for k, v in THRESHOLDS[args.suite].items() if summary[k] < v]
    if summary.get("infra_errors"):
        # Don't let quota/outage errors pass (or fail) as a quality result — the run itself is invalid.
        failed.append(f"INVALID RUN: {summary['infra_errors']} infra errors (quota/outage), rerun later")
    print("\nGATE:", "PASS ✅" if not failed else "FAIL ❌  " + "; ".join(failed))

    if not args.no_save:
        RESULTS.mkdir(exist_ok=True)
        out = RESULTS / f"{args.suite}-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.write_text(json.dumps({"config": cfg, "summary": summary, "cases": rows}, indent=2, default=str))
        print(f"saved {out.relative_to(ROOT.parent)}")
    sys.exit(1 if failed else 0)


async def _run_many(run, args):
    try:
        if args.suite != "retrieval":  # retrieval uses embeddings only
            await preflight()
        runs = []
        for i in range(args.repeat):
            if args.repeat > 1:
                log(f"run {i + 1}/{args.repeat}")
            runs.append(await run(args))
        return runs
    finally:
        await engine.dispose()


def average_summaries(summaries: list[dict]) -> dict:
    out = {}
    for key, val in summaries[0].items():
        if isinstance(val, (int, float)):
            vals = [s[key] for s in summaries]
            out[key] = round(sum(vals) / len(vals), 3)
            out[f"{key} (min)"] = min(vals)
        elif isinstance(val, dict):
            out[key] = summaries[-1][key]  # e.g. confusion matrix: show the last run's
    return out


OK_KEY = {"decision_ok", "fact_ok"}


def mark_flaky(runs) -> list[dict]:
    """Per case: how many of the N runs passed. 'Flaky' = passed sometimes, failed sometimes.
    Runs where the case hit an infra error are excluded — an error says nothing about quality."""
    latest: dict[str, dict] = {}  # latest SUCCESSFUL row per case (a case that errored last run still shows)
    results: dict[str, list[bool]] = {}
    key = None
    for _, rows in runs:
        for r in rows:
            key = key or next((k for k in OK_KEY if k in r), None)
            if key is None:
                continue
            latest[r["id"]] = r
            results.setdefault(r["id"], []).append(bool(r[key]))
    last_rows = list(latest.values())
    if key is None:
        return runs[-1][1]
    for row in last_rows:
        outcomes = results[row["id"]]
        row["passes"] = f"{sum(outcomes)}/{len(outcomes)}"
        row["flaky"] = 0 < sum(outcomes) < len(outcomes)
    flaky = [f"{r['id']} ({r['passes']})" for r in last_rows if r["flaky"]]
    always_fail = [r["id"] for r in last_rows if not any(results[r["id"]])]
    print(f"\nstability over {len(runs)} runs → flaky: {flaky or 'none'} · always failing: {always_fail or 'none'}")
    return last_rows
    for i, row in enumerate(last_rows):
        passes = sum(bool(r[i][key]) for _, r in runs)
        row["passes"] = f"{passes}/{n}"
        row["flaky"] = 0 < passes < n
    flaky = [r["id"] for r in last_rows if r["flaky"]]
    always_fail = [r["id"] for r in last_rows if r["passes"] == f"0/{n}"]
    print(f"\nstability over {n} runs → flaky: {flaky or 'none'} · always failing: {always_fail or 'none'}")
    return last_rows


if __name__ == "__main__":
    main()

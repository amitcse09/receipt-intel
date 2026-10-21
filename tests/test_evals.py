"""The metrics are code too — if hit@k is wrong, every eval result built on it is wrong."""

import asyncio
import json
from pathlib import Path

from evals import metrics as m
from evals.run import RateLimiter, mark_flaky

DATASETS = Path(__file__).resolve().parent.parent / "evals" / "datasets"


def test_hit_recall_mrr():
    retrieved = ["a", "b", "c", "d"]
    assert m.hit_at_k(retrieved, {"c"}, 2) == 0.0 and m.hit_at_k(retrieved, {"c"}, 4) == 1.0
    assert m.recall_at_k(retrieved, {"a", "d"}, 2) == 0.5
    assert m.reciprocal_rank(retrieved, {"c"}) == 1 / 3
    assert m.reciprocal_rank(retrieved, {"z"}) == 0.0


def test_text_checks():
    assert m.contains_all("Totaling 4% cashback", ["4%", "cashback"])
    assert not m.contains_all("2% plus 2%", ["4%"])
    assert m.contains_none("I don't know.", ["50%"])
    assert m.citation_precision({"x", "y"}, {"x"}) == 0.5


def test_field_matchers():
    assert m.store_match("Big Bazaar", "BIG BAZAAR - Koramangala")
    assert m.store_match("Big Bazaar", "B1G BAZAAR")  # OCR noise tolerated
    assert not m.store_match("Big Bazaar", "DMart")
    assert m.money_match(499.5, 499.50) and not m.money_match(499.5, 4995.0)
    assert m.store_match(None, None) and not m.money_match(None, 1.0)


def test_confusion_and_percentiles():
    assert m.confusion([("a", "a"), ("a", "b"), ("b", "b")], ["a", "b"]) == [[1, 1], [0, 1]]
    p50, p95 = m.p50_p95([1.0, 2.0, 3.0, 4.0, 100.0])
    assert p50 == 3.0 and p95 > 4.0


def test_flaky_ignores_errored_runs():
    run1 = ({}, [{"id": "a", "decision_ok": True}, {"id": "b", "decision_ok": True}])
    run2 = ({}, [{"id": "a", "decision_ok": False}])  # "b" hit an infra error in run 2 → excluded
    rows = mark_flaky([run1, run2])
    by_id = {r["id"]: r for r in rows}
    assert by_id["a"]["flaky"] and by_id["a"]["passes"] == "1/2"
    assert not by_id["b"]["flaky"] and by_id["b"]["passes"] == "1/1"


async def test_rate_limiter_spaces_calls():
    limiter = RateLimiter(rpm=600)  # 0.1s apart
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.gather(*(limiter.wait() for _ in range(4)))
    assert loop.time() - start >= 0.29  # 3 gaps of 0.1s


def test_datasets_are_well_formed():
    """Catch typos in hand-written golden data before they silently skew metrics."""
    for line in (DATASETS / "extraction.jsonl").read_text().splitlines():
        c = json.loads(line)
        assert c["expected_status"] in {"validated", "rejected", "needs_review"}, c["id"]
        assert {"store", "total", "purchased_on"} <= c["claim"].keys(), c["id"]
    rag = [json.loads(line) for line in (DATASETS / "rag.jsonl").read_text().splitlines()]
    sections = {f"{p.stem} > {h[3:].strip()}" for p in (DATASETS.parent.parent / "data" / "policies").glob("*.md")
                for h in p.read_text().splitlines() if h.startswith("## ")}
    for c in rag:
        assert set(c["relevant"]) <= sections, f"{c['id']}: unknown section in {c['relevant']}"
        assert c["answerable"] == bool(c["relevant"]), c["id"]
    assert len({c["id"] for c in rag}) == len(rag)

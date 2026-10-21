"""Eval metrics — pure functions, no I/O. Each one answers a single, explainable question."""

from difflib import SequenceMatcher
from statistics import mean, quantiles

# ---------- retrieval: "did we find the right chunk?" ----------


def hit_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """1 if ANY relevant chunk is in the top k. 'Did the model get a chance to answer?'"""
    return float(any(r in relevant for r in retrieved[:k]))


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    """Fraction of relevant chunks in the top k. Matters when an answer needs SEVERAL chunks."""
    return len(relevant & set(retrieved[:k])) / len(relevant) if relevant else 1.0


def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float:
    """1/rank of the first relevant chunk (1.0 = ranked first, 0.5 = second, 0 = not found)."""
    for rank, r in enumerate(retrieved, start=1):
        if r in relevant:
            return 1 / rank
    return 0.0


# ---------- generation: "is the answer right?" ----------


def contains_all(text: str, needles: list[str]) -> bool:
    """Deterministic fact check: every required fact appears (case-insensitive)."""
    t = text.lower()
    return all(n.lower() in t for n in needles)


def contains_none(text: str, needles: list[str]) -> bool:
    t = text.lower()
    return not any(n.lower() in t for n in needles)


def citation_precision(cited: set[str], relevant: set[str]) -> float:
    """Of the sources the model cited, how many were actually relevant? (1.0 when nothing cited)"""
    return len(cited & relevant) / len(cited) if cited else 1.0


# ---------- extraction: "did we read the receipt correctly?" ----------


def store_match(expected: str | None, got: str | None) -> bool:
    if expected is None or got is None:
        return expected == got
    e, g = expected.casefold(), got.casefold()
    return e in g or SequenceMatcher(None, e, g).ratio() >= 0.75


def money_match(expected: float | None, got: float | None, tol: float = 0.01) -> bool:
    if expected is None or got is None:
        return expected == got
    return abs(expected - got) <= tol


# ---------- aggregation ----------


def rate(values: list[bool | float]) -> float:
    return round(mean(float(v) for v in values), 3) if values else 0.0


def p50_p95(latencies: list[float]) -> tuple[float, float]:
    if len(latencies) < 2:
        v = latencies[0] if latencies else 0.0
        return v, v
    qs = quantiles(latencies, n=20, method="inclusive")  # 5% steps: index 9 = p50, 18 = p95
    return round(qs[9], 2), round(qs[18], 2)


def confusion(pairs: list[tuple[str, str]], labels: list[str]) -> list[list[int]]:
    """rows = expected, cols = predicted."""
    idx = {lab: i for i, lab in enumerate(labels)}
    m = [[0] * len(labels) for _ in labels]
    for exp, got in pairs:
        m[idx[exp]][idx[got]] += 1
    return m

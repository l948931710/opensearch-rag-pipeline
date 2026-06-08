"""Ranking + aggregate metric implementations for the eval harness.

Pure functions, stdlib only. Conventions:
  - A "rank" is 1-based. rank=None means the gold item was not found in the result list.
  - relevances: list[bool] aligned to ranked results (index 0 = rank 1), True = relevant.

Fairness notes baked in:
  - Recall@k counts a query as a hit only if a gold item appears within the top-k.
  - Queries with no resolvable gold (gold not in the live corpus) must be EXCLUDED by the
    caller before aggregation — these functions assume every input query is scorable.
  - Bootstrap CI uses a fixed seed for reproducibility.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import List, Optional, Sequence


# ── per-query primitives ────────────────────────────────────────────────

def reciprocal_rank(rank: Optional[int]) -> float:
    return 1.0 / rank if rank else 0.0


def hit_at_k(rank: Optional[int], k: int) -> float:
    return 1.0 if (rank is not None and rank <= k) else 0.0


def dcg(relevances: Sequence[float], k: Optional[int] = None) -> float:
    rel = list(relevances)[: k] if k else list(relevances)
    return sum(r / math.log2(i + 2) for i, r in enumerate(rel))


def ndcg_at_k(relevances: Sequence[float], k: int) -> float:
    """nDCG@k. relevances aligned to ranked order (index 0 = top result)."""
    actual = dcg(relevances, k)
    ideal = dcg(sorted(relevances, reverse=True), k)
    return (actual / ideal) if ideal > 0 else 0.0


def precision_at_k(relevances: Sequence[float], k: int) -> float:
    rel = list(relevances)[:k]
    if not rel:
        return 0.0
    return sum(1.0 for r in rel if r > 0) / float(len(rel))


# ── aggregate primitives ────────────────────────────────────────────────

def mean(xs: Sequence[float]) -> float:
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else 0.0


def percentiles(xs: Sequence[float], ps=(50, 90, 95, 99)) -> dict:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {f"p{p}": None for p in ps}
    out = {}
    for p in ps:
        # nearest-rank percentile
        idx = max(0, min(len(xs) - 1, int(math.ceil(p / 100.0 * len(xs))) - 1))
        out[f"p{p}"] = xs[idx]
    return out


def bootstrap_ci(values: Sequence[float], n_boot: int = 2000, alpha: float = 0.05,
                 seed: int = 7) -> dict:
    """Bootstrap (percentile method) CI for the mean of `values`.

    Returns {mean, lo, hi, n}. With small n (~50) the CI width is the honest
    signal of how much to trust the point estimate.
    """
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n == 0:
        return {"mean": None, "lo": None, "hi": None, "n": 0}
    if n == 1:
        return {"mean": float(vals[0]), "lo": float(vals[0]), "hi": float(vals[0]), "n": 1}
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        sample = [vals[rng.randrange(n)] for _ in range(n)]
        boots.append(sum(sample) / n)
    boots.sort()
    lo = boots[int((alpha / 2) * n_boot)]
    hi = boots[int((1 - alpha / 2) * n_boot) - 1]
    return {"mean": sum(vals) / n, "lo": lo, "hi": hi, "n": n}


def ranking_summary(ranks: Sequence[Optional[int]], ks=(1, 3, 5, 10),
                    relevances_per_query: Optional[List[Sequence[float]]] = None,
                    ndcg_k: int = 10) -> dict:
    """Aggregate recall@k + MRR (+ optional nDCG@k) over a set of single-gold queries.

    ranks: 1-based rank of the (first) gold item per query; None if absent.
    relevances_per_query: optional aligned relevance lists for nDCG (multi-relevant gold).
    """
    n = len(ranks)
    out = {"n_queries": n}
    for k in ks:
        hits = [hit_at_k(r, k) for r in ranks]
        ci = bootstrap_ci(hits)
        out[f"recall@{k}"] = round(ci["mean"], 4) if ci["mean"] is not None else None
        out[f"recall@{k}_ci"] = [round(ci["lo"], 4), round(ci["hi"], 4)] if ci["lo"] is not None else None
    rr = [reciprocal_rank(r) for r in ranks]
    ci = bootstrap_ci(rr)
    out["mrr"] = round(ci["mean"], 4) if ci["mean"] is not None else None
    out["mrr_ci"] = [round(ci["lo"], 4), round(ci["hi"], 4)] if ci["lo"] is not None else None
    out["found_rate"] = round(mean([1.0 if r else 0.0 for r in ranks]), 4)
    if relevances_per_query is not None:
        nd = [ndcg_at_k(r, ndcg_k) for r in relevances_per_query]
        ci = bootstrap_ci(nd)
        out[f"ndcg@{ndcg_k}"] = round(ci["mean"], 4) if ci["mean"] is not None else None
        out[f"ndcg@{ndcg_k}_ci"] = [round(ci["lo"], 4), round(ci["hi"], 4)] if ci["lo"] is not None else None
    return out


def score_distribution(scores: Sequence[float]) -> dict:
    xs = [s for s in scores if s is not None]
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": round(min(xs), 4),
        "max": round(max(xs), 4),
        "mean": round(statistics.mean(xs), 4),
        "median": round(statistics.median(xs), 4),
        "stdev": round(statistics.pstdev(xs), 4) if len(xs) > 1 else 0.0,
        **{k: (round(v, 4) if v is not None else None)
           for k, v in percentiles(xs, (25, 50, 75, 90)).items()},
    }

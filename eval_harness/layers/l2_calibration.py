"""Layer 2 — Score calibration (no live calls; analyses L1's fused scores).

The serving layer labels context chunks 高/中/低 using fixed thresholds
(score_threshold_high=8.0, medium=5.0) that were calibrated on the PRE-rebuild weighted-
fusion score distribution. If the rebuild changed the score scale (e.g. Linear-exact vs
HNSW, or a fusion-weight drift), correct top-1 hits could fall below 8.0 and get labelled
中/低, making the LLM over-hedge. This layer checks the labels still fit.
"""
from __future__ import annotations

from typing import Dict

from .. import envboot  # noqa: F401


def _band(score, high, med):
    if score is None:
        return "none"
    if score >= high:
        return "高"
    if score >= med:
        return "中"
    return "低"


def run(l1_result: Dict, high: float = None, med: float = None) -> Dict:
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    rag = cfg.rag
    # When rerank is on, chunk["score"] (hence top1_score) is the 0~1 rerank score, so the
    # label bands must use the rerank thresholds, not the fused-score thresholds.
    if cfg.alibaba_vector.rerank_enable:
        high = rag.rerank_score_threshold_high if high is None else high
        med = rag.rerank_score_threshold_medium if med is None else med
    else:
        high = rag.score_threshold_high if high is None else high
        med = rag.score_threshold_medium if med is None else med

    pos = [q["top1_score"] for q in l1_result.get("per_query", [])
           if q["kind"] == "positive" and q["live_scorable"] and q["publicly_retrievable"]
           and q.get("gold_rank") == 1 and q["top1_score"] is not None]
    neg = [q["top1_score"] for q in l1_result.get("per_query", [])
           if q["kind"] == "negative" and q["top1_score"] is not None]

    n = len(pos)
    bands = {"高": 0, "中": 0, "低": 0}
    for s in pos:
        bands[_band(s, high, med)] += 1

    pos_mean = sum(pos) / n if n else None
    neg_mean = sum(neg) / len(neg) if neg else None
    # fraction of negatives that score in the 高 band (false-confidence -> would mislabel)
    neg_high = sum(1 for s in neg if s >= high) / len(neg) if neg else None

    # Healthy: most CORRECT top-1 hits land in 高 (or at least 中), and positives separate
    # clearly from negatives.
    frac_high = bands["高"] / n if n else None
    frac_at_least_med = (bands["高"] + bands["中"]) / n if n else None
    separation = (pos_mean - neg_mean) if (pos_mean is not None and neg_mean is not None) else None

    thresholds_ok = (
        n > 0 and frac_at_least_med is not None and frac_at_least_med >= 0.8
        and (separation is None or separation > 0)
        and (neg_high is None or neg_high <= 0.2)
    )

    notes = []
    if n and frac_high is not None and frac_high < 0.5:
        notes.append(f"Only {frac_high:.0%} of correct top-1 hits reach 高(>= {high}); "
                     f"score scale may have compressed post-rebuild — consider recalibrating "
                     f"score_threshold_high/medium.")
    if neg_high and neg_high > 0.2:
        notes.append(f"{neg_high:.0%} of negative queries top-1 still scores 高 — risk of "
                     f"confident answers on unanswerable queries.")
    if separation is not None and separation <= 0:
        notes.append("Positive and negative top-1 scores do not separate — relevance signal weak.")

    return {
        "thresholds": {"high": high, "medium": med},
        "n_correct_top1_positives": n,
        "positive_top1_mean": round(pos_mean, 4) if pos_mean is not None else None,
        "negative_top1_mean": round(neg_mean, 4) if neg_mean is not None else None,
        "separation_pos_minus_neg": round(separation, 4) if separation is not None else None,
        "label_bands_on_correct_hits": bands,
        "frac_高": round(frac_high, 3) if frac_high is not None else None,
        "frac_at_least_中": round(frac_at_least_med, 3) if frac_at_least_med is not None else None,
        "frac_negatives_in_高": round(neg_high, 3) if neg_high is not None else None,
        "thresholds_ok": bool(thresholds_ok),
        "notes": notes or ["Score labels still fit the rebuilt index's score distribution."],
    }

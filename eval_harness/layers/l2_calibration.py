"""Layer 2 — Score calibration (no live calls; analyses L1's fused scores).

The serving layer labels context chunks 高/中/低 using fixed thresholds
(score_threshold_high=8.0, medium=5.0) that were calibrated on the PRE-rebuild weighted-
fusion score distribution. If the rebuild changed the score scale (e.g. Linear-exact vs
HNSW, or a fusion-weight drift), correct top-1 hits could fall below 8.0 and get labelled
中/低, making the LLM over-hedge. This layer checks the labels still fit.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Dict, List

from .. import envboot  # noqa: F401

# Off-topic discrimination gate: AUC of positive vs OFF-TOPIC-negative top-1 scores, gated only when
# enough off-topic negatives exist (else advisory). 0.5 = no separation.
_AUC_MIN = float(os.environ.get("RAG_EVAL_L2_AUC_MIN", "0.85"))
_MIN_OFFTOPIC = int(os.environ.get("RAG_EVAL_L2_MIN_OFFTOPIC", "5"))


def _band(score, high, med):
    if score is None:
        return "none"
    if score >= high:
        return "高"
    if score >= med:
        return "中"
    return "低"


def _auc(pos: List[float], neg: List[float]):
    """Mann-Whitney AUC = P(a random positive scores above a random negative). 0.5 = indistinguishable,
    1.0 = perfectly separable. None if either side is empty."""
    if not pos or not neg:
        return None
    wins = ties = 0
    for p in pos:
        for q in neg:
            if p > q:
                wins += 1
            elif p == q:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


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
    neg_rows = [q for q in l1_result.get("per_query", [])
                if q["kind"] == "negative" and q["top1_score"] is not None]
    neg = [q["top1_score"] for q in neg_rows]
    # Negatives by type. Only OFF-TOPIC negatives are a true high-score "leak": near-miss /
    # answer-absent / metadata / modality-gap / live-data negatives retrieve a genuinely topically-
    # relevant chunk, so the reranker SHOULD score them high. Penalising that conflates relevance
    # with answerability (the generator decides answerability — verified 0 fabrication 2026-06-18).
    by_type = defaultdict(list)
    for q in neg_rows:
        by_type[q.get("neg_type") or "untyped"].append(q["top1_score"])
    offtopic = by_type.get("off_topic", [])

    n = len(pos)
    bands = {"高": 0, "中": 0, "低": 0}
    for s in pos:
        bands[_band(s, high, med)] += 1

    pos_mean = sum(pos) / n if n else None
    neg_mean = sum(neg) / len(neg) if neg else None
    neg_high = sum(1 for s in neg if s >= high) / len(neg) if neg else None       # informational only
    neg_high_by_type = {t: round(sum(1 for s in v if s >= high) / len(v), 3)
                        for t, v in sorted(by_type.items()) if v}

    frac_high = bands["高"] / n if n else None
    frac_at_least_med = (bands["高"] + bands["中"]) / n if n else None
    separation = (pos_mean - neg_mean) if (pos_mean is not None and neg_mean is not None) else None
    # principled discrimination metric (can the relevance score separate answerable from truly-
    # unanswerable?): AUC of positives vs OFF-TOPIC negatives. Gated only when >= _MIN_OFFTOPIC exist.
    auc_off = _auc(pos, offtopic) if (pos and len(offtopic) >= _MIN_OFFTOPIC) else None

    # The gate is POSITIVE calibration + off-topic discrimination WHEN measurable. The blanket
    # "negatives in 高" rate is now INFORMATIONAL (near-miss high is expected, not a defect) — this is
    # the 2026-06-18 metric-definition fix (relevance != answerability; 0 fabrication confirmed).
    # NOTE: separation over ALL negatives is NOT in the gate — near-miss/live-data/modality negatives
    # can legitimately out-score positives on mean (they're topically perfect matches), which would be
    # a false fail. Discrimination is judged ONLY against off-topic negatives via auc_off.
    thresholds_ok = (
        n > 0 and frac_at_least_med is not None and frac_at_least_med >= 0.8
        and (auc_off is None or auc_off >= _AUC_MIN)
    )

    notes = []
    if n and frac_high is not None and frac_high < 0.5:
        notes.append(f"Only {frac_high:.0%} of correct top-1 hits reach 高(>= {high}); score scale "
                     f"may have compressed — consider recalibrating score_threshold_high/medium.")
    if auc_off is not None and auc_off < _AUC_MIN:
        notes.append(f"off-topic discrimination AUC={auc_off:.2f} < {_AUC_MIN} — the relevance score "
                     f"does not separate answerable from off-topic queries.")
    if len(offtopic) < _MIN_OFFTOPIC:
        notes.append(f"only {len(offtopic)} off-topic negatives (< {_MIN_OFFTOPIC}) — off-topic "
                     f"discrimination UNMEASURED; author off-topic negatives (Step 5). neg-高 by type "
                     f"(advisory, near-miss/live-data/modality high is EXPECTED): {neg_high_by_type}")

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
        "neg_high_by_type": neg_high_by_type,
        "n_offtopic_neg": len(offtopic),
        "separation_auc_offtopic": round(auc_off, 3) if auc_off is not None else None,
        "thresholds_ok": bool(thresholds_ok),
        "notes": notes or ["Score labels fit: positives calibrate; off-topic discrimination "
                           "measured/within bounds."],
    }

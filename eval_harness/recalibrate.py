"""(a) Recalibrate score-band thresholds from the measured fused-score distribution.

The serving layer labels context chunks 高/中/低 by fixed thresholds (score_threshold_high,
score_threshold_medium) calibrated PRE-rebuild. This derives data-driven thresholds from the
current run so that:
  - most CORRECT top-1 hits land at least in 中  (so the LLM isn't told good context is 低),
  - few NEGATIVE-query top-1 scores reach 高     (so it doesn't over-trust unanswerable hits).

It also reports the best achievable pos/neg separation (Youden J) — if that's low, the score
SCALE has weak discrimination and thresholds alone can't fix it (a retrieval/fusion issue).

Usage: python -m eval_harness.recalibrate --results eval_harness/reports/run_<ts>/report.json
"""
from __future__ import annotations

import argparse
import json
import os
from typing import List

from .metrics import percentiles, mean


def _pct(xs: List[float], p: float):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    import math
    i = max(0, min(len(xs) - 1, int(math.ceil(p / 100.0 * len(xs))) - 1))
    return xs[i]


def best_separation(pos: List[float], neg: List[float]):
    """Youden's J optimal split between pos and neg top-1 scores."""
    if not pos or not neg:
        return None
    cand = sorted(set(pos + neg))
    best = {"threshold": None, "J": -1, "tpr": None, "fpr": None}
    for t in cand:
        tpr = sum(1 for s in pos if s >= t) / len(pos)
        fpr = sum(1 for s in neg if s >= t) / len(neg)
        J = tpr - fpr
        if J > best["J"]:
            best = {"threshold": round(t, 3), "J": round(J, 3),
                    "tpr": round(tpr, 3), "fpr": round(fpr, 3)}
    return best


def recommend(results: dict) -> dict:
    pq = results.get("l1", {}).get("per_query", [])
    pos_correct = [q["top1_score"] for q in pq
                   if q["kind"] == "positive" and q.get("live_scorable")
                   and q.get("publicly_retrievable") and q.get("gold_rank") == 1
                   and q.get("top1_score") is not None]
    pos_all = [q["top1_score"] for q in pq
               if q["kind"] == "positive" and q.get("top1_score") is not None]
    neg = [q["top1_score"] for q in pq if q["kind"] == "negative" and q.get("top1_score") is not None]

    cur = results.get("l2", {}).get("thresholds", {})
    cur_high = cur.get("high", 8.0)
    cur_med = cur.get("medium", 5.0)

    # data-driven recommendation: the 高/中/低 labels exist to tell the LLM how much to
    # trust each retrieved chunk, so anchor them to the CORRECT-hit distribution —
    # high = median of correct top-1 (so ~half of correct hits read 高), medium = P15
    # (so ~85% of correct hits read at least 中). We do NOT anchor to negatives because
    # pos/neg overlap so heavily that no threshold separates them (see best_separation).
    high_rec = round(_pct(pos_correct, 50) or cur_high, 1)
    med_rec = round(_pct(pos_correct, 15) or cur_med, 1)
    if med_rec >= high_rec:
        med_rec = round(high_rec - 1.5, 1)

    def bands(scores, hi, md):
        n = len(scores) or 1
        return {"高": round(sum(1 for s in scores if s >= hi) / n, 3),
                "中": round(sum(1 for s in scores if md <= s < hi) / n, 3),
                "低": round(sum(1 for s in scores if s < md) / n, 3)}

    sep = best_separation(pos_correct, neg)
    return {
        "n_pos_correct_top1": len(pos_correct), "n_neg": len(neg),
        "pos_correct_top1": {"mean": round(mean(pos_correct), 3),
                             **{k: (round(v, 3) if v else v) for k, v in
                                percentiles(pos_correct, (10, 25, 50, 75, 90)).items()}},
        "neg_top1": {"mean": round(mean(neg), 3),
                     **{k: (round(v, 3) if v else v) for k, v in
                        percentiles(neg, (10, 25, 50, 75, 90)).items()}},
        "current_thresholds": {"high": cur_high, "medium": cur_med},
        "current_bands_on_correct_hits": bands(pos_correct, cur_high, cur_med),
        "current_bands_on_negatives": bands(neg, cur_high, cur_med),
        "recommended_thresholds": {"high": high_rec, "medium": med_rec},
        "recommended_bands_on_correct_hits": bands(pos_correct, high_rec, med_rec),
        "recommended_bands_on_negatives": bands(neg, high_rec, med_rec),
        "best_separation_youden": sep,
        "env_lines": [f"RAG_SCORE_THRESHOLD_HIGH={high_rec}",
                      f"RAG_SCORE_THRESHOLD_MEDIUM={med_rec}"],
        "interpretation": (
            "Recommended thresholds re-center 高/中 onto the correct-hit distribution so the LLM "
            "stops seeing good context labelled 低/中. "
            + ("HOWEVER pos/neg separation is weak (best Youden J="
               + str(sep["J"] if sep else "n/a") + " at score " + str(sep["threshold"] if sep else "?")
               + "): the fused score barely distinguishes answerable from unanswerable queries, so "
                 "lowering 高 also lets more negatives reach 高 (see recommended_bands_on_negatives). "
                 "A threshold change is a modest patch; the real fix for confidence is improving "
                 "score discrimination (re-tune fusion weights / add a reranker / per-query score "
                 "normalization)." if (sep and sep["J"] < 0.5)
               else "Separation is adequate; recalibration should restore useful labels.")
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    args = ap.parse_args()
    results = json.load(open(args.results, encoding="utf-8"))
    rec = recommend(results)
    out = os.path.join(os.path.dirname(args.results), "recalibration.json")
    json.dump(rec, open(out, "w"), ensure_ascii=False, indent=2)
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

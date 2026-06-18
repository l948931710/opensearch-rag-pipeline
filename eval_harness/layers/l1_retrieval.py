"""Layer 1 — Retrieval ranking quality (drives L2 calibration + L6 latency too).

Runs the PRODUCTION retrieval (search_chunks: 3-way weighted hybrid, order=DESC) once per
gold case and scores the rank of the gold document.

Fairness:
  - Public path (user_dept=None) is used for ranking. A positive whose gold doc is
    permission-gated (dept_internal/restricted) is NOT publicly retrievable, so it is
    reported separately and EXCLUDED from the public-recall metric (the index is not
    charged for correct permission filtering).
  - Only `live_scorable` positives (gold doc present in the live index) count toward recall.
  - Negatives have no gold doc: we record their top-1 score to detect over-confident false
    matches; true interception is measured at the answer layer (L3).
  - All aggregates carry bootstrap 95% CIs (n~50 is small).
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict, List

from .. import envboot  # noqa: F401
from ..matching import gold_doc_rank, relevance_vector, keyword_coverage
from ..metrics import ranking_summary, score_distribution, percentiles, mean, bootstrap_ci


def _publicly_retrievable(case: Dict) -> bool:
    perms = case.get("expected_permission") or []
    return all(p == "public" for p in perms)  # empty => treat as public


def run(cases: List[Dict], top_k: int = 10, stitch_window: int = 1) -> Dict:
    # PRODUCTION retrieval path: retrieve_and_enrich = 3-way hybrid + neighbor stitching
    # + step-card expansion (exactly what api.py / dingtalk_bot.py serve). Verified that
    # stitching adds same-doc neighbors and does not change doc-recall vs raw search_chunks.
    from opensearch_pipeline.retriever import retrieve_and_enrich

    per_query: List[Dict] = []
    for c in cases:
        t0 = time.time()
        try:
            res = retrieve_and_enrich(c["query"], top_k=top_k, user_dept=None,
                                      stitch_window=stitch_window)
            err = None
        except Exception as e:
            res, err = [], f"{type(e).__name__}: {e}"[:200]
        latency_ms = int((time.time() - t0) * 1000)

        names = c.get("expected_docs", [])
        ids = c.get("expected_doc_ids", [])
        rank = gold_doc_rank(res, names, ids) if (names or ids) else None
        relv = relevance_vector(res, names, ids) if (names or ids) else []
        scores = [r.get("score") for r in res if r.get("score") is not None]
        concat = " ".join(str(r.get("chunk_text") or "") for r in res[:top_k])
        kwcov = (None if not c.get("keyword_gt")
                 else round(keyword_coverage(concat, c["keyword_gt"]), 4))
        per_query.append({
            "qid": c["qid"], "query": c["query"], "module": c["module"],
            "source": c.get("source"),
            "dept": c.get("dept"), "difficulty": c.get("difficulty"), "kind": c["kind"],
            "neg_type": c.get("neg_type"),  # off_topic / near_miss_answer_absent / metadata / modality_gap / live_data
            "publicly_retrievable": _publicly_retrievable(c),
            "live_scorable": c.get("live_scorable"),
            "expected_permission": c.get("expected_permission"),
            "gold_rank": rank, "found_top10": bool(rank),
            "top1_score": scores[0] if scores else None,
            "top3_scores": scores[:3], "n_results": len(res),
            "relevances": relv,
            "keyword_coverage_topk": kwcov,
            # content hit: gold keywords present in retrieved context (robust to mislabeled
            # gold-doc names in the reuse120 JSON set)
            "content_hit": (None if kwcov is None else bool(kwcov >= 0.5)),
            "latency_ms": latency_ms, "error": err,
        })

    # ── positive recall (public, scorable only) ──
    pos = [q for q in per_query if q["kind"] == "positive" and q["live_scorable"]]
    pos_public = [q for q in pos if q["publicly_retrievable"]]
    gated = [q for q in pos if not q["publicly_retrievable"]]

    # Headline = single-target recall. multi_doc queries span several docs, so single-doc
    # rank is not the right metric for them (and some reuse120 multi_doc gold labels are
    # unreliable) -> report them separately, not in the headline.
    pos_single = [q for q in pos_public if q.get("difficulty") != "multi_doc"]
    pos_multi = [q for q in pos_public if q.get("difficulty") == "multi_doc"]

    summary = ranking_summary([q["gold_rank"] for q in pos_single], ks=(1, 3, 5, 10),
                              relevances_per_query=[q["relevances"] for q in pos_single],
                              ndcg_k=10) if pos_single else {}
    summary_multidoc = ranking_summary([q["gold_rank"] for q in pos_multi], ks=(1, 3, 5, 10)) \
        if pos_multi else {}

    # per-stratum recall@1/@5 + mrr (over single-target positives)
    def stratum(keyfn):
        out = {}
        groups = defaultdict(list)
        for q in pos_single:
            groups[keyfn(q)].append(q)
        for key, qs in groups.items():
            rks = [x["gold_rank"] for x in qs]
            out[str(key)] = {
                "n": len(qs),
                "recall@1": round(mean([1.0 if (r and r <= 1) else 0.0 for r in rks]), 3),
                "recall@5": round(mean([1.0 if (r and r <= 5) else 0.0 for r in rks]), 3),
                "mrr": round(mean([1.0 / r if r else 0.0 for r in rks]), 3),
            }
        return out

    # content-hit rate (keyword-based) for cases carrying keyword GT — robust to mislabeled
    # gold-doc names (the right content is retrieved even when the doc label is wrong).
    kw_cases = [q for q in per_query if q["kind"] == "positive" and q.get("content_hit") is not None]
    content_hit_rate = round(mean([1.0 if q["content_hit"] else 0.0 for q in kw_cases]), 3) if kw_cases else None

    # ── negatives: over-confident false-match probe ──
    negs = [q for q in per_query if q["kind"] == "negative"]
    neg_top1 = [q["top1_score"] for q in negs if q["top1_score"] is not None]

    lat = [q["latency_ms"] for q in per_query if q["error"] is None]

    return {
        "top_k": top_k,
        "n_cases": len(cases),
        "n_positive_scorable": len(pos),
        "n_positive_public": len(pos_public),
        "n_positive_single_target": len(pos_single),
        "n_positive_multidoc": len(pos_multi),
        "n_permission_gated": len(gated),
        "permission_gated_qids": [q["qid"] for q in gated],
        "n_negative": len(negs),
        "errors": [q["qid"] for q in per_query if q["error"]],
        "ranking": summary,
        "ranking_multidoc": summary_multidoc,
        "content_hit_rate": content_hit_rate,
        "n_content_hit_cases": len(kw_cases),
        "by_module": stratum(lambda q: q["module"]),
        "by_source": stratum(lambda q: q.get("source")),
        "by_dept": stratum(lambda q: q.get("dept")),
        "by_difficulty": stratum(lambda q: q.get("difficulty")),
        "positive_top1_score_dist": score_distribution(
            [q["top1_score"] for q in pos_public if q["top1_score"] is not None]),
        "negative_top1_score_dist": score_distribution(neg_top1),
        "latency_ms": {"mean": round(mean(lat), 1), **percentiles(lat)},
        "per_query": per_query,
    }

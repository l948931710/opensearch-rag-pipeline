"""Layer 4 — Multimodal answer quality (image-text interleaving).

Reuses the production-faithful deterministic metrics from the existing
mm_answer_metrics.py (marker placement, availability, orphan/dangling). For image
*relevance* vs gold (which normally needs visual judging on rendered images), the live
path can't cheaply render OSS images, so we emit a TEXT-grounded multimodal judge bundle
(expected image descriptions vs the visual_summary/ocr of the images the card would show)
for the Claude panel — reported honestly as a caption-level proxy, not pixel rendering.

Only image-expecting (expect_images=True), live-scorable cases are run.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List

from .. import envboot  # noqa: F401
from ..gen_nothink import generate_answer_nothink

_SCRIPTS = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples/scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def run(cases: List[Dict], top_k: int = 7, max_images: int = 3) -> Dict:
    import mm_answer_metrics as M
    from opensearch_pipeline.retriever import retrieve_and_enrich

    img_cases = [c for c in cases if c.get("expect_images") and c.get("live_scorable")]
    if not img_cases:
        return {"applicable": False,
                "note": "No image-expecting, live-scorable cases in this run-set; "
                        "multimodal layer not exercised."}

    per_query: List[Dict] = []
    det_list: List[Dict] = []
    judge_bundle: List[Dict] = []

    for c in img_cases:
        try:
            chunks = retrieve_and_enrich(c["query"], top_k=top_k, user_dept=None,
                                         cosurface_images=True)
            gen = generate_answer_nothink(c["query"], chunks, pure_text=False)
        except Exception as e:
            per_query.append({"qid": c["qid"], "error": f"{type(e).__name__}: {e}"[:160]})
            continue
        ans = gen["answer"]
        det = M.analyze_answer(ans, chunks, max_images=max_images)
        det_list.append(det)
        per_query.append({"qid": c["qid"], "query": c["query"], "answer": ans, **det})
        judge_bundle.append({
            "qid": c["qid"], "query": c["query"],
            "expected_images": c.get("expected_images", []),
            "shown_image_captions": det.get("image_map_summary", {}),
            "n_available": det["n_available"], "strategy": det["strategy"],
            "answer": ans,
        })

    return {
        "applicable": True,
        "n_image_cases": len(img_cases),
        "aggregate": M.aggregate(det_list) if det_list else {},
        "per_query": per_query,
        "judge_bundle_mm": judge_bundle,
    }

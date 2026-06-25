"""Layer 3 — Answer quality (thinking OFF) + Claude-judge bundle.

Full serving path per case: retrieve_and_enrich -> generate (enable_thinking=False).
Produces:
  - deterministic metrics (refusal / source-leak / marker-leak / numbered-steps / length /
    keyword coverage / reasoning-leak verification)
  - a judge_bundle for an INDEPENDENT Claude panel (Qwen is the generator, so Qwen-as-judge
    would be self-evaluation bias -> we judge with Claude only).

The judge bundle is blinded to model identity and carries gold answer points + retrieved
context so Claude can score faithfulness (answer ⊆ context), correctness (answer vs gold),
completeness, and — for negatives — correct refusal / no fabrication.
"""
from __future__ import annotations

from typing import Dict, List

from .. import envboot  # noqa: F401
from ..gen_nothink import generate_answer_nothink
from ..matching import (refusal_detected, hard_refusal, source_leak_detected,
                        img_marker_count, numbered_step_count, keyword_coverage)
from ..metrics import mean
from opensearch_pipeline import llm_generator as _L

# Faithfulness must be judged against the SAME context the model saw, so the bundle carries
# the exact formatted context (capped at the production max_context_chars), not a short preview.
_CTX_CHARS = 1100


def run(cases: List[Dict], top_k: int = 7, retrieved_by_qid: Dict = None) -> Dict:
    from opensearch_pipeline.retriever import retrieve_and_enrich

    per_query: List[Dict] = []
    bundle: List[Dict] = []

    for c in cases:
        # reuse L1 retrieval if available to save an embedding call; else retrieve fresh
        chunks = (retrieved_by_qid or {}).get(c["qid"])
        if chunks is None:
            try:
                # authenticate as the case's dept (like L1) — else dept_internal docs are
                # ACL-filtered out at answer time and EVERY dept_internal case wrongly refuses
                # (prod authenticates the DingTalk/API user's dept; the eval must mirror that).
                chunks = retrieve_and_enrich(c["query"], top_k=top_k, user_dept=c.get("dept"))
            except Exception as e:
                chunks = []
                per_query.append({"qid": c["qid"], "error": f"retrieve:{e}"[:160]})
                continue
        try:
            gen = generate_answer_nothink(c["query"], chunks, pure_text=False)
        except Exception as e:
            per_query.append({"qid": c["qid"], "error": f"generate:{e}"[:160]})
            continue

        ans = gen["answer"]
        kw = c.get("keyword_gt") or []
        rec = {
            "qid": c["qid"], "kind": c["kind"], "module": c["module"],
            "dept": c.get("dept"), "difficulty": c.get("difficulty"),
            "live_scorable": c.get("live_scorable"),
            "query": c["query"], "answer": ans,
            "answer_chars": len(ans),
            "refusal": refusal_detected(ans),          # soft signal (diagnostic)
            "hard_refusal": hard_refusal(ans),          # answer dominated by a decline
            "source_leak": source_leak_detected(ans),
            "img_markers": img_marker_count(ans),
            "numbered_steps": numbered_step_count(ans),
            "had_reasoning": gen["had_reasoning"],   # MUST be False (thinking off)
            "keyword_coverage": (None if not kw else round(keyword_coverage(ans, kw), 4)),
            "n_context_chunks": len(chunks),
            "usage": gen.get("usage"),
            "latency_ms": gen.get("latency_ms"),
        }
        per_query.append(rec)

        # exact context the model consumed (production formatter + cap), so the judge can
        # score faithfulness against what the model actually saw — not a truncated preview.
        context_text = _L._format_context(chunks, max_chars=6000, pure_text=False)
        ctx = [{
            "i": i + 1,
            "title": ch.get("title"), "section": ch.get("section_title"),
            "text": (str(ch.get("chunk_text") or "")[:_CTX_CHARS]),
        } for i, ch in enumerate(chunks[:top_k])]
        bundle.append({
            "qid": c["qid"], "kind": c["kind"], "module": c["module"],
            "dept": c.get("dept"), "difficulty": c.get("difficulty"),
            "query": c["query"],
            "gold_answer_points": c.get("answer_points") or "",
            "gold_keywords": kw,
            "expected_docs": c.get("expected_docs", []),
            "context_text": context_text,
            "context": ctx,
            "answer": ans,
        })

    ok = [r for r in per_query if "error" not in r]
    pos = [r for r in ok if r["kind"] == "positive"]
    neg = [r for r in ok if r["kind"] == "negative"]
    # real over-refusal is only meaningful on positives whose gold doc IS in the index;
    # a positive whose gold is missing (coverage gap) SHOULD refuse -> not over-refusal.
    pos_scorable = [r for r in pos if r.get("live_scorable")]
    pos_unresolved = [r for r in pos if not r.get("live_scorable")]

    det = {
        "n_answered": len(ok),
        "errors": [r["qid"] for r in per_query if "error" in r],
        "reasoning_leak_count": sum(1 for r in ok if r["had_reasoning"]),  # want 0 (thinking off)
        "positive": {
            "n": len(pos),
            "n_scorable": len(pos_scorable), "n_unresolved_gold": len(pos_unresolved),
            # real over-refusal: hard refusal when the gold doc IS retrievable
            "over_refusal_rate": round(mean([1.0 if r["hard_refusal"] else 0.0 for r in pos_scorable]), 4) if pos_scorable else None,
            # coverage-gap refusals: hard refusal on positives whose gold isn't in the index (correct)
            "coverage_gap_refusal_rate": round(mean([1.0 if r["hard_refusal"] else 0.0 for r in pos_unresolved]), 4) if pos_unresolved else None,
            "over_refusal_rate_all_positives": round(mean([1.0 if r["hard_refusal"] else 0.0 for r in pos]), 4) if pos else None,
            "soft_decline_rate": round(mean([1.0 if r["refusal"] else 0.0 for r in pos]), 4) if pos else None,
            "source_leak_rate": round(mean([1.0 if r["source_leak"] else 0.0 for r in pos]), 4) if pos else None,
            "mean_keyword_coverage": round(mean([r["keyword_coverage"] for r in pos
                                                 if r["keyword_coverage"] is not None]), 4),
            "mean_chars": round(mean([r["answer_chars"] for r in pos]), 1) if pos else None,
        },
        "negative": {
            "n": len(neg),
            # negatives SHOULD decline / not fabricate -> hard refusal here is GOOD (interception).
            # Authoritative interception is the Claude judge's appropriate_refusal; this is the
            # rule-based proxy (a negative may legitimately be answerable from ANOTHER doc).
            "interception_rate_rulebased": round(mean([1.0 if r["hard_refusal"] else 0.0 for r in neg]), 4) if neg else None,
            "source_leak_rate": round(mean([1.0 if r["source_leak"] else 0.0 for r in neg]), 4) if neg else None,
        },
        "mean_latency_ms": round(mean([r["latency_ms"] for r in ok if r.get("latency_ms")]), 1) if ok else None,
    }

    return {"deterministic": det, "per_query": per_query, "judge_bundle": bundle}

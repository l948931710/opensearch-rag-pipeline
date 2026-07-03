# -*- coding: utf-8 -*-
"""#7 guard-flip A/B — READ-ONLY live retrieval, no LLM generation.

Replays the goldset through retrieve_and_enrich in the PRODUCTION multimodal regime
(rerank ON + cosurface ON — the env files don't set RAG_RERANK_ENABLE, so this script
forces it to match prod), then computes is_low_confidence_band under the cross-scale flag
(RAG_CONFIDENCE_CROSS_SCALE / #7) OFF vs ON on the SAME retrieved chunks. Reports how the
low-confidence guard flips, bucketed by answerable(positive)/unanswerable(negative).

Why this is the cheap first gate: #7 only manifests when a post-rerank chunk list MIXES
rerank-scored chunks (0-1) with fused-only chunks (cosurface补图, 0-~10). This script
measures exactly that flip with retrieval+rerank only — NO answer generation, NO judge.

Cost per query: query-embed + HA3 retrieve + (VL) rerank. Everything read-only against prod
(prod_ro overlay + envboot public endpoint). Must run from an IP allowlisted on the HA3
public endpoint (i.e. your laptop) — a non-allowlisted host gets HA3 403 Forbidden.

Usage:  python -m eval_harness.guard_flip_ab [--limit N] [--goldset PATH]
"""
from __future__ import annotations

import argparse
import json
import os

from . import envboot  # noqa: F401  side-effecting: loads .env + .env.prod_ro, forces public HA3
from .ha3live import install_into_retriever

HERE = os.path.dirname(os.path.abspath(__file__))


def _set(obj, attr, val):
    try:
        setattr(obj, attr, val)
    except Exception:
        object.__setattr__(obj, attr, val)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--goldset", default=os.path.join(HERE, "goldset", "golden_50.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "reports", "guard_flip_ab_result.json"))
    args = ap.parse_args()

    install_into_retriever()
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline import llm_generator as G
    from opensearch_pipeline.retriever import retrieve_and_enrich

    cfg = get_config()
    # Force the production multimodal regime: rerank ON + cosurface ON (env files set neither).
    _set(cfg.alibaba_vector, "rerank_enable", True)
    _set(cfg.rag, "image_cosurface", True)
    print("[facts]", json.dumps(envboot.facts().get("keys_present", {}), ensure_ascii=False),
          "| rerank_enable=", cfg.alibaba_vector.rerank_enable,
          "cosurface=", cfg.rag.image_cosurface, flush=True)

    cases = json.load(open(args.goldset, encoding="utf-8"))
    if args.limit:
        cases = cases[:args.limit]

    rows = []
    for i, c in enumerate(cases):
        try:
            chunks = retrieve_and_enrich(c["query"], top_k=7, user_dept=c.get("dept"),
                                         cosurface_images=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] {c['qid']} RETRIEVE-ERROR: {e!r}", flush=True)
            rows.append({"qid": c["qid"], "kind": c.get("kind"), "err": repr(e)})
            continue
        n_rerank = sum(1 for ch in chunks if isinstance(ch.get("rerank_score"), (int, float)))
        n_fusedonly = sum(1 for ch in chunks
                          if not isinstance(ch.get("rerank_score"), (int, float))
                          and isinstance(ch.get("score"), (int, float)))
        _set(cfg.rag, "confidence_cross_scale", False)
        low_off = bool(G.is_low_confidence_band(chunks))
        _set(cfg.rag, "confidence_cross_scale", True)
        low_on = bool(G.is_low_confidence_band(chunks))
        _set(cfg.rag, "confidence_cross_scale", False)
        rows.append({"qid": c["qid"], "kind": c.get("kind"), "live_scorable": c.get("live_scorable"),
                     "n_chunks": len(chunks), "n_rerank": n_rerank, "n_fusedonly": n_fusedonly,
                     "low_off": low_off, "low_on": low_on})
        flip = "→ANSWER" if (low_off and not low_on) else ("→REFUSE" if (low_on and not low_off) else "")
        print(f"  [{i}] {c['qid']:<28} kind={str(c.get('kind')):<8} chunks={len(chunks)} "
              f"(rerank={n_rerank}, fused-only={n_fusedonly}) low off/on={int(low_off)}/{int(low_on)} {flip}",
              flush=True)

    scored = [r for r in rows if "err" not in r]
    errs = [r for r in rows if "err" in r]
    pos = [r for r in scored if r["kind"] == "positive"]
    pos_scor = [r for r in pos if r.get("live_scorable")]
    neg = [r for r in scored if r["kind"] == "negative"]
    mixed = [r for r in scored if r["n_fusedonly"] > 0 and r["n_rerank"] > 0]
    b2a = lambda rs: [r for r in rs if r["low_off"] and not r["low_on"]]   # refuse→answer  # noqa: E731
    a2b = lambda rs: [r for r in rs if r["low_on"] and not r["low_off"]]   # answer→refuse  # noqa: E731

    print("\n" + "=" * 72)
    print("#7 GUARD-FLIP A/B (cross-scale OFF vs ON) — read-only, no generation")
    print("=" * 72)
    print(f"scored: {len(scored)} | errors: {len(errs)} | "
          f"mixed-scale (rerank+fused-only both present): {len(mixed)}")
    print(f"\nANSWERABLE positives (n={len(pos)}, scorable={len(pos_scor)}):")
    print(f"  low-conf fired OFF: {sum(r['low_off'] for r in pos)} | ON: {sum(r['low_on'] for r in pos)}")
    print(f"  → BENEFIT (refuse→answer): {len(b2a(pos))} [scorable: {len(b2a(pos_scor))}]")
    print(f"  ← new refusals (answer→refuse, want 0): {len(a2b(pos))}")
    print(f"\nUNANSWERABLE negatives (n={len(neg)}):")
    print(f"  low-conf fired OFF: {sum(r['low_off'] for r in neg)} | ON: {sum(r['low_on'] for r in neg)}")
    print(f"  ⚠ RISK (refuse→answer on negatives): {len(b2a(neg))} {[r['qid'] for r in b2a(neg)]}")
    if errs:
        print(f"\nerrors ({len(errs)}): {[r['qid'] for r in errs][:10]}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(rows, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nper-case rows → {args.out}")


if __name__ == "__main__":
    main()

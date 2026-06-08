"""A/B: hybrid baseline vs rerankers, over the same hybrid top-N candidate pool.

Two rerankers (DashScope, same provider as the stack):
  - qwen3-rerank      : TEXT-only cross-encoder  -> the DingTalk-bot / API serving path
  - qwen3-vl-rerank   : IMAGE+TEXT reranker       -> multimodal path (passes chunk image_url)
A model whose name contains "vl" runs in image-text mode: documents are
{"text": chunk_text, "image_url": <signed OSS GET url>} for image-bearing chunks.

Same candidate pool for every arm (rerankers only REORDER), isolating their value on:
  (1) ranking: recall@1/3/5/10, MRR, nDCG@10 (single-target scorable public positives)
  (2) confidence: Youden J separating answerable(pos) vs unanswerable(neg) on top-1 score.
The VL arm is additionally summarized on the image-relevant subset (pool has >=1 image).

Usage:
  python -m eval_harness.rerank_ab --goldset <json> --models qwen3-rerank,qwen3-vl-rerank \
      [--limit N] [--pool 20] [--image-only]
"""
from __future__ import annotations

import argparse
import json
import os
import time
import requests

from . import envboot  # noqa: F401
from .ha3live import install_into_retriever
from .matching import gold_doc_rank, relevance_vector
from .metrics import ranking_summary
from .recalibrate import best_separation

RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
_DOC_CHARS = 1200
_signed_cache: dict = {}


def _img_key(ch):
    if ch.get("source_image"):
        return ch["source_image"]
    for ir in (ch.get("image_refs") or []):
        if ir.get("oss_key"):
            return ir["oss_key"]
    return None


def _signed(oss_key):
    if oss_key not in _signed_cache:
        from opensearch_pipeline.oss_url import generate_signed_url
        _signed_cache[oss_key] = generate_signed_url(oss_key, expires=3600)
    return _signed_cache[oss_key]


def _doc_text(ch):
    # richest available text (image chunks carry content in visual_summary/ocr, not chunk_text)
    parts = [ch.get("chunk_text") or "", ch.get("visual_summary") or "", ch.get("ocr_text") or ""]
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        if p and p not in seen:
            seen.add(p); out.append(p)
    return " ".join(out)[:_DOC_CHARS]


def _build_docs(chunks, use_images):
    docs, n_img = [], 0
    for ch in chunks:
        text = _doc_text(ch)
        if use_images:
            k = _img_key(ch)
            url = _signed(k) if k else ""
            if url:
                docs.append({"text": text, "image_url": url}); n_img += 1
            else:
                docs.append({"text": text})
        else:
            docs.append(text)
    return docs, n_img


def rerank(query, docs, model, retries=3):
    key = os.environ.get("RAG_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    body = {"model": model, "input": {"query": query, "documents": docs},
            "parameters": {"return_documents": False, "top_n": len(docs)}}
    for a in range(retries):
        try:
            r = requests.post(RERANK_URL, headers={"Authorization": f"Bearer {key}",
                              "Content-Type": "application/json"}, json=body, timeout=60)
            r.raise_for_status()
            return r.json()["output"]["results"]
        except Exception:
            if a == retries - 1:
                raise
            time.sleep(1.2 * (a + 1))


def run(cases, models, pool=20):
    from opensearch_pipeline.retriever import search_chunks
    install_into_retriever()
    per = []
    for idx, c in enumerate(cases):
        names, ids = c.get("expected_docs", []), c.get("expected_doc_ids", [])
        perms = c.get("expected_permission") or []
        try:
            hits = search_chunks(c["query"], top_k=pool, user_dept=None)
        except Exception as e:
            per.append({"qid": c["qid"], "error": f"retrieve:{e}"[:120]}); continue
        rec = {
            "qid": c["qid"], "kind": c["kind"], "difficulty": c.get("difficulty"),
            "live_scorable": c.get("live_scorable"),
            "publicly_retrievable": all(p == "public" for p in perms),
            "n": len(hits),
            "base_rank": gold_doc_rank(hits, names, ids) if (names or ids) else None,
            "base_rel": relevance_vector(hits, names, ids) if (names or ids) else [],
            "base_top1": hits[0].get("score") if hits else None,
            "models": {},
        }
        for m in models:
            use_img = "vl" in m
            docs, n_img = _build_docs(hits, use_img)
            if use_img:
                rec["n_img_in_pool"] = n_img
                # VL can only add value when the pool has images; otherwise it would just
                # re-do the text reranker. Skip the call → VL ≡ baseline for that query,
                # and it isn't counted in VL's separation (top1=None).
                if n_img == 0:
                    rec["models"][m] = {"rank": rec["base_rank"], "rel": rec["base_rel"],
                                        "top1": None, "skipped_no_image": True}
                    continue
            try:
                res = rerank(c["query"], docs, m)
                order = [x["index"] for x in res]
                reranked = [hits[i] for i in order]
                rec["models"][m] = {
                    "rank": gold_doc_rank(reranked, names, ids) if (names or ids) else None,
                    "rel": relevance_vector(reranked, names, ids) if (names or ids) else [],
                    "top1": res[0]["relevance_score"] if res else None,
                }
            except Exception as e:
                rec["models"][m] = {"rank": rec["base_rank"], "rel": rec["base_rel"],
                                    "top1": None, "err": f"{e}"[:100]}
        per.append(rec)
        if (idx + 1) % 25 == 0:
            print(f"  ...{idx+1}/{len(cases)}", flush=True)
    return per


def _ranking(rows, rank_key, rel_key):
    return ranking_summary([r[rank_key] for r in rows], ks=(1, 3, 5, 10),
                           relevances_per_query=[r[rel_key] for r in rows], ndcg_k=10)


def summarize(per, models):
    def pos_set(rows):
        return [p for p in rows if p.get("kind") == "positive" and p.get("live_scorable")
                and p.get("publicly_retrievable") and p.get("difficulty") != "multi_doc"]
    ok = [p for p in per if "error" not in p]
    pos = pos_set(ok)
    neg = [p for p in ok if p.get("kind") == "negative"]

    base = _ranking(pos, "base_rank", "base_rel")
    base_pos1 = [p["base_top1"] for p in pos if p.get("base_rank") == 1 and p["base_top1"] is not None]
    base_neg1 = [p["base_top1"] for p in neg if p["base_top1"] is not None]

    out = {"n_positive": len(pos), "n_negative": len(neg),
           "ranking_baseline": base,
           "separation_baseline_fused": best_separation(base_pos1, base_neg1),
           "models": {}}

    # image-relevant subset (pool had >=1 image) — where the VL arm can differ
    img_pos = [p for p in pos if p.get("n_img_in_pool", 0) > 0]
    out["n_positive_image_pool"] = len(img_pos)
    if img_pos:
        out["ranking_baseline_image_subset"] = _ranking(img_pos, "base_rank", "base_rel")

    for m in models:
        rk = lambda p: p["models"][m]["rank"]      # noqa: E731
        rl = lambda p: p["models"][m]["rel"]       # noqa: E731
        mrows = [p for p in pos if m in p["models"]]
        rsum = ranking_summary([rk(p) for p in mrows], ks=(1, 3, 5, 10),
                               relevances_per_query=[rl(p) for p in mrows], ndcg_k=10)
        mpos1 = [p["models"][m]["top1"] for p in mrows
                 if p["models"][m]["rank"] == 1 and p["models"][m]["top1"] is not None]
        mneg1 = [p["models"][m]["top1"] for p in neg
                 if m in p["models"] and p["models"][m]["top1"] is not None]
        entry = {
            "ranking": rsum,
            "deltas_vs_baseline": {k: round(rsum[k] - base[k], 4) for k in
                                   ("recall@1", "recall@3", "recall@5", "recall@10", "mrr", "ndcg@10")
                                   if base.get(k) is not None and rsum.get(k) is not None},
            "separation": best_separation(mpos1, mneg1),
            "errors": [p["qid"] for p in mrows if p["models"][m].get("err")],
        }
        if img_pos and "vl" in m:
            imrows = [p for p in img_pos if m in p["models"]]
            entry["ranking_image_subset"] = ranking_summary(
                [rk(p) for p in imrows], ks=(1, 3, 5, 10),
                relevances_per_query=[rl(p) for p in imrows], ndcg_k=10)
        out["models"][m] = entry
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goldset", default=os.path.join(os.path.dirname(__file__), "goldset", "golden_full.json"))
    ap.add_argument("--models", default="qwen3-rerank,qwen3-vl-rerank")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pool", type=int, default=20)
    ap.add_argument("--image-only", action="store_true", help="only run cases whose retrieval pool has images")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "reports", "rerank_ab.json"))
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    cases = json.load(open(args.goldset, encoding="utf-8"))
    if args.limit:
        cases = cases[: args.limit]
    print(f"rerank A/B: {len(cases)} cases, pool={args.pool}, models={models}", flush=True)
    per = run(cases, models, pool=args.pool)
    summ = summarize(per, models)
    json.dump({"models": models, "summary": summ, "per_query": per},
              open(args.out, "w"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps(summ, ensure_ascii=False, indent=2))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()

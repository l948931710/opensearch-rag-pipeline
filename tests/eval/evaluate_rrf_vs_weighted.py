#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
evaluate_rrf_vs_weighted.py — RRF vs Weighted 融合策略基线对比实验

复用 weight_sweep 的评估基础设施，在同一 Clause_1000_150 chunking 上对比：
  1. 当前默认加权融合 (dense=0.5, sparse=0.2, BM25=0.3)
  2. 最优加权融合 (dense=0.4, sparse=0.2, BM25=0.4)
  3. 纯 BM25 + Vector 两路 (dense=0.7, BM25=0.3)
  4. RRF (k=60) — 二路 (BM25+Vector)
  5. RRF (k=60) — 三路 (BM25+Vector+Sparse)

输出结构化对比报告到 tests/eval/rrf_vs_weighted_report.md
"""

import os
import sys
import numpy as np
from datetime import datetime
from typing import List, Dict, Any

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))

if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from rank_bm25 import BM25Okapi  # noqa: E402
import jieba  # noqa: E402

from opensearch_pipeline.config import get_config  # noqa: E402

from evaluate_large_corpus_hybrid_sweep import (  # noqa: E402
    LARGE_EVAL_QUERIES,
    get_cached_embeddings,
    is_relevant_large,
)

# Reuse helper functions from weight_sweep
from evaluate_weight_sweep import (  # noqa: E402
    _get_dept_filter,
    _get_dept_from_doc_id,
    _get_doc_filter,
    _decompose_query,
    bootstrap_mrr_ci,
    build_chunks_clause_1000_150,
)


# ═══════════════════════════════════════════════════════════════
# RRF Fusion Implementation
# ═══════════════════════════════════════════════════════════════

def rrf_score(rank: int, k: int = 60) -> float:
    """Reciprocal Rank Fusion score: 1 / (k + rank)."""
    return 1.0 / (k + rank)


def evaluate_rrf(
    search_pool: List[Dict[str, Any]],
    all_chunks: List[Dict[str, Any]],
    bm25: BM25Okapi,
    new_query_vectors: List[List[float]],
    new_query_sparse: List[Dict],
    k: int = 60,
    use_sparse: bool = True,
) -> List[Dict[str, Any]]:
    """
    Run evaluation using RRF fusion strategy.
    
    For each query:
      1. Rank by dense vector score
      2. Rank by BM25 score
      3. (Optional) Rank by sparse vector score
      4. Fuse with RRF: score = sum(1/(k + rank_i)) for each ranker
    """
    eval_results = []

    for idx, q_info in enumerate(LARGE_EVAL_QUERIES):
        new_vec = np.array(new_query_vectors[idx])
        query_text = q_info["new_query"]
        target_doc = q_info["target_doc"]
        dept_filter = _get_dept_filter(target_doc)
        doc_filter = _get_doc_filter(dept_filter, query_text, target_doc)

        # ── 1. Sub-query decomposition ──
        sub_queries = _decompose_query(query_text)

        # ── 2. Score Computation ──
        # A. Dense Vector Scores
        filt_chunk_vectors = np.array([c["chunk_vector"] for c in search_pool])
        norms = np.linalg.norm(filt_chunk_vectors, axis=1)
        norms[norms == 0] = 1e-10
        norm_chunk_vectors = filt_chunk_vectors / norms[:, np.newaxis]
        q_norm = np.linalg.norm(new_vec)
        norm_query_vec = new_vec / q_norm if q_norm > 0 else new_vec
        vector_scores = np.dot(norm_chunk_vectors, norm_query_vec)

        # B. Sparse Scores
        q_sparse = new_query_sparse[idx] if new_query_sparse and idx < len(new_query_sparse) else None
        has_sparse = use_sparse and q_sparse and q_sparse.get("indices")
        if has_sparse:
            q_sp_map = dict(zip(q_sparse["indices"], q_sparse["values"]))
            sparse_scores = np.zeros(len(search_pool))
            for ci, c in enumerate(search_pool):
                c_sp = c.get("sparse_vector", {})
                c_sp_idx = c_sp.get("indices", [])
                c_sp_val = c_sp.get("values", [])
                dot = 0.0
                for si, sv in zip(c_sp_idx, c_sp_val):
                    if si in q_sp_map:
                        dot += sv * q_sp_map[si]
                sparse_scores[ci] = dot
        else:
            sparse_scores = np.zeros(len(search_pool))

        # C. BM25 Scores
        max_bm25_scores = np.zeros(len(search_pool))
        for sq in sub_queries:
            tokenized_sq = list(jieba.cut(sq))
            sq_bm25_scores = np.array(bm25.get_scores(tokenized_sq))
            max_bm25_scores = np.maximum(max_bm25_scores, sq_bm25_scores)

        # ── 3. RRF Ranking ──
        n = len(search_pool)
        
        # Rank each scorer (0-indexed, ascending rank = better)
        dense_order = np.argsort(-vector_scores)  # desc
        dense_ranks = np.zeros(n, dtype=int)
        for rank_pos, chunk_idx in enumerate(dense_order):
            dense_ranks[chunk_idx] = rank_pos + 1  # 1-indexed

        bm25_order = np.argsort(-max_bm25_scores)
        bm25_ranks = np.zeros(n, dtype=int)
        for rank_pos, chunk_idx in enumerate(bm25_order):
            bm25_ranks[chunk_idx] = rank_pos + 1

        # RRF fusion
        rrf_scores = np.zeros(n)
        for i in range(n):
            rrf_scores[i] = rrf_score(dense_ranks[i], k) + rrf_score(bm25_ranks[i], k)

        if has_sparse:
            sparse_order = np.argsort(-sparse_scores)
            sparse_ranks = np.zeros(n, dtype=int)
            for rank_pos, chunk_idx in enumerate(sparse_order):
                sparse_ranks[chunk_idx] = rank_pos + 1
            for i in range(n):
                rrf_scores[i] += rrf_score(sparse_ranks[i], k)

        # ── 4. Soft Filter Discounting (same as weight_sweep) ──
        final_scores = np.zeros(n)
        for i, c in enumerate(search_pool):
            c_doc = c.get("doc_id", "")
            c_dept = _get_dept_from_doc_id(c_doc)
            discount = 1.0
            if dept_filter and c_dept != dept_filter:
                discount *= 0.5
            if doc_filter and c_doc != doc_filter:
                discount *= 0.5
            final_scores[i] = rrf_scores[i] * discount

        # Fallback check
        fallback_triggered = False
        filters = q_info.get("filters", {})
        if "title_contains" in filters:
            for i, c in enumerate(search_pool):
                if filters["title_contains"] not in c.get("title", ""):
                    final_scores[i] = -1.0

        if len(final_scores) > 0 and np.max(final_scores) < 0.001:
            fallback_triggered = True
            final_scores = rrf_scores.copy()

        # ── 5. Parent Mapping + Neighbor Stitching ──
        def get_parent_id(c):
            if c.get("extra") and "parent_id" in c["extra"]:
                return c["extra"]["parent_id"]
            cid = c.get("chunk_id", "")
            if "_child_" in cid:
                return cid.split("_child_")[0]
            return cid

        is_parent_child = any(c.get("chunk_type") == "child_chunk" for c in search_pool)
        parents_dict = {}
        if is_parent_child:
            parents_pool = [c for c in all_chunks if c.get("chunk_type") != "child_chunk"]
            parents_dict = {p["chunk_id"]: p for p in parents_pool if "chunk_id" in p}

        parent_candidate_scores = {}
        for i, child_chunk in enumerate(search_pool):
            if final_scores[i] < 0:
                continue
            p_id = get_parent_id(child_chunk)
            score = float(final_scores[i])
            if is_parent_child and p_id in parents_dict:
                if p_id not in parent_candidate_scores or score > parent_candidate_scores[p_id]["score"]:
                    parent_candidate_scores[p_id] = {"chunk": parents_dict[p_id].copy(), "score": score}
            else:
                if p_id not in parent_candidate_scores or score > parent_candidate_scores[p_id]["score"]:
                    parent_candidate_scores[p_id] = {"chunk": child_chunk.copy(), "score": score}

        doc_groups = {}
        for p_id, item in parent_candidate_scores.items():
            chunk = item["chunk"]
            score = item["score"]
            doc_id = chunk.get("doc_id", "")
            if doc_id not in doc_groups:
                doc_groups[doc_id] = []
            doc_groups[doc_id].append((chunk, score))

        stitched_candidates = []
        for doc_id, items in doc_groups.items():
            items.sort(key=lambda x: x[0].get("chunk_index", 0))
            i_s = 0
            while i_s < len(items):
                current_chunk, current_score = items[i_s]
                current_chunk = current_chunk.copy()
                current_chunk["_score"] = current_score
                j_s = i_s + 1
                while j_s < len(items):
                    next_chunk, next_score = items[j_s]
                    idx1 = current_chunk.get("chunk_index", 0)
                    idx2 = next_chunk.get("chunk_index", 0)
                    if idx2 - idx1 <= 1:
                        current_chunk["chunk_text"] = current_chunk["chunk_text"] + "\n... [Contiguous] ...\n" + next_chunk["chunk_text"]
                        if current_chunk.get("raw_text") or next_chunk.get("raw_text"):
                            current_chunk["raw_text"] = (current_chunk.get("raw_text") or "") + "\n" + (next_chunk.get("raw_text") or "")
                        current_chunk["_score"] = max(current_chunk["_score"], next_score)
                        j_s += 1
                    else:
                        break
                stitched_candidates.append(current_chunk)
                i_s = j_s

        stitched_candidates.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        new_hits = stitched_candidates[:10]

        # ── Evaluate relevance ──
        new_hit_rank = 0
        for rank, chunk in enumerate(new_hits, start=1):
            if is_relevant_large(idx, chunk, strict_mode=True):
                new_hit_rank = rank
                break

        new_r1 = 1 if new_hit_rank == 1 else 0
        new_r5 = 1 if 0 < new_hit_rank <= 5 else 0
        new_mrr = 1.0 / new_hit_rank if new_hit_rank > 0 else 0.0

        top5_total = min(5, len(new_hits))
        top5_wrong = sum(1 for i in range(top5_total) if new_hits[i].get("doc_id", "") != q_info["target_doc"])
        top5_pollution = top5_wrong / max(top5_total, 1)

        if len(new_hits) >= 2:
            score_margin = new_hits[0].get("_score", 0.0) - new_hits[1].get("_score", 0.0)
        elif len(new_hits) == 1:
            score_margin = new_hits[0].get("_score", 0.0)
        else:
            score_margin = 0.0

        eval_results.append({
            "query_id": q_info["id"],
            "query": q_info["new_query"],
            "target_doc": q_info["target_doc"],
            "category": q_info["category"],
            "rank": new_hit_rank,
            "r1": new_r1,
            "r5": new_r5,
            "mrr": new_mrr,
            "top5_pollution": top5_pollution,
            "score_margin": score_margin,
            "top1_score": new_hits[0]["_score"] if new_hits else 0.0,
            "top1_doc_id": new_hits[0].get("doc_id", "") if new_hits else "",
            "fallback_triggered": fallback_triggered,
        })

    return eval_results


# ═══════════════════════════════════════════════════════════════
# Aggregate + Report
# ═══════════════════════════════════════════════════════════════

def aggregate_results(results: List[Dict], label: str) -> Dict:
    """Compute aggregate metrics for a set of per-query results."""
    n = len(results)
    categories = ["manual", "sop", "faq", "policy"]

    r1 = sum(r["r1"] for r in results) / n
    r5 = sum(r["r5"] for r in results) / n
    micro_mrr = sum(r["mrr"] for r in results) / n

    cat_mrrs = []
    for cat in categories:
        cat_res = [r for r in results if r["category"] == cat]
        if cat_res:
            cat_mrrs.append(sum(r["mrr"] for r in cat_res) / len(cat_res))
    macro_mrr = sum(cat_mrrs) / len(cat_mrrs) if cat_mrrs else 0.0

    avg_t5p = sum(r["top5_pollution"] for r in results) / n
    margins = [r["score_margin"] for r in results]
    margin_min = min(margins)
    margin_mean = float(np.mean(margins))

    per_q_mrrs = [r["mrr"] for r in results]
    boot_mean, boot_lo, boot_hi = bootstrap_mrr_ci(per_q_mrrs)

    fallback_count = sum(1 for r in results if r["fallback_triggered"])

    # Failed queries
    failed = [r for r in results if r["r1"] == 0]

    return {
        "label": label,
        "r1": r1,
        "r5": r5,
        "micro_mrr": micro_mrr,
        "macro_mrr": macro_mrr,
        "avg_t5p": avg_t5p,
        "margin_min": margin_min,
        "margin_mean": margin_mean,
        "boot_lo": boot_lo,
        "boot_hi": boot_hi,
        "fallback_count": fallback_count,
        "failed_queries": failed,
        "results": results,
    }


def generate_report(all_aggs: List[Dict], report_path: str):
    """Generate structured markdown report."""
    n_queries = len(LARGE_EVAL_QUERIES)
    categories = ["manual", "sop", "faq", "policy"]

    lines = [
        "# RRF vs Weighted Fusion Baseline Comparison Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "**Chunk Config:** Clause_1000_150 (locked)",
        f"**Query Count:** {n_queries}",
        "**RRF Constant (k):** 60",
        "",
        "---",
        "",
        "## 1. Overall Comparison",
        "",
        "| Strategy | R@1 | R@5 | Micro MRR | **Macro MRR** | Boot 95% CI | Margin Min | Margin Mean | Top5 Poll | Fallback |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    for agg in all_aggs:
        lines.append(
            f"| **{agg['label']}** | {agg['r1']:.2%} | {agg['r5']:.2%} "
            f"| {agg['micro_mrr']:.4f} | **{agg['macro_mrr']:.4f}** "
            f"| [{agg['boot_lo']:.4f}, {agg['boot_hi']:.4f}] "
            f"| {agg['margin_min']:.4f} | {agg['margin_mean']:.4f} "
            f"| {agg['avg_t5p']:.2%} | {agg['fallback_count']} |"
        )

    # ── 2. Per-Category Breakdown ──
    lines.extend(["", "---", "", "## 2. Per-Category MRR Breakdown", ""])

    header = "| Strategy |"
    sep = "| :--- |"
    for cat in categories:
        header += f" {cat} MRR |"
        sep += " :---: |"
    lines.append(header)
    lines.append(sep)

    for agg in all_aggs:
        row = f"| **{agg['label']}** |"
        for cat in categories:
            cat_res = [r for r in agg["results"] if r["category"] == cat]
            if cat_res:
                cat_mrr = sum(r["mrr"] for r in cat_res) / len(cat_res)
                row += f" {cat_mrr:.4f} |"
            else:
                row += " — |"
        lines.append(row)

    # ── 3. Failed Queries Analysis ──
    lines.extend(["", "---", "", "## 3. Failed Queries Analysis (R@1 ≠ 1)", ""])

    any_failure = False
    for agg in all_aggs:
        fails = [r for r in agg["results"] if r["r1"] == 0]
        if fails:
            any_failure = True
            lines.append(f"### {agg['label']} — {len(fails)} failures")
            lines.append("")
            lines.append("| Query ID | Category | Query | Target Doc | Rank | Top-1 Doc |")
            lines.append("| :---: | :---: | :--- | :--- | :---: | :--- |")
            for f in fails:
                lines.append(
                    f"| {f['query_id']} | {f['category']} | {f['query'][:40]}... "
                    f"| {f['target_doc']} | {f['rank']} | {f['top1_doc_id']} |"
                )
            lines.append("")

    if not any_failure:
        lines.append("✅ No failures across all strategies — all queries achieved R@1 = 1.")

    # ── 4. RRF vs Weighted Decision Summary ──
    lines.extend(["", "---", "", "## 4. Decision Summary", ""])

    rrf_aggs = [a for a in all_aggs if "RRF" in a["label"]]
    weighted_aggs = [a for a in all_aggs if "RRF" not in a["label"]]

    best_rrf = max(rrf_aggs, key=lambda a: (a["macro_mrr"], a["margin_mean"])) if rrf_aggs else None
    best_weighted = max(weighted_aggs, key=lambda a: (a["macro_mrr"], a["margin_mean"])) if weighted_aggs else None

    if best_rrf and best_weighted:
        delta_mrr = best_rrf["macro_mrr"] - best_weighted["macro_mrr"]
        delta_r1 = best_rrf["r1"] - best_weighted["r1"]

        lines.append(f"- **Best RRF:** {best_rrf['label']} (Macro MRR={best_rrf['macro_mrr']:.4f}, R@1={best_rrf['r1']:.2%})")
        lines.append(f"- **Best Weighted:** {best_weighted['label']} (Macro MRR={best_weighted['macro_mrr']:.4f}, R@1={best_weighted['r1']:.2%})")
        lines.append(f"- **Delta:** Macro MRR={delta_mrr:+.4f}, R@1={delta_r1:+.2%}")
        lines.append("")

        if abs(delta_mrr) < 0.005 and abs(delta_r1) < 0.02:
            lines.append("> [!NOTE]")
            lines.append("> RRF and weighted fusion perform **equivalently** on this evaluation set.")
            lines.append("> Retaining the current weighted fusion default is reasonable.")
        elif delta_mrr > 0.005:
            lines.append("> [!TIP]")
            lines.append("> RRF **outperforms** weighted fusion. Consider switching the production default to RRF.")
        else:
            lines.append("> [!NOTE]")
            lines.append("> Weighted fusion **outperforms** RRF on this evaluation set.")

    lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n✅ Report generated: {report_path}")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("   RRF vs Weighted Fusion Baseline Comparison")
    print("=" * 60)

    config = get_config()

    # ── Step 1: Build chunks ──
    print("\n>>> Step 1: Building chunks (Clause_1000_150)...")
    all_chunks, emb_cache = build_chunks_clause_1000_150(config)

    # ── Step 2: Build search pool and BM25 index ──
    print("\n>>> Step 2: Building search pool and BM25 index...")
    search_pool = [c for c in all_chunks if c.get("chunk_type") == "child_chunk" or
                   not any(cc.get("chunk_type") == "child_chunk" for cc in all_chunks)]

    # Match weight_sweep logic: child_chunks + standalone chunks (FAQ/Policy) without children
    has_children = any(c.get("chunk_type") == "child_chunk" for c in all_chunks)
    if has_children:
        def _get_parent_id(c):
            if c.get("extra") and "parent_id" in c["extra"]:
                return c["extra"]["parent_id"]
            cid = c.get("chunk_id", "")
            if "_child_" in cid:
                return cid.split("_child_")[0]
            return cid
        child_parent_ids = {_get_parent_id(c) for c in all_chunks if c.get("chunk_type") == "child_chunk"}
        search_pool = [
            c for c in all_chunks
            if c.get("chunk_type") == "child_chunk" or _get_parent_id(c) not in child_parent_ids
        ]
    else:
        search_pool = all_chunks

    tokenized_corpus = [list(jieba.cut(c["chunk_text"])) for c in search_pool]
    bm25 = BM25Okapi(tokenized_corpus)
    print(f"    └─ Search pool size: {len(search_pool)}")

    # ── Step 3: Load query embeddings ──
    print("\n>>> Step 3: Loading query embeddings...")
    query_texts = [q["new_query"] for q in LARGE_EVAL_QUERIES]
    query_vectors, query_sparse = get_cached_embeddings(query_texts, emb_cache, config)
    print(f"    └─ Query vectors: {len(query_vectors)}")

    # ── Step 4: Run all strategies ──
    print("\n>>> Step 4: Running fusion strategy evaluations...")

    # Import the weighted evaluator from weight_sweep
    from evaluate_weight_sweep import evaluate_single_weight

    strategies = []

    # Strategy 1: Current Default Weighted (0.5/0.2/0.3)
    print("    [1/5] Weighted: Default (0.5/0.2/0.3)")
    res_default = evaluate_single_weight(
        search_pool, all_chunks, bm25, query_vectors, query_sparse,
        w_dense=0.5, w_sparse=0.2, w_bm25=0.3, is_3way=True,
    )
    strategies.append(aggregate_results(res_default, "Weighted 3-Way Default (D0.5/S0.2/B0.3)"))

    # Strategy 2: Optimal Weighted (0.4/0.2/0.4)
    print("    [2/5] Weighted: Optimal (0.4/0.2/0.4)")
    res_optimal = evaluate_single_weight(
        search_pool, all_chunks, bm25, query_vectors, query_sparse,
        w_dense=0.4, w_sparse=0.2, w_bm25=0.4, is_3way=True,
    )
    strategies.append(aggregate_results(res_optimal, "Weighted 3-Way Optimal (D0.4/S0.2/B0.4)"))

    # Strategy 3: 2-Way Default (0.7/0.3)
    print("    [3/5] Weighted: 2-Way (0.7/0.3)")
    res_2way = evaluate_single_weight(
        search_pool, all_chunks, bm25, query_vectors, query_sparse,
        w_dense=0.7, w_sparse=0.0, w_bm25=0.3, is_3way=False,
    )
    strategies.append(aggregate_results(res_2way, "Weighted 2-Way (D0.7/B0.3)"))

    # Strategy 4: RRF 2-Way (Dense + BM25, k=60)
    print("    [4/5] RRF: 2-Way (Dense + BM25, k=60)")
    res_rrf_2way = evaluate_rrf(
        search_pool, all_chunks, bm25, query_vectors, query_sparse,
        k=60, use_sparse=False,
    )
    strategies.append(aggregate_results(res_rrf_2way, "RRF 2-Way (D+B, k=60)"))

    # Strategy 5: RRF 3-Way (Dense + Sparse + BM25, k=60)
    print("    [5/5] RRF: 3-Way (Dense + Sparse + BM25, k=60)")
    res_rrf_3way = evaluate_rrf(
        search_pool, all_chunks, bm25, query_vectors, query_sparse,
        k=60, use_sparse=True,
    )
    strategies.append(aggregate_results(res_rrf_3way, "RRF 3-Way (D+S+B, k=60)"))

    # ── Step 5: Print Summary ──
    print("\n" + "=" * 60)
    print("   RESULTS SUMMARY")
    print("=" * 60)
    print(f"{'Strategy':<45} {'R@1':>8} {'R@5':>8} {'Macro MRR':>10} {'Margin':>8}")
    print("-" * 80)
    for s in strategies:
        print(f"{s['label']:<45} {s['r1']:>7.2%} {s['r5']:>7.2%} {s['macro_mrr']:>10.4f} {s['margin_mean']:>8.4f}")

    # ── Step 6: Generate Report ──
    report_path = os.path.join(_SCRIPT_DIR, "rrf_vs_weighted_report.md")
    generate_report(strategies, report_path)


if __name__ == "__main__":
    main()

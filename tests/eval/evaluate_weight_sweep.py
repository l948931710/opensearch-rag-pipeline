# -*- coding: utf-8 -*-
"""
evaluate_weight_sweep.py — 混合检索权重网格搜索实验

锁定 Chunk 配置为 Clause_1000_150（当前最优），仅 sweep dense/sparse/BM25 融合权重。
三路融合（dense + sparse + BM25 = 1.0）步长 0.1，共 66 组。
两路 fallback（dense + BM25 = 1.0，无 sparse）步长 0.1，共 11 组。

Review Findings 集成：
  1. 记录每个 (query, weight) 是否触发 L948 fallback（混杂因素跟踪）
  2. 输出 Score Margin 的 min/P25/median/mean 分布
  3. Bootstrap CI（1000 次有放回抽样）防止过拟合 47 条样本
  4. 报告只展示 Top-10 和 Bottom-10，避免冗长
"""

import os
import sys
import json
import re
import numpy as np
from datetime import datetime
from typing import List, Dict, Any, Tuple
from rank_bm25 import BM25Okapi
import jieba

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT_EARLY = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))

# Add project root for opensearch_pipeline imports
if _PROJECT_ROOT_EARLY not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT_EARLY)
# Add script dir for sibling module import
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from opensearch_pipeline.config import get_config  # noqa: E402
from opensearch_pipeline.chunker import DocumentChunker  # noqa: E402

# ─── 复用 hybrid_sweep 的共享基础设施 (同目录直接 import) ───
from evaluate_large_corpus_hybrid_sweep import (  # noqa: E402
    LARGE_EVAL_QUERIES,
    load_embedding_cache,
    save_embedding_cache,
    get_cached_embeddings,
    is_relevant_large,
)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ─── Weight Grid Generation ───

def generate_3way_grid(step: float = 0.1) -> List[Tuple[float, float, float]]:
    """Generate all (w_dense, w_sparse, w_bm25) where sum=1.0, step=0.1."""
    combos = []
    steps = int(round(1.0 / step)) + 1
    for i in range(steps):
        w_dense = round(i * step, 2)
        for j in range(steps - i):
            w_sparse = round(j * step, 2)
            w_bm25 = round(1.0 - w_dense - w_sparse, 2)
            if w_bm25 >= -1e-9:  # floating point guard
                combos.append((w_dense, max(0.0, w_sparse), max(0.0, w_bm25)))
    return combos


def generate_2way_grid(step: float = 0.1) -> List[Tuple[float, float]]:
    """Generate all (w_dense, w_bm25) where sum=1.0."""
    combos = []
    steps = int(round(1.0 / step)) + 1
    for i in range(steps):
        w_dense = round(i * step, 2)
        w_bm25 = round(1.0 - w_dense, 2)
        combos.append((w_dense, w_bm25))
    return combos


# ─── Core Evaluation with Weight Parameterization ───

def evaluate_single_weight(
    search_pool: List[Dict[str, Any]],
    all_chunks: List[Dict[str, Any]],
    bm25: BM25Okapi,
    new_query_vectors: List[List[float]],
    new_query_sparse: List[Dict],
    w_dense: float,
    w_sparse: float,
    w_bm25: float,
    is_3way: bool = True,
) -> List[Dict[str, Any]]:
    """
    Run evaluation for a single weight combination.
    Returns per-query results including score margin, fallback trigger, etc.
    """
    eval_results = []

    for idx, q_info in enumerate(LARGE_EVAL_QUERIES):
        new_vec = np.array(new_query_vectors[idx])
        query_text = q_info["new_query"]

        target_doc = q_info["target_doc"]
        dept_filter = _get_dept_filter(target_doc)
        doc_filter = _get_doc_filter(dept_filter, query_text, target_doc)

        # ─── 1. Query Decomposition ───
        sub_queries = _decompose_query(query_text)

        # ─── 2. Score Computation ───
        # A. Dense Vector Scores
        filt_chunk_vectors = np.array([c["chunk_vector"] for c in search_pool])
        norms = np.linalg.norm(filt_chunk_vectors, axis=1)
        norms[norms == 0] = 1e-10
        norm_chunk_vectors = filt_chunk_vectors / norms[:, np.newaxis]
        norm_query_vec = new_vec / np.linalg.norm(new_vec)
        vector_scores = np.dot(norm_chunk_vectors, norm_query_vec)

        # B. Sparse Vector Scores
        q_sparse = new_query_sparse[idx] if new_query_sparse and idx < len(new_query_sparse) else None
        has_sparse = q_sparse and q_sparse.get("indices")
        if has_sparse and is_3way:
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

        # Normalize (per-query min-max)
        def normalize(scores):
            min_s, max_s = np.min(scores), np.max(scores)
            if max_s - min_s == 0:
                return np.zeros_like(scores)
            return (scores - min_s) / (max_s - min_s)

        norm_vector = normalize(vector_scores)
        norm_bm25 = normalize(max_bm25_scores)
        norm_sparse = normalize(sparse_scores) if (has_sparse and is_3way) else np.zeros_like(norm_vector)

        # Hybrid Fusion with parameterized weights
        if is_3way:
            hybrid_scores = w_dense * norm_vector + w_sparse * norm_sparse + w_bm25 * norm_bm25
        else:
            hybrid_scores = w_dense * norm_vector + w_bm25 * norm_bm25

        # ─── 3. Soft Filter Discounting ───
        final_scores = np.zeros(len(search_pool))
        for i, c in enumerate(search_pool):
            c_doc = c.get("doc_id", "")
            c_dept = _get_dept_from_doc_id(c_doc)
            discount = 1.0
            if dept_filter and c_dept != dept_filter:
                discount *= 0.5
            if doc_filter and c_doc != doc_filter:
                discount *= 0.5
            final_scores[i] = hybrid_scores[i] * discount

        # ─── Review Finding #1: Track fallback trigger ───
        fallback_triggered = False
        filters = q_info.get("filters", {})
        if "title_contains" in filters:
            for i, c in enumerate(search_pool):
                if filters["title_contains"] not in c.get("title", ""):
                    final_scores[i] = -1.0

        if len(final_scores) > 0 and np.max(final_scores) < 0.35:
            fallback_triggered = True
            final_scores = hybrid_scores.copy()
            if "title_contains" in filters:
                for i, c in enumerate(search_pool):
                    if filters["title_contains"] not in c.get("title", ""):
                        final_scores[i] = -1.0

        # ─── 4. Parent Mapping ───
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
            # Build parents_dict from ALL chunks (not search_pool which excludes parents)
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

        # ─── 5. Neighbor Stitching ───
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
            i = 0
            while i < len(items):
                current_chunk, current_score = items[i]
                current_chunk = current_chunk.copy()
                current_chunk["_score"] = current_score
                j = i + 1
                while j < len(items):
                    next_chunk, next_score = items[j]
                    idx1 = current_chunk.get("chunk_index", 0)
                    idx2 = next_chunk.get("chunk_index", 0)
                    if idx2 - idx1 <= 1:
                        current_chunk["chunk_text"] = current_chunk["chunk_text"] + "\n... [Contiguous] ...\n" + next_chunk["chunk_text"]
                        if current_chunk.get("raw_text") or next_chunk.get("raw_text"):
                            current_chunk["raw_text"] = (current_chunk.get("raw_text") or "") + "\n" + (next_chunk.get("raw_text") or "")
                        current_chunk["_score"] = max(current_chunk["_score"], next_score)
                        j += 1
                    else:
                        break
                stitched_candidates.append(current_chunk)
                i = j

        stitched_candidates.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        new_hits = stitched_candidates[:10]

        # ─── Evaluate relevance ───
        new_hit_rank = 0
        for rank, chunk in enumerate(new_hits, start=1):
            if is_relevant_large(idx, chunk, strict_mode=True):
                new_hit_rank = rank
                break

        new_r1 = 1 if new_hit_rank == 1 else 0
        new_r5 = 1 if 0 < new_hit_rank <= 5 else 0
        new_mrr = 1.0 / new_hit_rank if new_hit_rank > 0 else 0.0

        # Top-5 pollution
        top5_total = min(5, len(new_hits))
        top5_wrong = sum(1 for i in range(top5_total) if new_hits[i].get("doc_id", "") != q_info["target_doc"])
        top5_pollution = top5_wrong / max(top5_total, 1)

        # ─── Review Finding #2: Score Margin ───
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


# ─── Helper Functions (extracted from hybrid_sweep) ───

def _get_dept_filter(target_doc: str):
    if target_doc.startswith("eval_it_"):
        return "it"
    elif target_doc.startswith("eval_prod_"):
        return "production"
    elif target_doc.startswith("eval_admin_"):
        return "admin"
    elif target_doc.startswith("eval_hr_"):
        return "hr"
    elif target_doc == "eval_company_faq":
        return "admin"
    return None


def _get_dept_from_doc_id(doc_id: str):
    if doc_id.startswith("eval_it_"):
        return "it"
    elif doc_id.startswith("eval_prod_"):
        return "production"
    elif doc_id.startswith("eval_admin_"):
        return "admin"
    elif doc_id.startswith("eval_hr_"):
        return "hr"
    elif doc_id == "eval_company_faq":
        return "admin"
    return None


def _get_doc_filter(dept_filter, query_text, target_doc):
    """Refined business intent routing (same logic as hybrid_sweep)."""
    doc_filter = None
    if dept_filter == "it":
        if any(w in query_text for w in ["海外发票", "发票系统", "发票出库", "发票入库", "参照生单"]):
            doc_filter = "eval_it_invoice_system"
        elif any(w in query_text for w in ["wifi", "wi-fi", "无线", "打印机", "卡纸", "内线", "分机", "系统管理员", "电话"]):
            doc_filter = "eval_it_faq"
        elif any(w in query_text for w in ["成品仓库", "成品仓", "销售出库", "PDA", "扫码枪", "出库单", "条码", "扫码", "检验单", "入库单"]):
            doc_filter = "eval_it_warehouse_u8"
        elif any(w in query_text for w in ["五金仓", "材料及五金仓", "限额领料", "非限额", "五金", "限额", "仓库人员", "系统领用量", "领用量", "出库类别", "超额领料"]):
            doc_filter = "eval_it_wujin_u8"
        elif any(w in query_text for w in ["车间生产", "车间", "看板", "生产订单"]):
            doc_filter = "eval_it_chejian_u8"
        elif any(w in query_text for w in ["工价", "工资核算", "成品工价单"]):
            doc_filter = "eval_it_payroll_manual"
        elif any(w in query_text for w in ["英特尔", "针脚", "lga", "压杆", "处理器", "cpu"]):
            doc_filter = "eval_it_pc_install"
        elif any(w in query_text for w in ["财务部", "凭证", "付款单据", "普通发票", "专用发票"]):
            doc_filter = "eval_it_finance_u8"
        elif any(w in query_text for w in ["入职登记", "重新入职", "卡号", "考勤排班"]):
            doc_filter = "eval_it_hr_u8"
    elif dept_filter == "production":
        if "入库" in query_text:
            doc_filter = "eval_prod_xisu_ruku"
        elif "交货" in query_text:
            doc_filter = "eval_prod_xisu_jiaohuo"
        elif "领料" in query_text:
            doc_filter = "eval_prod_xisu_lingliao"
        elif "数量本" in query_text:
            doc_filter = "eval_prod_xisu_shuliang"
        elif any(w in query_text for w in ["测水", "吸管孔", "粘贴胶带", "盖塞"]):
            doc_filter = "eval_prod_naichabei"
        elif any(w in query_text for w in ["纸吸管", "耐热", "耐高温"]):
            doc_filter = "eval_prod_xiguan_receshi"
        elif any(w in query_text for w in ["发帽", "手机", "拍照", "酒后", "车间上班", "进入车间"]):
            doc_filter = "eval_prod_newcomer"
        elif any(w in query_text for w in ["储物柜", "更衣室"]):
            doc_filter = "eval_prod_locker"
        elif any(w in query_text for w in ["注塑机", "螺杆", "润滑油", "液压油"]):
            doc_filter = "eval_prod_injection"
    elif dept_filter == "hr":
        if any(w in query_text for w in ["请假", "考勤", "旷工", "缺勤"]):
            doc_filter = "eval_hr_attendance"
        elif any(w in query_text for w in ["安全隐患", "举报", "奖励", "报告"]):
            doc_filter = "eval_hr_safety_report"
        else:
            doc_filter = "eval_hr_manual"
    elif dept_filter == "admin":
        if any(w in query_text for w in ["采购", "招投标", "采购部", "行政部"]):
            doc_filter = "eval_admin_procurement"
        elif any(w in query_text for w in ["宿舍", "搬", "做饭", "留宿", "电线", "迁离"]):
            doc_filter = "eval_admin_dormitory"
        else:
            doc_filter = "eval_company_faq"
    return doc_filter


def _decompose_query(query_text: str) -> List[str]:
    """Query decomposition + keyword expansion (same as hybrid_sweep)."""
    delimiters = [r"？", r"。", r"；", r"\?", r"\.", r";"]
    pattern = "|".join(delimiters)
    sub_queries = [q.strip() for q in re.split(pattern, query_text) if q.strip()]
    if not sub_queries:
        sub_queries = [query_text]

    expanded = []
    for sq in sub_queries:
        expanded.append(sq)
        sq_lower = sq.lower()
        if "wifi" in sq_lower or "无线" in sq:
            expanded.append("Wi-Fi 无线网络 密码 WiFi")
        if "入库" in sq:
            expanded.append("产品入库单 打印 仓管")
        if "领料" in sq:
            expanded.append("领料单 辅料工 纸箱仓管")
        if "交货" in sq:
            expanded.append("吸塑交货单 打印 包材")
        if "工价" in sq:
            expanded.append("半成品工价单 成品工价单")
        if "卡纸" in sq:
            expanded.append("打印机 卡纸 IT部 8088")
        if "年休假" in sq or "转正" in sq:
            expanded.append("带薪年休假 试用小结")
    return list(set(expanded))


# ─── Bootstrap Confidence Interval (Review Finding #3) ───

def bootstrap_mrr_ci(per_query_mrrs: List[float], n_bootstrap: int = 1000, ci: float = 0.95) -> Tuple[float, float, float]:
    """
    Bootstrap MRR mean with confidence interval.
    Returns (mean, ci_lower, ci_upper).
    """
    rng = np.random.RandomState(42)
    n = len(per_query_mrrs)
    arr = np.array(per_query_mrrs)
    boot_means = []
    for _ in range(n_bootstrap):
        sample = rng.choice(arr, size=n, replace=True)
        boot_means.append(np.mean(sample))
    boot_means = np.array(boot_means)
    alpha = (1.0 - ci) / 2.0
    return float(np.mean(arr)), float(np.percentile(boot_means, alpha * 100)), float(np.percentile(boot_means, (1 - alpha) * 100))


# ─── Chunk Generation (locked to Clause_1000_150) ───

def build_chunks_clause_1000_150(config) -> Tuple[List[Dict[str, Any]], Dict]:
    """Build all chunks using Clause_1000_150 config. Returns (chunks, embedding_cache)."""
    from opensearch_pipeline.extraction.docx_extractor import extract_docx

    base_dir = "/Users/laijunchen/fuling_raw_for_chunk_test"
    chunk_exp_dir = os.path.join(_PROJECT_ROOT, "fuling_chunk_exp")

    raw_tasks = [
        {"doc_id": "eval_it_finance_u8", "local_path": os.path.join(base_dir, "it/富岭U8+财务部操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_hr_u8", "local_path": os.path.join(base_dir, "it/富岭U8+人事部操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_wujin_u8", "local_path": os.path.join(base_dir, "it/富岭U8+材料及五金仓操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_trade_u8", "local_path": os.path.join(base_dir, "it/富岭U8+贸易部操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_chejian_u8", "local_path": os.path.join(base_dir, "it/富岭U8+车间操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_warehouse_u8", "local_path": os.path.join(base_dir, "it/富岭U8+成品仓库操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_it_payroll_manual", "local_path": os.path.join(base_dir, "it/工资核算管理操作手册（2025年5月28日初版）.docx"), "category": "manual"},
        {"doc_id": "eval_it_pc_install", "local_path": os.path.join(base_dir, "it/FL-CW-XXH-003-《电脑安装》作业指导书.pdf"), "category": "manual"},
        {"doc_id": "eval_it_invoice_system", "local_path": os.path.join(base_dir, "it/海外发票系统操作手册.docx"), "category": "manual"},
        {"doc_id": "eval_prod_naichabei", "local_path": os.path.join(base_dir, "production/FL-ZS-WI-002-奶茶杯与杯盖装配（测漏水）作业指导书.pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xisu_shuliang", "local_path": os.path.join(base_dir, "production/FL-XS-WI-001吸塑《数量本》填写作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xisu_lingliao", "local_path": os.path.join(base_dir, "production/FL-XS-WI-005吸塑《领料单》开立作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xisu_jiaohuo", "local_path": os.path.join(base_dir, "production/FL-XS-WI-006《吸塑交货单》打印作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xiguan_receshi", "local_path": os.path.join(base_dir, "production/FL-XG-WI-008纸吸管耐高温测试作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_prod_xisu_ruku", "local_path": os.path.join(base_dir, "production/FL-XS-WI-009《吸塑-产品入库单》打印作业指导书(1).pdf"), "category": "manual"},
        {"doc_id": "eval_hr_manual", "local_path": os.path.join(base_dir, "hr/FL-HR-003《员工手册》2023年4月第三版.pdf"), "category": "sop"},
        {"doc_id": "eval_it_faq", "local_path": os.path.join(base_dir, "it/IT常见问题解答.docx"), "category": "faq"},
        {"doc_id": "eval_company_faq", "local_path": os.path.join(base_dir, "hr/新员工入职常见问题FAQ.docx"), "category": "faq"},
        # Policy docs
        {"doc_id": "eval_admin_procurement", "local_path": os.path.join(chunk_exp_dir, "admin_FL-AD-012《公司采购与招投标管理制度》.docx"), "category": "policy"},
        {"doc_id": "eval_hr_attendance", "local_path": os.path.join(chunk_exp_dir, "hr_FL-HR-005《员工考勤与请休假管理规定》.docx"), "category": "policy"},
        {"doc_id": "eval_admin_dormitory", "local_path": os.path.join(chunk_exp_dir, "admin_宿舍管理制度.docx"), "category": "policy"},
        {"doc_id": "eval_hr_safety_report", "local_path": os.path.join(chunk_exp_dir, "hr_A09安全隐患报告和举报奖励制度.docx"), "category": "policy"},
        # SOP docs
        {"doc_id": "eval_prod_newcomer", "local_path": os.path.join(chunk_exp_dir, "production_注塑事业部_新进员工告知书.docx"), "category": "sop"},
        {"doc_id": "eval_prod_locker", "local_path": os.path.join(chunk_exp_dir, "production_注塑事业部_更衣室使用规范.docx"), "category": "sop"},
        {"doc_id": "eval_prod_injection", "local_path": os.path.join(chunk_exp_dir, "production_注塑事业部_FL-ZS-WI-015《注塑机日常保养规范》.docx"), "category": "sop"},
    ]

    # Locked sweep config: Clause_1000_150
    sw = {"manual": (600, 100), "sop": (600, 100), "faq": (600, 100), "clause": (1000, 150)}

    emb_cache = load_embedding_cache()
    all_chunks = []

    for task in raw_tasks:
        doc_id = task["doc_id"]
        cat_l1 = task.get("category", "manual")

        if doc_id.startswith("eval_it_"):
            dept = "it"
        elif doc_id.startswith("eval_prod_"):
            dept = "production"
        elif doc_id.startswith("eval_admin_"):
            dept = "admin"
        elif doc_id.startswith("eval_hr_"):
            dept = "hr"
        elif doc_id == "eval_company_faq":
            dept = "admin"
        else:
            dept = task["local_path"].split("/")[-2]

        canon_file = os.path.join(_PROJECT_ROOT, "processing/canonical", dept, doc_id, "v1", "content.canonical.json")

        if os.path.exists(canon_file):
            with open(canon_file, "r") as f:
                doc_data = json.load(f)
            blocks = doc_data.get("blocks", [])
            doc_title = doc_data.get("title", "")
        else:
            local_path = task["local_path"]
            if not os.path.exists(local_path):
                print(f"    ⚠️ Skipping {doc_id}: no canonical and no local file at {local_path}")
                continue
            print(f"    📄 {doc_id}: Loading blocks directly from docx (no canonical)")
            raw_blocks, warnings = extract_docx(local_path)
            doc_title = os.path.basename(local_path).replace(".docx", "")
            blocks = []
            for i, rb in enumerate(raw_blocks):
                blocks.append({
                    "block_id": str(i + 1),
                    "block_type": rb.block_type,
                    "text": rb.text,
                    "page_num": getattr(rb, "page_num", 1) or 1,
                    "section_path": rb.section_path,
                    "source": rb.source or "native"
                })

        for b in blocks:
            b["block_id"] = b.get("block_id", "1")

        # Determine chunk mode
        if cat_l1 == "faq":
            m_chunk, m_overlap, m_mode = sw["faq"][0], sw["faq"][1], "faq"
        elif cat_l1 == "policy":
            m_chunk, m_overlap, m_mode = sw["clause"][0], sw["clause"][1], "clause"
        elif cat_l1 == "manual":
            m_chunk, m_overlap, m_mode = sw["manual"][0], sw["manual"][1], "text"
        else:
            m_chunk, m_overlap, m_mode = sw["sop"][0], sw["sop"][1], "text"

        chunker = DocumentChunker(
            max_chunk_chars=m_chunk,
            min_chunk_chars=10,
            overlap_chars=m_overlap,
            split_mode=m_mode,
            prepend_dept=False,
            prepend_title=True,
            prepend_section=True,
            prepend_for_faq=False,
            max_context_chars=100,
            max_context_ratio=0.3,
            parent_child=True
        )

        owner_dept = "it" if "it" in doc_id else ("hr" if "hr" in doc_id else ("production" if "prod" in doc_id else "admin"))
        metadata = {"title": doc_title, "owner_dept": owner_dept, "category_l1": cat_l1}
        chunks = chunker.chunk_from_blocks(blocks=blocks, doc_id=doc_id, version_no=1, metadata=metadata)

        texts_to_embed = [c.chunk_text for c in chunks]
        vectors, sparse_vecs = get_cached_embeddings(texts_to_embed, emb_cache, config)

        for i, c in enumerate(chunks):
            c_dict = {
                "chunk_id": c.chunk_id,
                "chunk_index": c.chunk_index,
                "doc_id": c.doc_id,
                "title": getattr(c, "title", "") or doc_title,
                "chunk_text": c.chunk_text,
                "chunk_type": c.chunk_type,
                "section_title": c.section_title,
                "raw_text": c.raw_text,
                "context_prefix": c.context_prefix,
                "chunk_vector": vectors[i],
                "sparse_vector": sparse_vecs[i] if i < len(sparse_vecs) else {"indices": [], "values": []},
                "extra": getattr(c, "extra", {})
            }
            all_chunks.append(c_dict)

    save_embedding_cache(emb_cache)
    print(f"    └─ Total chunks (Clause_1000_150): {len(all_chunks)}")
    return all_chunks, emb_cache


# ─── Report Generation ───

def generate_report(
    all_3way_results: List[Dict],
    all_2way_results: List[Dict],
    report_path: str,
):
    """Generate weight_sweep_report.md with Top/Bottom leaderboard, sensitivity analysis, and bootstrap CI."""

    categories = ["manual", "sop", "faq", "policy"]

    # ─── Compute aggregate metrics for each weight combo ───
    def aggregate(sweep_results: List[Dict]) -> List[Dict]:
        aggregated = []
        for sw in sweep_results:
            results = sw["results"]
            n = len(results)
            r1 = sum(r["r1"] for r in results) / n
            r5 = sum(r["r5"] for r in results) / n
            micro_mrr = sum(r["mrr"] for r in results) / n

            # Macro MRR (equal weight per category)
            cat_mrrs = []
            for cat in categories:
                cat_res = [r for r in results if r["category"] == cat]
                if cat_res:
                    cat_mrrs.append(sum(r["mrr"] for r in cat_res) / len(cat_res))
            macro_mrr = sum(cat_mrrs) / len(cat_mrrs) if cat_mrrs else 0.0

            # Top-5 pollution
            avg_t5p = sum(r["top5_pollution"] for r in results) / n

            # Score margin stats (Review Finding #2)
            margins = [r["score_margin"] for r in results]
            margin_min = min(margins) if margins else 0.0
            margin_p25 = float(np.percentile(margins, 25)) if margins else 0.0
            margin_median = float(np.percentile(margins, 50)) if margins else 0.0
            margin_mean = float(np.mean(margins)) if margins else 0.0

            # Fallback trigger count (Review Finding #1)
            fallback_count = sum(1 for r in results if r["fallback_triggered"])

            # Bootstrap CI (Review Finding #3)
            per_q_mrrs = [r["mrr"] for r in results]
            boot_mean, boot_lo, boot_hi = bootstrap_mrr_ci(per_q_mrrs)

            # R@1 = 1.0 durability: how many queries maintain R@1
            r1_count = sum(r["r1"] for r in results)

            aggregated.append({
                "label": sw["label"],
                "w_dense": sw.get("w_dense", 0),
                "w_sparse": sw.get("w_sparse", 0),
                "w_bm25": sw.get("w_bm25", 0),
                "r1": r1,
                "r5": r5,
                "micro_mrr": micro_mrr,
                "macro_mrr": macro_mrr,
                "avg_t5p": avg_t5p,
                "margin_min": margin_min,
                "margin_p25": margin_p25,
                "margin_median": margin_median,
                "margin_mean": margin_mean,
                "fallback_count": fallback_count,
                "boot_mean": boot_mean,
                "boot_lo": boot_lo,
                "boot_hi": boot_hi,
                "r1_count": r1_count,
                "results": results,
            })
        # Sort by macro_mrr desc, then margin_mean desc, then avg_t5p asc
        aggregated.sort(key=lambda x: (x["macro_mrr"], x["margin_mean"], -x["avg_t5p"]), reverse=True)
        return aggregated

    agg_3way = aggregate(all_3way_results)
    agg_2way = aggregate(all_2way_results)

    # ─── Find current default ───
    default_3way_idx = None
    for i, a in enumerate(agg_3way):
        if abs(a["w_dense"] - 0.5) < 0.01 and abs(a["w_sparse"] - 0.2) < 0.01 and abs(a["w_bm25"] - 0.3) < 0.01:
            default_3way_idx = i
            break

    for i, a in enumerate(agg_2way):
        if abs(a["w_dense"] - 0.7) < 0.01 and abs(a["w_bm25"] - 0.3) < 0.01:
            break

    # ─── R@1 perfect count ───
    n_queries = len(LARGE_EVAL_QUERIES)
    perfect_3way = sum(1 for a in agg_3way if a["r1_count"] == n_queries)
    perfect_2way = sum(1 for a in agg_2way if a["r1_count"] == n_queries)

    # ─── Build report ───
    lines = [
        "# Hybrid Search Weight Sweep Report",
        "",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "**Chunk Config:** Clause_1000_150 (locked)",
        f"**Query Count:** {n_queries} (Manual={sum(1 for q in LARGE_EVAL_QUERIES if q['category']=='manual')}, "
        f"SOP={sum(1 for q in LARGE_EVAL_QUERIES if q['category']=='sop')}, "
        f"FAQ={sum(1 for q in LARGE_EVAL_QUERIES if q['category']=='faq')}, "
        f"Policy={sum(1 for q in LARGE_EVAL_QUERIES if q['category']=='policy')})",
        f"**3-Way Combos:** {len(agg_3way)} | **2-Way Combos:** {len(agg_2way)}",
        "",
        "---",
        "",
        "## 1. Executive Summary",
        "",
        f"- **R@1 = 100% 的权重组合数（三路）:** {perfect_3way} / {len(agg_3way)} ({perfect_3way/len(agg_3way)*100:.1f}%)",
        f"- **R@1 = 100% 的权重组合数（两路）:** {perfect_2way} / {len(agg_2way)} ({perfect_2way/len(agg_2way)*100:.1f}%)",
    ]

    if default_3way_idx is not None:
        d = agg_3way[default_3way_idx]
        lines.append(f"- **当前默认 (0.5/0.2/0.3) 排名:** #{default_3way_idx + 1} / {len(agg_3way)} "
                     f"(Macro MRR={d['macro_mrr']:.4f}, Margin Mean={d['margin_mean']:.4f}, "
                     f"Bootstrap 95% CI=[{d['boot_lo']:.4f}, {d['boot_hi']:.4f}])")

    champion = agg_3way[0]
    lines.append(f"- **最优权重:** dense={champion['w_dense']:.1f}, sparse={champion['w_sparse']:.1f}, BM25={champion['w_bm25']:.1f} "
                 f"(Macro MRR={champion['macro_mrr']:.4f}, Margin Mean={champion['margin_mean']:.4f})")

    # Check if default is statistically distinguishable from champion
    if default_3way_idx is not None:
        d = agg_3way[default_3way_idx]
        if d["boot_lo"] <= champion["boot_hi"] and champion["boot_lo"] <= d["boot_hi"]:
            lines.append("")
            lines.append("> [!NOTE]")
            lines.append(f"> 当前默认权重 (0.5/0.2/0.3) 的 Bootstrap 95% CI [{d['boot_lo']:.4f}, {d['boot_hi']:.4f}] "
                         f"与最优权重的 CI [{champion['boot_lo']:.4f}, {champion['boot_hi']:.4f}] **重叠**，差异不显著。")
        else:
            lines.append("")
            lines.append("> [!WARNING]")
            lines.append(f"> 最优权重的 Bootstrap 95% CI [{champion['boot_lo']:.4f}, {champion['boot_hi']:.4f}] "
                         f"与默认权重 CI [{d['boot_lo']:.4f}, {d['boot_hi']:.4f}] **不重叠**，差异显著，建议更新默认权重。")

    # ─── 2. Three-Way Leaderboard (Top-10 + Bottom-10) ───
    lines.extend([
        "",
        "---",
        "",
        "## 2. Three-Way Fusion Leaderboard (Dense + Sparse + BM25)",
        "",
        "### Top-10",
        "",
        "| # | Dense | Sparse | BM25 | R@1 | Macro MRR | Boot 95% CI | Margin Min | Margin Mean | Top5 Poll | Fallback | Note |",
        "| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
    ])

    def format_row(i, a, mark=""):
        note = mark
        if abs(a["w_dense"] - 0.5) < 0.01 and abs(a["w_sparse"] - 0.2) < 0.01 and abs(a["w_bm25"] - 0.3) < 0.01:
            note += " 📌 当前默认"
        return (f"| {i+1} | {a['w_dense']:.1f} | {a['w_sparse']:.1f} | {a['w_bm25']:.1f} "
                f"| {a['r1']:.2%} | {a['macro_mrr']:.4f} | [{a['boot_lo']:.4f}, {a['boot_hi']:.4f}] "
                f"| {a['margin_min']:.4f} | {a['margin_mean']:.4f} | {a['avg_t5p']:.2%} "
                f"| {a['fallback_count']} | {note.strip()} |")

    for i in range(min(10, len(agg_3way))):
        mark = "🏆" if i == 0 else ""
        lines.append(format_row(i, agg_3way[i], mark))

    if len(agg_3way) > 20:
        lines.extend([
            "",
            "### Bottom-10",
            "",
            "| # | Dense | Sparse | BM25 | R@1 | Macro MRR | Boot 95% CI | Margin Min | Margin Mean | Top5 Poll | Fallback | Note |",
            "| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
        ])
        for i in range(max(0, len(agg_3way) - 10), len(agg_3way)):
            lines.append(format_row(i, agg_3way[i]))

    # If default is outside top-10, show it explicitly
    if default_3way_idx is not None and default_3way_idx >= 10:
        lines.extend([
            "",
            f"### 当前默认权重 (排名 #{default_3way_idx + 1})",
            "",
            "| # | Dense | Sparse | BM25 | R@1 | Macro MRR | Boot 95% CI | Margin Min | Margin Mean | Top5 Poll | Fallback |",
            "| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
        ])
        d = agg_3way[default_3way_idx]
        lines.append(f"| {default_3way_idx+1} | {d['w_dense']:.1f} | {d['w_sparse']:.1f} | {d['w_bm25']:.1f} "
                     f"| {d['r1']:.2%} | {d['macro_mrr']:.4f} | [{d['boot_lo']:.4f}, {d['boot_hi']:.4f}] "
                     f"| {d['margin_min']:.4f} | {d['margin_mean']:.4f} | {d['avg_t5p']:.2%} | {d['fallback_count']} |")

    # ─── 3. Two-Way Fallback Leaderboard ───
    lines.extend([
        "",
        "---",
        "",
        "## 3. Two-Way Fallback Leaderboard (Dense + BM25, no Sparse)",
        "",
        "| # | Dense | BM25 | R@1 | Macro MRR | Boot 95% CI | Margin Min | Margin Mean | Top5 Poll | Fallback | Note |",
        "| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |",
    ])
    for i, a in enumerate(agg_2way):
        note = ""
        if i == 0:
            note = "🏆"
        if abs(a["w_dense"] - 0.7) < 0.01 and abs(a["w_bm25"] - 0.3) < 0.01:
            note += " 📌 当前默认"
        lines.append(f"| {i+1} | {a['w_dense']:.1f} | {a['w_bm25']:.1f} "
                     f"| {a['r1']:.2%} | {a['macro_mrr']:.4f} | [{a['boot_lo']:.4f}, {a['boot_hi']:.4f}] "
                     f"| {a['margin_min']:.4f} | {a['margin_mean']:.4f} | {a['avg_t5p']:.2%} "
                     f"| {a['fallback_count']} | {note.strip()} |")

    # ─── 4. Per-Query Sensitivity Analysis ───
    lines.extend([
        "",
        "---",
        "",
        "## 4. Per-Query Sensitivity Analysis (三路融合)",
        "",
        "每个 Query 在多少种权重组合下保持 R@1 = 1（越低 = 越脆弱）",
        "",
        "| Query ID | Category | Query | Target Doc | R@1 维持率 | 最小 Score Margin | 最脆弱权重 |",
        "| :---: | :---: | :--- | :--- | :---: | :---: | :--- |",
    ])

    # Compute per-query sensitivity across all 3-way combos
    for q_idx, q_info in enumerate(LARGE_EVAL_QUERIES):
        qid = q_info["id"]
        r1_count = 0
        min_margin = float("inf")
        worst_label = ""
        for sw in all_3way_results:
            r = sw["results"][q_idx]
            if r["r1"] == 1:
                r1_count += 1
            if r["score_margin"] < min_margin:
                min_margin = r["score_margin"]
                worst_label = sw["label"]

        durability = r1_count / len(all_3way_results) if all_3way_results else 0
        min_margin_str = f"{min_margin:.4f}" if min_margin < float("inf") else "N/A"
        emoji = "✅" if durability >= 0.9 else ("⚠️" if durability >= 0.5 else "❌")
        lines.append(f"| **{qid}** | `{q_info['category']}` | {q_info['new_query'][:30]}... "
                     f"| `{q_info['target_doc']}` | {emoji} {durability:.0%} "
                     f"| {min_margin_str} | `{worst_label}` |")

    # ─── 5. Fallback Trigger Heatmap ───
    lines.extend([
        "",
        "---",
        "",
        "## 5. Fallback 触发分析 (Review Finding #1)",
        "",
        "记录权重组合触发 L948 全局 fallback 的频率（跳过元数据过滤）",
        "",
    ])
    fallback_combos = [(a["label"], a["fallback_count"]) for a in agg_3way if a["fallback_count"] > 0]
    if fallback_combos:
        lines.append("| 权重组合 | Fallback 触发次数 (/ " + str(n_queries) + ") |")
        lines.append("| :--- | :---: |")
        fallback_combos.sort(key=lambda x: x[1], reverse=True)
        for label, count in fallback_combos[:20]:
            lines.append(f"| `{label}` | {count} |")
    else:
        lines.append("✅ 无任何权重组合触发 fallback。")

    # ─── 6. Score Margin Distribution for Top-5 configs ───
    lines.extend([
        "",
        "---",
        "",
        "## 6. Score Margin 分布（Top-5 配置）",
        "",
        "| 配置 | Min | P25 | Median | Mean | 注释 |",
        "| :--- | :---: | :---: | :---: | :---: | :--- |",
    ])
    for a in agg_3way[:5]:
        note = ""
        if abs(a["w_dense"] - 0.5) < 0.01 and abs(a["w_sparse"] - 0.2) < 0.01:
            note = "📌 当前默认"
        lines.append(f"| `{a['label']}` | {a['margin_min']:.4f} | {a['margin_p25']:.4f} "
                     f"| {a['margin_median']:.4f} | {a['margin_mean']:.4f} | {note} |")

    # ─── Limitations ───
    lines.extend([
        "",
        "---",
        "",
        "## 7. 实验局限性",
        "",
        "> [!NOTE]",
        "> 1. **归一化方法影响**: 权重最优值基于 per-query min-max 归一化。如果生产环境使用不同归一化策略，结论不可直接迁移。",
        "> 2. **样本量限制**: 47 条 Query（FAQ 仅 7 条），per-category 结论的统计功效有限。Bootstrap CI 提供了量化的不确定性估计。",
        "> 3. **天花板效应**: 在当前评测集上大量权重组合可能都达到 R@1=100%，区分力主要来自 Score Margin 和 Top-5 Pollution。",
    ])

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n>>> Report generated at {report_path}")


# ─── Main ───

def main():
    config = get_config()
    config.simulate = False

    print("=" * 60)
    print("   Weight Sweep Experiment")
    print("   Locked Chunk Config: Clause_1000_150")
    print("=" * 60)

    # ─── Step 1: Build chunks (one-time) ───
    print("\n>>> Step 1: Building chunks (Clause_1000_150)...")
    all_chunks, emb_cache = build_chunks_clause_1000_150(config)

    # ─── Step 2: Prepare query embeddings ───
    print("\n>>> Step 2: Loading query embeddings...")
    new_queries_text = [q["new_query"] for q in LARGE_EVAL_QUERIES]
    new_query_vectors, new_query_sparse = get_cached_embeddings(new_queries_text, emb_cache, config)

    # ─── Step 3: Build search pool + BM25 (one-time) ───
    print("\n>>> Step 3: Building search pool and BM25 index...")
    is_parent_child = any(c.get("chunk_type") == "child_chunk" for c in all_chunks)
    if is_parent_child:
        def get_parent_id(c):
            if c.get("extra") and "parent_id" in c["extra"]:
                return c["extra"]["parent_id"]
            cid = c.get("chunk_id", "")
            if "_child_" in cid:
                return cid.split("_child_")[0]
            return cid
        child_parent_ids = {get_parent_id(c) for c in all_chunks if c.get("chunk_type") == "child_chunk"}
        search_pool = [
            c for c in all_chunks
            if c.get("chunk_type") == "child_chunk" or get_parent_id(c) not in child_parent_ids
        ]
    else:
        search_pool = all_chunks

    print(f"    └─ Search pool size: {len(search_pool)}")
    tokenized_corpus = [list(jieba.cut(c["chunk_text"])) for c in search_pool]
    bm25 = BM25Okapi(tokenized_corpus)

    # ─── Step 4: Three-Way Weight Sweep ───
    grid_3way = generate_3way_grid(0.1)
    print(f"\n>>> Step 4: Running 3-way weight sweep ({len(grid_3way)} combos)...")

    all_3way_results = []
    for combo_idx, (wd, ws, wb) in enumerate(grid_3way):
        label = f"D{wd:.1f}_S{ws:.1f}_B{wb:.1f}"
        if (combo_idx + 1) % 10 == 0 or combo_idx == 0:
            print(f"    [{combo_idx+1}/{len(grid_3way)}] {label}")

        results = evaluate_single_weight(
            search_pool, all_chunks, bm25, new_query_vectors, new_query_sparse,
            w_dense=wd, w_sparse=ws, w_bm25=wb, is_3way=True
        )
        all_3way_results.append({
            "label": label,
            "w_dense": wd, "w_sparse": ws, "w_bm25": wb,
            "results": results,
        })

    # ─── Step 5: Two-Way Weight Sweep (fallback, no sparse) ───
    grid_2way = generate_2way_grid(0.1)
    print(f"\n>>> Step 5: Running 2-way weight sweep ({len(grid_2way)} combos)...")

    all_2way_results = []
    for combo_idx, (wd, wb) in enumerate(grid_2way):
        label = f"D{wd:.1f}_B{wb:.1f}"
        print(f"    [{combo_idx+1}/{len(grid_2way)}] {label}")

        results = evaluate_single_weight(
            search_pool, all_chunks, bm25, new_query_vectors, new_query_sparse,
            w_dense=wd, w_sparse=0.0, w_bm25=wb, is_3way=False
        )
        all_2way_results.append({
            "label": label,
            "w_dense": wd, "w_sparse": 0.0, "w_bm25": wb,
            "results": results,
        })

    # ─── Step 6: Generate Report ───
    print("\n>>> Step 6: Generating report...")
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weight_sweep_report.md")
    generate_report(all_3way_results, all_2way_results, report_path)

    # ─── Quick Summary ───
    print("\n" + "=" * 60)
    print("   QUICK SUMMARY")
    print("=" * 60)

    # Find default in 3-way results
    for sw in all_3way_results:
        if abs(sw["w_dense"] - 0.5) < 0.01 and abs(sw["w_sparse"] - 0.2) < 0.01:
            n = len(sw["results"])
            r1 = sum(r["r1"] for r in sw["results"]) / n
            mrr = sum(r["mrr"] for r in sw["results"]) / n
            margins = [r["score_margin"] for r in sw["results"]]
            print(f"  Default (0.5/0.2/0.3): R@1={r1:.2%}, MRR={mrr:.4f}, Margin Mean={np.mean(margins):.4f}, Min={min(margins):.4f}")
            break

    # Find best in 3-way results
    best = None
    best_macro = -1.0
    for sw in all_3way_results:
        results = sw["results"]
        n = len(results)
        cat_mrrs = []
        for cat in ["manual", "sop", "faq", "policy"]:
            cat_res = [r for r in results if r["category"] == cat]
            if cat_res:
                cat_mrrs.append(sum(r["mrr"] for r in cat_res) / len(cat_res))
        macro = sum(cat_mrrs) / len(cat_mrrs) if cat_mrrs else 0.0
        if macro > best_macro or (macro == best_macro and best is not None and np.mean([r["score_margin"] for r in results]) > np.mean([r["score_margin"] for r in best["results"]])):
            best_macro = macro
            best = sw

    if best:
        n = len(best["results"])
        r1 = sum(r["r1"] for r in best["results"]) / n
        margins = [r["score_margin"] for r in best["results"]]
        print(f"  Best    ({best['label']}): R@1={r1:.2%}, Macro MRR={best_macro:.4f}, Margin Mean={np.mean(margins):.4f}, Min={min(margins):.4f}")

    print(f"\n  Full report: {report_path}")


if __name__ == "__main__":
    main()

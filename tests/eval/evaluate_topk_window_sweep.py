# -*- coding: utf-8 -*-
"""
evaluate_topk_window_sweep.py — top_k × stitch_window 参数对比评测

在 120-query 生产评测集上，对比不同 (top_k, stitch_window) 组合的检索质量。

只测量确定性指标（无 LLM 调用），运行速度快：
  - R@1 / R@5: 目标文档是否在 top-K 检索结果中
  - Context Coverage: required_keyword_groups 在 context 中的覆盖率
  - Context Length: context 总字符数（评估 max_context_chars 溢出）
  - Effective Chunks: 在 6000 chars 截断下实际利用的 chunk 数

用法：
  python -m tests.eval.evaluate_topk_window_sweep
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple

# 项目路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env.local"))

os.environ["RAG_ENVIRONMENT"] = "development"

import pymysql  # noqa: E402
from opensearch_pipeline.retriever import search_chunks  # noqa: E402

try:  # expand_top_document 已从生产代码移除（deprecated 死代码）；旧版对照模式自动降级跳过
    from opensearch_pipeline.retriever import expand_top_document
except ImportError:
    expand_top_document = None

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

QUERIES_PATH = os.path.join(PROJECT_ROOT, "scratch", "targeted_eval_queries.json")
REPORT_PATH = os.path.join(PROJECT_ROOT, "tests", "eval", "topk_window_sweep_report.md")
MAX_CONTEXT_CHARS = 6000  # 与 llm_generator._format_context 一致

# 要测试的参数组合
SWEEP_CONFIGS = [
    # (top_k, stitch_window, expand, label)
    (5,  0, False, "top_k=5, no stitch (当前 API)"),
    (5,  1, False, "top_k=5, window=±1"),
    (5,  1, True,  "top_k=5, expand+stitch=±1 (旧版)"),
    (7,  0, False, "top_k=7, no stitch"),
    (7,  1, False, "top_k=7, window=±1 (推荐)"),
    (7,  1, True,  "top_k=7, expand+stitch=±1"),
    (10, 0, False, "top_k=10, no stitch"),
    (10, 1, False, "top_k=10, window=±1"),
    (10, 2, False, "top_k=10, window=±2"),
    (20, 2, False, "top_k=20→10, window=±2 (当前 DingTalk)"),
]


# ═══════════════════════════════════════════════════════════════
# RDS 连接
# ═══════════════════════════════════════════════════════════════

def get_rds_conn():
    return pymysql.connect(
        host=os.environ.get("RAG_RDS_HOST", "localhost"),
        port=int(os.environ.get("RAG_RDS_PORT", 3306)),
        user=os.environ.get("RAG_RDS_USER", "root"),
        password=os.environ.get("RAG_RDS_PASSWORD", ""),
        database=os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge"),
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_neighbor_chunks(conn, doc_id: str, center_index: int, window: int) -> List[Dict]:
    cur = conn.cursor()
    cur.execute("""
        SELECT chunk_id, doc_id, chunk_index, chunk_text, section_title
        FROM chunk_meta
        WHERE doc_id = %s
          AND is_active = 1
          AND chunk_index BETWEEN %s AND %s
        ORDER BY chunk_index
    """, (doc_id, center_index - window, center_index + window))
    return cur.fetchall()


# ═══════════════════════════════════════════════════════════════
# 关键词匹配
# ═══════════════════════════════════════════════════════════════

def is_keyword_in_text(keyword: str, text: str) -> bool:
    text_lower = text.lower().replace(" ", "")
    keyword_lower = keyword.lower().replace(" ", "")
    return keyword_lower in text_lower


def compute_keyword_coverage(text: str, required_keyword_groups: List[List[str]]) -> float:
    if not required_keyword_groups:
        return 1.0
    matched_groups = 0
    for group in required_keyword_groups:
        if any(is_keyword_in_text(kw, text) for kw in group):
            matched_groups += 1
    return matched_groups / len(required_keyword_groups)


# ═══════════════════════════════════════════════════════════════
# Context 构建（模拟 _format_context 的截断行为）
# ═══════════════════════════════════════════════════════════════

def build_context_with_stitch(
    hits: List[Dict], rds_conn, window: int, max_chars: int = MAX_CONTEXT_CHARS
) -> Tuple[str, int, int]:
    """构建 stitched context 并模拟 max_context_chars 截断。

    Returns:
        (context_text, total_chars_before_truncation, effective_chunk_count)
    """
    if window <= 0:
        # No stitch: 直接拼接
        return _build_flat_context(hits, max_chars)

    seen_keys = set()
    parts = []
    total_chars = 0
    effective_count = 0

    for chunk in hits:
        doc_id = chunk.get("doc_id", "")
        center_idx = chunk.get("chunk_index", 0)
        title = chunk.get("title", "未知文档")
        section = chunk.get("section_title", "")

        center_key = (doc_id, center_idx)
        if center_key in seen_keys:
            continue
        seen_keys.add(center_key)

        # 从 RDS 查询邻居
        if doc_id:
            neighbors = fetch_neighbor_chunks(rds_conn, doc_id, center_idx, window)
            window_chunks = []
            for n in neighbors:
                key = (n["doc_id"], n["chunk_index"])
                if key not in seen_keys:
                    window_chunks.append(n)
                    seen_keys.add(key)
                elif key == center_key:
                    window_chunks.append(n)

            if not window_chunks:
                window_chunks = [chunk]

            window_chunks.sort(key=lambda c: c.get("chunk_index", 0))
            stitched_text = "\n".join(c.get("chunk_text", "") for c in window_chunks)
        else:
            stitched_text = chunk.get("chunk_text", "")

        header = f"[文档] {title}"
        if section:
            header += f" > {section}"
        entry = f"{header}\n{stitched_text}\n"

        if total_chars + len(entry) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 100:
                parts.append(entry[:remaining] + "...(截断)")
                effective_count += 1
            break

        parts.append(entry)
        total_chars += len(entry)
        effective_count += 1

    full_text = "\n---\n".join(parts)
    total_before_trunc = sum(len(p) for p in parts)

    return full_text, total_before_trunc, effective_count


def _build_flat_context(
    hits: List[Dict], max_chars: int = MAX_CONTEXT_CHARS
) -> Tuple[str, int, int]:
    """Flat context（无 stitch）。"""
    parts = []
    total_chars = 0
    effective_count = 0

    for chunk in hits:
        title = chunk.get("title", "未知文档")
        section = chunk.get("section_title", "")
        text = chunk.get("chunk_text", "")

        header = f"[文档] {title}"
        if section:
            header += f" > {section}"
        entry = f"{header}\n{text}\n"

        if total_chars + len(entry) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 100:
                parts.append(entry[:remaining] + "...(截断)")
                effective_count += 1
            break

        parts.append(entry)
        total_chars += len(entry)
        effective_count += 1

    full_text = "\n---\n".join(parts)
    return full_text, total_chars, effective_count


# ═══════════════════════════════════════════════════════════════
# 评测主流程
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  top_k × stitch_window 参数对比评测")
    print("  120-query 生产评测集 · 确定性指标 · 无 LLM 调用")
    print("=" * 70)

    # 1. 加载 queries
    with open(QUERIES_PATH, "r", encoding="utf-8") as f:
        queries = json.load(f)
    print(f"\n  Queries: {len(queries)}")
    print(f"  Configs: {len(SWEEP_CONFIGS)}")

    # 2. 初始化
    rds_conn = get_rds_conn()
    print("  ✅ RDS 连接成功")

    search_chunks("测试连接", top_k=1)
    print("  ✅ HA3 连接成功")

    # 3. 缓存所有 query 的 HA3 检索结果（共享 embedding，减少 API 调用）
    # 只需要最大 top_k 的结果，较小的 top_k 取前缀即可
    max_top_k = max(cfg[0] for cfg in SWEEP_CONFIGS)
    print(f"\n  预检索: 对 {len(queries)} 个 query 执行 top_k={max_top_k} 检索...")

    query_hits_cache = {}
    t0 = time.time()

    for qi, q in enumerate(queries):
        query_text = q["query"]
        dept = q.get("owner_dept")

        try:
            hits = search_chunks(query_text, top_k=max_top_k, user_dept=dept)
            query_hits_cache[qi] = hits
        except Exception as e:
            print(f"    ⚠️ Q{qi}: 检索失败 {e}")
            query_hits_cache[qi] = []

        if (qi + 1) % 30 == 0:
            elapsed = time.time() - t0
            print(f"    {qi+1}/{len(queries)} done ({elapsed:.1f}s)")

    elapsed = time.time() - t0
    print(f"  ✅ 预检索完成 ({elapsed:.1f}s, 平均 {elapsed/len(queries):.2f}s/query)")

    # 4. 对每个 config 执行评测
    all_results = {}

    for top_k, window, use_expand, label in SWEEP_CONFIGS:
        print(f"\n  === Config: {label} ===")

        metrics = {
            "r_at_1": 0, "r_at_5": 0, "ctx_coverage": 0,
            "ctx_len_total": 0, "effective_chunks_total": 0,
            "count": 0,
            "per_category": defaultdict(lambda: {"r_at_1": 0, "ctx_coverage": 0, "count": 0}),
            "per_difficulty": defaultdict(lambda: {"r_at_1": 0, "ctx_coverage": 0, "count": 0}),
        }

        for qi, q in enumerate(queries):
            cached_hits = query_hits_cache.get(qi, [])

            # 模拟 DingTalk 的 top_k=20 → [:10] 截取
            if top_k == 20 and label.startswith("top_k=20"):
                hits = cached_hits[:10]
            else:
                hits = cached_hits[:top_k]

            # 旧版策略：先 expand_top_document 再 stitch（函数已移除时跳过该对照模式）
            if use_expand and hits and expand_top_document is not None:
                try:
                    hits = expand_top_document(hits)
                except Exception:
                    pass  # expand 失败时回退到原始结果

            if not hits:
                continue

            target_doc = q.get("target_doc_id", "")
            keyword_groups = q.get("required_keyword_groups", [])
            difficulty = q.get("difficulty", "unknown")
            category = q.get("category_name", "unknown")

            # R@1 / R@5
            r_at_1 = 1 if hits[0].get("doc_id") == target_doc else 0
            r_at_5 = 1 if any(h.get("doc_id") == target_doc for h in hits[:5]) else 0

            # Context Coverage
            context_text, ctx_len, eff_chunks = build_context_with_stitch(
                hits, rds_conn, window
            )
            ctx_coverage = compute_keyword_coverage(context_text, keyword_groups)

            metrics["r_at_1"] += r_at_1
            metrics["r_at_5"] += r_at_5
            metrics["ctx_coverage"] += ctx_coverage
            metrics["ctx_len_total"] += ctx_len
            metrics["effective_chunks_total"] += eff_chunks
            metrics["count"] += 1

            cat_m = metrics["per_category"][category]
            cat_m["r_at_1"] += r_at_1
            cat_m["ctx_coverage"] += ctx_coverage
            cat_m["count"] += 1

            diff_m = metrics["per_difficulty"][difficulty]
            diff_m["r_at_1"] += r_at_1
            diff_m["ctx_coverage"] += ctx_coverage
            diff_m["count"] += 1

        n = max(metrics["count"], 1)
        print(f"    R@1={metrics['r_at_1']/n:.1%}  R@5={metrics['r_at_5']/n:.1%}  "
              f"CC={metrics['ctx_coverage']/n:.1%}  "
              f"AvgCtxLen={metrics['ctx_len_total']/n:.0f}  "
              f"AvgEffChunks={metrics['effective_chunks_total']/n:.1f}")

        all_results[label] = metrics

    rds_conn.close()

    # 5. 生成报告
    print("\n  生成报告...")
    report = generate_report(all_results, len(queries))
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✅ 报告 → {REPORT_PATH}")

    # Print summary table
    print("\n" + "=" * 100)
    print("  评测结果总览")
    print("=" * 100)
    header = f"  {'Config':<40} {'R@1':>6} {'R@5':>6} {'CC':>6} {'AvgLen':>8} {'EffChks':>8}"
    print(header)
    print("  " + "-" * 94)
    for label, m in all_results.items():
        n = max(m["count"], 1)
        print(f"  {label:<40} "
              f"{m['r_at_1']/n:>5.1%} "
              f"{m['r_at_5']/n:>5.1%} "
              f"{m['ctx_coverage']/n:>5.1%} "
              f"{m['ctx_len_total']/n:>7.0f} "
              f"{m['effective_chunks_total']/n:>7.1f}")


def generate_report(all_results: Dict, total_queries: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    report = f"""# top_k × stitch_window 参数对比评测报告

**生成时间**: {now}
**评测 Query 数**: {total_queries}
**检索方式**: HA3 混合检索 (Dense + Sparse + BM25 Weighted)
**max_context_chars**: {MAX_CONTEXT_CHARS}

---

## 1. 总体对比

| Config | R@1 | R@5 | Context Coverage | Avg Context Len | Avg Effective Chunks | 溢出情况 |
|---|---|---|---|---|---|---|
"""

    for label, m in all_results.items():
        n = max(m["count"], 1)
        avg_len = m["ctx_len_total"] / n
        eff = m["effective_chunks_total"] / n
        overflow = "✅ 无溢出" if avg_len <= MAX_CONTEXT_CHARS else f"⚠️ 溢出 {avg_len - MAX_CONTEXT_CHARS:.0f} chars"
        report += (f"| {label} "
                   f"| {m['r_at_1']/n:.1%} "
                   f"| {m['r_at_5']/n:.1%} "
                   f"| {m['ctx_coverage']/n:.1%} "
                   f"| {avg_len:.0f} "
                   f"| {eff:.1f} "
                   f"| {overflow} |\n")

    # Per-difficulty breakdown
    report += "\n---\n\n## 2. 按难度分类\n\n"
    difficulties = ["single_chunk", "cross_chunk", "multi_doc", "reasoning",
                     "disambiguation", "query_robustness", "unanswerable", "permission"]

    for diff in difficulties:
        rows = []
        for label, m in all_results.items():
            dm = m["per_difficulty"].get(diff)
            if dm and dm["count"] > 0:
                rows.append((label, dm))

        if not rows:
            continue

        report += f"\n### {diff}\n\n"
        report += "| Config | R@1 | CC | N |\n|---|---|---|---|\n"
        for label, dm in rows:
            dn = dm["count"]
            report += f"| {label} | {dm['r_at_1']/dn:.1%} | {dm['ctx_coverage']/dn:.1%} | {dn} |\n"

    # Per-category breakdown for top configs
    report += "\n---\n\n## 3. 按 Query 类别对比（推荐 vs 当前）\n\n"

    # Compare recommended vs current DingTalk
    recommended_label = "top_k=7, window=±1 (推荐)"
    dingtalk_label = "top_k=20→10, window=±2 (当前 DingTalk)"
    api_label = "top_k=5, no stitch (当前 API)"
    old_label = "top_k=5, expand+stitch=±1 (旧版)"

    compare_labels = [api_label, old_label, recommended_label, dingtalk_label]
    categories = set()
    for m in all_results.values():
        categories.update(m["per_category"].keys())

    report += "| 类别 | N |"
    for lbl in compare_labels:
        short = lbl.split("(")[1].rstrip(")") if "(" in lbl else lbl
        report += f" {short} CC |"
    report += "\n|---|---|" + "---|" * len(compare_labels) + "\n"

    for cat in sorted(categories):
        first_m = list(all_results.values())[0]["per_category"].get(cat)
        if not first_m:
            continue
        cn = first_m["count"]
        report += f"| {cat} | {cn} |"
        for lbl in compare_labels:
            cm = all_results.get(lbl, {}).get("per_category", {}).get(cat)
            if cm and cm["count"] > 0:
                report += f" {cm['ctx_coverage']/cm['count']:.1%} |"
            else:
                report += " — |"
        report += "\n"

    # Recommendation
    report += """
---

## 4. 参数选择建议

基于以上数据，最终统一参数应选择 **R@1 和 Context Coverage 最优且无 context 溢出** 的组合。

> [!NOTE]
> R@1 衡量检索精度（第一条结果是否命中目标文档），Context Coverage 衡量上下文完整性（关键信息是否在 context 中），
> 两者需要平衡。R@1 高但 CC 低 = 找到了但信息不完整；R@1 低但 CC 高 = 信息分散在多个文档中。
"""

    return report


if __name__ == "__main__":
    main()

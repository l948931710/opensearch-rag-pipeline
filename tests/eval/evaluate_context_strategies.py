# -*- coding: utf-8 -*-
"""
evaluate_context_strategies.py — 上下文策略对比评测（v2: 直接调用 HA3 生产检索）

对比 2 种策略在生产数据上的表现：
  A: Flat Baseline — 直接用 HA3 返回的 chunk 文本作为 LLM 上下文
  B: Neighbor Stitching — 命中 chunk + 从 RDS 查询同文档 chunk_index ±1 邻居

评测指标：
  - Context Coverage: required_keyword_groups 在上下文中的覆盖率（确定性）
  - Answer Completeness: required_keyword_groups 在 LLM 答案中的覆盖率
  - R@1: 第一条检索结果是否命中目标文档
  - Context Length: 上下文总字符数（token 成本指标）

设计原则：
  - 复用 HA3 生产检索基础设施，不重新 embedding chunk
  - 只对 query 做 embedding（通过 search_chunks 封装）
  - 邻居 chunk 从 RDS chunk_meta 表查询
"""

import os
import sys
import json
import time
from datetime import datetime
from typing import List, Dict

# 项目路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(PROJECT_ROOT, ".env.local"))

# 本地评测强制用公网域名（覆盖 .env.local 里的 production 设置）
os.environ["RAG_ENVIRONMENT"] = "development"

import pymysql  # noqa: E402
import requests  # noqa: E402

from opensearch_pipeline.retriever import search_chunks  # noqa: E402

# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

API_KEY = os.environ.get("DASHSCOPE_API_KEY", os.environ.get("RAG_DASHSCOPE_API_KEY", ""))
API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
LLM_MODEL = "qwen3.6-plus"

QUERIES_PATH = os.path.join(PROJECT_ROOT, "scratch", "targeted_eval_queries.json")
RESULTS_PATH = os.path.join(PROJECT_ROOT, "tests", "eval", "context_strategy_results.json")
REPORT_PATH = os.path.join(PROJECT_ROOT, "tests", "eval", "context_strategy_report.md")

TOP_K = 5
NEIGHBOR_WINDOW = 1  # ±1 chunk

# Rate limiting
MIN_INTERVAL = 0.2
_last_call_time = 0.0


# ═══════════════════════════════════════════════════════════════
# RDS 连接（查邻居 chunk）
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


def fetch_neighbor_chunks(
    conn, doc_id: str, center_index: int, window: int = NEIGHBOR_WINDOW
) -> List[Dict]:
    """从 RDS 查询指定 doc_id 的相邻 chunk"""
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


def fetch_section_chunks(conn, doc_id: str, section_title: str) -> List[Dict]:
    """从 RDS 查询同 doc_id + 同 section_title 的所有 chunk（模拟 parent section）"""
    cur = conn.cursor()
    if section_title:
        cur.execute("""
            SELECT chunk_id, doc_id, chunk_index, chunk_text, section_title
            FROM chunk_meta
            WHERE doc_id = %s
              AND section_title = %s
              AND is_active = 1
            ORDER BY chunk_index
        """, (doc_id, section_title))
    else:
        # 无 section_title 的 chunk，fallback 到 ±2 窗口
        cur.execute("""
            SELECT chunk_id, doc_id, chunk_index, chunk_text, section_title
            FROM chunk_meta
            WHERE doc_id = %s
              AND (section_title IS NULL OR section_title = '')
              AND is_active = 1
            ORDER BY chunk_index
            LIMIT 10
        """, (doc_id,))
    return cur.fetchall()


# ═══════════════════════════════════════════════════════════════
# LLM 调用
# ═══════════════════════════════════════════════════════════════

def call_llm_for_answer(query: str, context: str, max_retries: int = 3) -> str:
    """调用 LLM 生成回答"""
    global _last_call_time

    system_prompt = """你是富岭科技股份有限公司的内部知识库助手。
请根据提供的参考资料回答员工的问题。
要求：
1. 只使用参考资料中的信息回答，不要编造
2. 回答要具体、准确，包含关键数据和步骤
3. 如果参考资料中没有相关信息，明确说明"未找到相关信息"
4. 用简洁的中文回答"""

    user_prompt = f"""参考资料：
{context}

员工问题：{query}

请根据参考资料回答："""

    for attempt in range(max_retries):
        elapsed = time.time() - _last_call_time
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)

        try:
            resp = requests.post(
                API_URL,
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 1500,
                    "enable_thinking": False,
                },
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=120,
            )
            _last_call_time = time.time()

            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            result = resp.json()
            content = result["choices"][0]["message"]["content"]
            if "</think>" in content:
                content = content.split("</think>")[-1].strip()
            return content

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                return f"[LLM 调用失败: {e}]"


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
# 上下文策略
# ═══════════════════════════════════════════════════════════════

def strategy_flat(hits: List[Dict]) -> str:
    """策略 A: 直接拼接 HA3 返回的 top-K chunk 文本"""
    parts = []
    for chunk in hits:
        title = chunk.get("title", "")
        section = chunk.get("section_title", "")
        text = chunk.get("chunk_text", "")
        header = f"[{title}]"
        if section:
            header += f" > {section}"
        parts.append(f"{header}\n{text}")
    return "\n\n---\n\n".join(parts)


def strategy_neighbor_stitch(
    hits: List[Dict], rds_conn
) -> str:
    """策略 B: 每个命中 chunk 扩展为 chunk_index ±1 窗口，从 RDS 查邻居"""
    seen_keys = set()
    context_parts = []

    for chunk in hits:
        doc_id = chunk.get("doc_id", "")
        center_idx = chunk.get("chunk_index", 0)
        title = chunk.get("title", "")

        # 从 RDS 查询邻居
        neighbors = fetch_neighbor_chunks(rds_conn, doc_id, center_idx, NEIGHBOR_WINDOW)

        window = []
        for n in neighbors:
            key = (n["doc_id"], n["chunk_index"])
            if key not in seen_keys:
                window.append(n)
                seen_keys.add(key)

        if not window:
            # Fallback: 至少保留原始 chunk
            key = (doc_id, center_idx)
            if key not in seen_keys:
                window.append(chunk)
                seen_keys.add(key)

        window.sort(key=lambda c: c.get("chunk_index", 0))
        window_text = "\n".join(c.get("chunk_text", "") for c in window)

        header = f"[{title}]"
        section = window[0].get("section_title", "") if window else ""
        if section:
            header += f" > {section}"
        context_parts.append(f"{header}\n{window_text}")

    return "\n\n---\n\n".join(context_parts)


def strategy_parent_child(hits: List[Dict], rds_conn) -> str:
    """策略 C: 命中 child chunk → 返回同 section_title 的全部 chunk（模拟 parent section）"""
    seen_keys = set()
    context_parts = []

    for chunk in hits:
        doc_id = chunk.get("doc_id", "")
        section = chunk.get("section_title", "")
        title = chunk.get("title", "")

        # 从 RDS 查询同 section 的所有 chunk
        section_chunks = fetch_section_chunks(rds_conn, doc_id, section)

        window = []
        for sc in section_chunks:
            key = (sc["doc_id"], sc["chunk_index"])
            if key not in seen_keys:
                window.append(sc)
                seen_keys.add(key)

        if not window:
            key = (doc_id, chunk.get("chunk_index", 0))
            if key not in seen_keys:
                window.append(chunk)
                seen_keys.add(key)

        window.sort(key=lambda c: c.get("chunk_index", 0))
        window_text = "\n".join(c.get("chunk_text", "") for c in window)

        header = f"[{title}]"
        if section:
            header += f" > {section}"
        context_parts.append(f"{header}\n{window_text}")

    return "\n\n---\n\n".join(context_parts)


def strategy_parent_neighbor(hits: List[Dict], rds_conn) -> str:
    """策略 D: Parent + Neighbor 综合 — section 全量 + ±1 邻居扩展"""
    seen_keys = set()
    context_parts = []

    for chunk in hits:
        doc_id = chunk.get("doc_id", "")
        center_idx = chunk.get("chunk_index", 0)
        section = chunk.get("section_title", "")
        title = chunk.get("title", "")

        # 1. 先拿同 section 的全部 chunk
        section_chunks = fetch_section_chunks(rds_conn, doc_id, section)
        for sc in section_chunks:
            key = (sc["doc_id"], sc["chunk_index"])
            if key not in seen_keys:
                seen_keys.add(key)

        # 2. 再拿 ±1 邻居（可能跨 section 边界）
        neighbors = fetch_neighbor_chunks(rds_conn, doc_id, center_idx, NEIGHBOR_WINDOW)
        for n in neighbors:
            key = (n["doc_id"], n["chunk_index"])
            if key not in seen_keys:
                seen_keys.add(key)

        # 合并所有 chunk
        all_chunks = {(c["doc_id"], c["chunk_index"]): c
                      for c in section_chunks + neighbors}
        window = sorted(all_chunks.values(), key=lambda c: c.get("chunk_index", 0))
        window_text = "\n".join(c.get("chunk_text", "") for c in window)

        if not window_text.strip():
            window_text = chunk.get("chunk_text", "")

        header = f"[{title}]"
        if section:
            header += f" > {section}"
        context_parts.append(f"{header}\n{window_text}")

    return "\n\n---\n\n".join(context_parts)


# ═══════════════════════════════════════════════════════════════
# 主评测流程
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  Context Strategy Evaluation v2 — 生产 HA3 检索 + RDS 邻居扩展")
    print("=" * 70)

    # ── 1. 加载 eval queries ──
    print("\n[1/4] 加载评测 Query...")

    with open(QUERIES_PATH, "r", encoding="utf-8") as f:
        queries = json.load(f)
    print(f"  Queries: {len(queries)}")

    # ── 2. 初始化连接 ──
    print("\n[2/4] 初始化 HA3 + RDS 连接...")
    rds_conn = get_rds_conn()
    # search_chunks 内部会自动初始化 HA3 client
    print("  ✅ RDS 连接成功")

    # 测试 HA3
    test_results = search_chunks("测试连接", top_k=1)
    print(f"  ✅ HA3 连接成功 (test search returned {len(test_results)} results)")

    # ── 3. 逐 Query 评测 ──
    print(f"\n[3/4] 开始逐 Query 评测 ({len(queries)} queries)...")

    strategy_names = ["flat", "neighbor_stitch"]
    results = []

    # 聚合指标
    metrics_sum = {s: {"context_coverage": 0, "answer_completeness": 0,
                       "r_at_1": 0, "context_len": 0, "count": 0}
                   for s in strategy_names}

    for qi, q in enumerate(queries):
        query_text = q["query"]
        target_doc = q.get("target_doc_id", "")
        keyword_groups = q.get("required_keyword_groups", [])
        difficulty = q.get("difficulty", "unknown")
        category = q.get("category_name", "")
        dept = q.get("owner_dept")

        if (qi + 1) % 20 == 0 or qi == 0:
            print(f"\n  [{qi+1}/{len(queries)}] {query_text[:50]}...")

        # ── 3a. HA3 检索（一次检索，两种策略共享） ──
        try:
            hits = search_chunks(query_text, top_k=TOP_K, user_dept=dept)
        except Exception as e:
            print(f"    ⚠️ HA3 检索失败: {e}")
            continue

        if not hits:
            print("    ⚠️ 无检索结果，跳过")
            continue

        # R@1
        r_at_1 = 1 if hits[0].get("doc_id") == target_doc else 0

        # ── 3b. 构建两种策略的上下文 ──
        flat_context = strategy_flat(hits)
        neighbor_context = strategy_neighbor_stitch(hits, rds_conn)

        contexts = {"flat": flat_context, "neighbor_stitch": neighbor_context}

        # ── 3c. 确定性指标 ──
        q_result = {
            "query_id": qi,
            "query": query_text,
            "target_doc_id": target_doc,
            "target_title": q.get("target_title", ""),
            "difficulty": difficulty,
            "category_name": category,
            "owner_dept": q.get("owner_dept", ""),
            "category_l1": q.get("category_l1", ""),
            "keyword_groups": keyword_groups,
            "r_at_1": r_at_1,
            "top_hit_doc": hits[0].get("doc_id", ""),
            "top_hit_score": hits[0].get("score", 0),
            "strategies": {},
        }

        for sname in strategy_names:
            ctx = contexts[sname]
            ctx_coverage = compute_keyword_coverage(ctx, keyword_groups)
            q_result["strategies"][sname] = {
                "context_coverage": ctx_coverage,
                "context_len": len(ctx),
            }
            metrics_sum[sname]["context_coverage"] += ctx_coverage
            metrics_sum[sname]["r_at_1"] += r_at_1
            metrics_sum[sname]["context_len"] += len(ctx)
            metrics_sum[sname]["count"] += 1

        # ── 3d. LLM 回答 + Answer Completeness ──
        # 优化：当两种策略上下文完全相同时只调一次 LLM（同输入 → 同输出，公平且省资源）
        contexts_identical = (flat_context == neighbor_context)

        flat_ctx = flat_context[:6000] + "\n...[截断]..." if len(flat_context) > 6000 else flat_context
        flat_answer = call_llm_for_answer(query_text, flat_ctx)
        flat_completeness = compute_keyword_coverage(flat_answer, keyword_groups)

        q_result["strategies"]["flat"]["answer"] = flat_answer[:500]
        q_result["strategies"]["flat"]["answer_completeness"] = flat_completeness
        metrics_sum["flat"]["answer_completeness"] += flat_completeness

        if contexts_identical:
            # 上下文一致，复用 Flat 的 LLM 回答
            q_result["strategies"]["neighbor_stitch"]["answer"] = flat_answer[:500]
            q_result["strategies"]["neighbor_stitch"]["answer_completeness"] = flat_completeness
            metrics_sum["neighbor_stitch"]["answer_completeness"] += flat_completeness
        else:
            neighbor_ctx = neighbor_context[:6000] + "\n...[截断]..." if len(neighbor_context) > 6000 else neighbor_context
            neighbor_answer = call_llm_for_answer(query_text, neighbor_ctx)
            neighbor_completeness = compute_keyword_coverage(neighbor_answer, keyword_groups)
            q_result["strategies"]["neighbor_stitch"]["answer"] = neighbor_answer[:500]
            q_result["strategies"]["neighbor_stitch"]["answer_completeness"] = neighbor_completeness
            metrics_sum["neighbor_stitch"]["answer_completeness"] += neighbor_completeness

        q_result["contexts_identical"] = contexts_identical
        results.append(q_result)

        # 每 50 条保存一次
        if (qi + 1) % 50 == 0:
            identical_cnt = sum(1 for r in results if r.get("contexts_identical"))
            with open(RESULTS_PATH, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"    💾 已保存 {len(results)} 条 | 上下文相同: {identical_cnt} ({identical_cnt/len(results):.0%}) | LLM 省: {identical_cnt} 次")

    rds_conn.close()

    # ── 4. 汇总 & 报告 ──
    print(f"\n[4/4] 生成报告 ({len(results)} queries evaluated)...")

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"  详细结果 → {RESULTS_PATH}")

    report = generate_report(results, metrics_sum, strategy_names)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  报告 → {REPORT_PATH}")

    # Print summary
    print("\n" + "=" * 70)
    print("  评测结果摘要")
    print("=" * 70)

    header = f"{'Strategy':<20} {'Ctx Coverage':>13} {'Ans Complete':>13} {'R@1':>6} {'Avg Ctx Len':>12}"
    print(f"\n  {header}")
    print(f"  {'-' * len(header)}")

    for sname in strategy_names:
        m = metrics_sum[sname]
        n = max(m["count"], 1)
        print(f"  {sname:<20} "
              f"{m['context_coverage']/n:>12.1%} "
              f"{m['answer_completeness']/n:>12.1%} "
              f"{m['r_at_1']/n:>6.1%} "
              f"{m['context_len']/n:>11.0f}")

    # Improvement analysis
    improved = degraded = unchanged = 0
    for r in results:
        flat_cc = r["strategies"]["flat"]["context_coverage"]
        neighbor_cc = r["strategies"]["neighbor_stitch"]["context_coverage"]
        if neighbor_cc > flat_cc:
            improved += 1
        elif neighbor_cc < flat_cc:
            degraded += 1
        else:
            unchanged += 1

    n = max(len(results), 1)
    identical_cnt = sum(1 for r in results if r.get("contexts_identical"))
    print("\n  Neighbor Stitch vs Flat (Context Coverage):")
    print(f"    提升: {improved} ({improved/n:.1%})")
    print(f"    下降: {degraded} ({degraded/n:.1%})")
    print(f"    不变: {unchanged} ({unchanged/n:.1%})")
    print(f"    上下文完全相同: {identical_cnt} ({identical_cnt/n:.1%})")
    print(f"    LLM 调用: {n + (n - identical_cnt)} 次（省 {identical_cnt} 次）")


def generate_report(
    results: List[Dict], metrics_sum: Dict, strategy_names: List[str],
) -> str:
    """生成 Markdown 评测报告"""
    import statistics

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = len(results)

    report = f"""# Context Strategy Evaluation Report

**生成时间**: {now}
**评测 Query 数**: {total}
**检索方式**: HA3 混合检索 (Dense + Sparse + BM25 RRF)
**策略对比**: Flat Baseline vs Neighbor Stitching (±{NEIGHBOR_WINDOW} chunk)

---

## 1. 总体对比

| 指标 | Flat Baseline | Neighbor Stitch | 差异 |
|---|---|---|---|
"""
    for metric_name, metric_key in [
        ("Context Coverage", "context_coverage"),
        ("Answer Completeness", "answer_completeness"),
        ("R@1", "r_at_1"),
        ("Avg Context Length (chars)", "context_len"),
    ]:
        flat_val = metrics_sum["flat"][metric_key] / max(metrics_sum["flat"]["count"], 1)
        neighbor_val = metrics_sum["neighbor_stitch"][metric_key] / max(metrics_sum["neighbor_stitch"]["count"], 1)

        if metric_key == "context_len":
            diff = f"+{neighbor_val - flat_val:.0f}"
            report += f"| {metric_name} | {flat_val:.0f} | {neighbor_val:.0f} | {diff} |\n"
        else:
            diff_pct = (neighbor_val - flat_val) * 100
            sign = "+" if diff_pct >= 0 else ""
            report += f"| {metric_name} | {flat_val:.1%} | {neighbor_val:.1%} | {sign}{diff_pct:.1f}pp |\n"

    # Per-difficulty breakdown
    report += "\n---\n\n## 2. 按难度分类\n\n"
    for difficulty in ["single_chunk", "cross_chunk"]:
        subset = [r for r in results if r.get("difficulty") == difficulty]
        if not subset:
            continue

        report += f"### {difficulty} ({len(subset)} queries)\n\n"
        report += "| 指标 | Flat | Neighbor | Δ |\n|---|---|---|---|\n"

        for metric_key, label in [("context_coverage", "Ctx Coverage"), ("answer_completeness", "Ans Complete")]:
            flat_avg = sum(r["strategies"]["flat"][metric_key] for r in subset) / len(subset)
            neighbor_avg = sum(r["strategies"]["neighbor_stitch"][metric_key] for r in subset) / len(subset)
            diff = (neighbor_avg - flat_avg) * 100
            sign = "+" if diff >= 0 else ""
            report += f"| {label} | {flat_avg:.1%} | {neighbor_avg:.1%} | {sign}{diff:.1f}pp |\n"
        report += "\n"

    # Per-category breakdown (核心分析维度)
    report += "---\n\n## 3. 按 Query 类别分类\n\n"
    categories = sorted(set(r.get("category_name", "unknown") for r in results))
    report += "| 类别 | N | Flat CC | Neigh CC | Δ CC | Flat AC | Neigh AC | Δ AC |\n"
    report += "|---|---|---|---|---|---|---|---|\n"

    for cat in categories:
        subset = [r for r in results if r.get("category_name") == cat]
        if not subset:
            continue
        flat_cc = sum(r["strategies"]["flat"]["context_coverage"] for r in subset) / len(subset)
        neighbor_cc = sum(r["strategies"]["neighbor_stitch"]["context_coverage"] for r in subset) / len(subset)
        flat_ac = sum(r["strategies"]["flat"]["answer_completeness"] for r in subset) / len(subset)
        neighbor_ac = sum(r["strategies"]["neighbor_stitch"]["answer_completeness"] for r in subset) / len(subset)
        d_cc = (neighbor_cc - flat_cc) * 100
        d_ac = (neighbor_ac - flat_ac) * 100
        s_cc = "+" if d_cc >= 0 else ""
        s_ac = "+" if d_ac >= 0 else ""
        report += f"| {cat} | {len(subset)} | {flat_cc:.0%} | {neighbor_cc:.0%} | {s_cc}{d_cc:.1f}pp | {flat_ac:.0%} | {neighbor_ac:.0%} | {s_ac}{d_ac:.1f}pp |\n"

    # Top improvements
    report += "\n---\n\n## 4. Neighbor Stitching 改善最大的 Query (Top 10)\n\n"
    diffs = []
    for r in results:
        flat_cc = r["strategies"]["flat"]["context_coverage"]
        neighbor_cc = r["strategies"]["neighbor_stitch"]["context_coverage"]
        diffs.append((r, neighbor_cc - flat_cc))

    diffs.sort(key=lambda x: -x[1])
    report += "| Query | Target Doc | Flat CC | Neighbor CC | Δ |\n|---|---|---|---|---|\n"
    for r, diff in diffs[:10]:
        if diff <= 0:
            break
        query_short = r["query"][:50]
        title_short = r.get("target_title", "")[:30]
        flat_cc = r["strategies"]["flat"]["context_coverage"]
        neighbor_cc = r["strategies"]["neighbor_stitch"]["context_coverage"]
        report += f"| {query_short} | {title_short} | {flat_cc:.0%} | {neighbor_cc:.0%} | +{diff:.0%} |\n"

    # Context length analysis
    report += "\n---\n\n## 5. 上下文长度分析（Token 成本）\n\n"
    flat_lens = [r["strategies"]["flat"]["context_len"] for r in results]
    neighbor_lens = [r["strategies"]["neighbor_stitch"]["context_len"] for r in results]

    if flat_lens and neighbor_lens:
        flat_avg = statistics.mean(flat_lens)
        neighbor_avg = statistics.mean(neighbor_lens)
        report += "| 指标 | Flat | Neighbor | 增幅 |\n|---|---|---|---|\n"
        report += f"| 平均长度 (chars) | {flat_avg:.0f} | {neighbor_avg:.0f} | +{(neighbor_avg/max(flat_avg,1)-1)*100:.0f}% |\n"
        report += f"| 中位数 (chars) | {statistics.median(flat_lens):.0f} | {statistics.median(neighbor_lens):.0f} | — |\n"

    return report


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""feedback_miner.py — 反馈闭环挖掘（只读）：把 赞踩/转人工/错误 日志变成下一轮检索改进的工单。

数据源（fuling_operation 库，显式库名前缀，连接复用 RAG_RDS_* 环境变量）：
  qa_session_log（每问一行：query/answer/status/top_score/hit_count/latency/error）
  user_feedback（赞踩 + 评论）、escalation_ticket（转人工）

产出一份 markdown báo 告，按"可行动桶"归类（每桶对应一个明确的修复杠杆）：
  B1 检索空/低分 + 差评/转人工   → 语料缺口（对照 coverage_gap 的缺失文档清单）
  B2 高分检索 + 差评/转人工      → 排序错 or 答案质量（喂给 rerank/prompt 迭代）
  B3 NO_RESULT                  → 真缺口 or 召回失败（按 query 关键词聚类）
  B4 LLM_ERROR / RETRIEVAL_ERROR → 工程故障（按 error_message 聚类）
  B5 差评但带评论                → 人工逐条看（评论是最高信号）

用法：
  RAG_ENV=test python -m scripts.feedback_miner [--days 30] [--out docs/feedback_badcase_report.md]

注意 top_score 双分制：rerank 开启后 ~0-1（高≥0.9/中≥0.8），未开启时融合分 ~0-10
（高≥7.7/中≥5.8）。按 score>1.5 判定分制。
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eval_harness import envboot  # noqa: F401,E402

import pymysql  # noqa: E402


def _conn():
    return pymysql.connect(
        host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT", 3306)),
        user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


def _score_band(score):
    """统一双分制到 高/中/低 标签；None → unknown。"""
    if score is None:
        return "unknown"
    s = float(score)
    hi, mid = (0.9, 0.8) if s <= 1.5 else (7.7, 5.8)
    return "high" if s >= hi else ("medium" if s >= mid else "low")


_STOP = set("的了吗呢吧请问怎么如何什么哪些哪里是有在和与及个我你他她它们这那")


def _kw(q):
    """朴素关键词袋（去停用字的 2-gram），用于 NO_RESULT 聚类。"""
    q = re.sub(r"[^一-鿿A-Za-z0-9]", "", q or "")
    q = "".join(ch for ch in q if ch not in _STOP)
    return {q[i:i + 2] for i in range(len(q) - 1)} if len(q) > 1 else {q}


def fetch(days):
    conn = _conn()
    cur = conn.cursor()
    since = f"DATE_SUB(NOW(), INTERVAL {int(days)} DAY)"
    cur.execute(f"""
        SELECT q.message_id, q.user_dept, q.query_text, q.answer_text, q.answer_status,
               q.error_message, q.opensearch_hit_count, q.top_score, q.latency_ms,
               q.created_at, q.conversation_type,
               f.feedback_type, f.feedback_reason, f.feedback_comment,
               e.id IS NOT NULL AS escalated
        FROM fuling_operation.qa_session_log q
        LEFT JOIN fuling_operation.user_feedback f ON f.message_id = q.message_id
        LEFT JOIN fuling_operation.escalation_ticket e ON e.message_id = q.message_id
        WHERE q.created_at >= {since}
        ORDER BY q.created_at""")
    rows = cur.fetchall()
    conn.close()
    return rows


def bucketize(rows):
    b = {"B1_lowscore_negative": [], "B2_highscore_negative": [], "B3_no_result": [],
         "B4_errors": [], "B5_commented": []}
    for r in rows:
        negative = (r["feedback_type"] == "downvote") or r["escalated"]
        band = _score_band(r["top_score"])
        if r["answer_status"] in ("LLM_ERROR", "RETRIEVAL_ERROR"):
            b["B4_errors"].append(r)
        elif r["answer_status"] in ("NO_RESULT", "REFUSAL"):
            # NO_RESULT=检索空（缺语料）；REFUSAL=有候选但拒答（语料弱/未召回）——
            # 都是语料缺口信号，同入 B3（2026-06-12 起拒答型不再混在 SUCCESS 里）
            b["B3_no_result"].append(r)
        elif negative and band in ("low", "medium", "unknown"):
            b["B1_lowscore_negative"].append(r)
        elif negative:
            b["B2_highscore_negative"].append(r)
        if r["feedback_comment"]:
            b["B5_commented"].append(r)
    return b


def cluster_queries(rows, sim=0.5):
    """贪心聚类：2-gram Jaccard ≥ sim 归同簇。"""
    clusters = []
    for r in rows:
        ks = _kw(r["query_text"])
        for c in clusters:
            inter = len(ks & c["kw"]) / max(1, len(ks | c["kw"]))
            if inter >= sim:
                c["rows"].append(r)
                c["kw"] |= ks
                break
        else:
            clusters.append({"kw": set(ks), "rows": [r]})
    return sorted(clusters, key=lambda c: -len(c["rows"]))


def _row_line(r):
    q = (r["query_text"] or "")[:42].replace("|", "/")
    a = (r["answer_text"] or r["error_message"] or "")[:56].replace("|", "/").replace("\n", " ")
    fb = r["feedback_type"] or ("转人工" if r["escalated"] else "")
    return (f"| {r['created_at']:%m-%d} | {r['user_dept'] or '-'} | {q} | "
            f"{_score_band(r['top_score'])} {r['top_score'] if r['top_score'] is None else round(float(r['top_score']), 2)} | "
            f"{r['opensearch_hit_count']} | {fb} | {a} |")


_HDR = ("| 日期 | 部门 | 问题 | 分档 分数 | 命中 | 反馈 | 答案/错误摘录 |\n"
        "|---|---|---|---|---|---|---|")


def render(rows, b, days):
    L = [f"# 反馈闭环挖掘报告 — 近 {days} 天",
         f"\n生成: {datetime.now():%Y-%m-%d %H:%M} · 来源: fuling_operation（只读）\n"]
    n = len(rows)
    fb = Counter(r["feedback_type"] for r in rows if r["feedback_type"])
    st = Counter(r["answer_status"] for r in rows)
    n_esc = sum(1 for r in rows if r["escalated"])
    L.append(f"**总量:** {n} 次问答 · 反馈 {sum(fb.values())}（{dict(fb)}）· 转人工 {n_esc} · "
             f"状态 {dict(st)}\n")
    L.append("## 行动桶（每桶一个修复杠杆）\n")

    L.append(f"### B1 低/中分检索 + 差评/转人工 → 疑似语料缺口（{len(b['B1_lowscore_negative'])}）\n")
    L.append("对照 `eval_harness/reports/coverage_gap_findings.md` 的 ~7 份缺失文档；"
             "不在清单内的→新缺口，提请补充文档。\n")
    if b["B1_lowscore_negative"]:
        L.append(_HDR)
        L += [_row_line(r) for r in b["B1_lowscore_negative"]]

    L.append(f"\n### B2 高分检索 + 差评/转人工 → 排序/答案质量问题（{len(b['B2_highscore_negative'])}）\n")
    L.append("检索自信但用户不满：要么 top-1 排错（喂给 rerank 迭代做困难负例），"
             "要么答案生成丢点（看 answer 与 cited docs 是否对题）。\n")
    if b["B2_highscore_negative"]:
        L.append(_HDR)
        L += [_row_line(r) for r in b["B2_highscore_negative"]]

    L.append(f"\n### B3 NO_RESULT（{len(b['B3_no_result'])}）— 按主题聚类\n")
    for c in cluster_queries(b["B3_no_result"]):
        qs = "; ".join((r["query_text"] or "")[:30] for r in c["rows"][:4])
        L.append(f"- ×{len(c['rows'])}: {qs}")

    L.append(f"\n### B4 工程错误（{len(b['B4_errors'])}）— 按错误信息聚类\n")
    errs = Counter((r["error_message"] or "?")[:80] for r in b["B4_errors"])
    for msg, cnt in errs.most_common(10):
        L.append(f"- ×{cnt}: `{msg}`")

    L.append(f"\n### B5 带评论的反馈（{len(b['B5_commented'])}）— 逐条人工看\n")
    for r in b["B5_commented"]:
        L.append(f"- [{r['created_at']:%m-%d}] {r['query_text'][:36]} → "
                 f"**{r['feedback_type']}**: {(r['feedback_comment'] or '')[:80]}")

    L.append("\n## 用法提示\n")
    L.append("- 周更：`RAG_ENV=production python -m scripts.feedback_miner --days 7`（SAE 内或可达 RDS 处）。")
    L.append("- B2 桶攒够 ≥20 条后，把（query, 差评答案引用的 docs）作为困难负例集，"
             "加入 rerank 阈值/提示词迭代的评测集。")
    L.append("- top_score 双分制（rerank 0-1 / 融合 0-10）已自动归一为 高/中/低 分档。")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="docs/feedback_badcase_report.md")
    args = ap.parse_args()
    rows = fetch(args.days)
    b = bucketize(rows)
    md = render(rows, b, args.days)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"rows={len(rows)} buckets=" +
          json.dumps({k: len(v) for k, v in b.items()}) + f"\nsaved → {args.out}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""analyze_refusal_intents.py — 通用能力分级开放的 Step-0 定量分析（只读，PROD-RO）。

回答三个问题（docs/general_ability_opening_design.md §Step-0）：
  1. 拒答规模：近 N 天 NO_RESULT/REFUSAL 占比按日趋势；
  2. 拒答构成：拒答样本里有多少是【本不该进知识库】的（寒暄/办公/通识/实时）——
     这部分才是"开放通用能力"能救回的体验；企业相关未覆盖应继续拒答（只升级话术）；
  3. 体验伤害证据：拒答 × 差评（downvote）关联。

分类口径与线上同源：优先用 intent_router 的确定性词典/句式（零 LLM、可复算）粗分；
--llm-triage 时对"未知"桶追跑与线上同一份分诊 prompt（需 DashScope key，限速执行）。

用法（形态：只读 SQL 经 prod_access，严禁写；参照 scripts/audit_binding_coverage.py）:
  RAG_ENV=prod_ro python scripts/analyze_refusal_intents.py [--days 30] [--sample 500]
      [--llm-triage] [--out reports/refusal_intents_<date>.md]
  --sql-only  # 不连库，仅打印三段 SQL（供 DataWorks 手工执行后离线分析）

预注册决策规则（先注册后看数，防"看数定规则"）：
  可救回占比 = (smalltalk+office+general+realtime) / 全量问答
    < 5%   → 只上 RAG_GUIDED_REFUSAL（话术升级），暂缓通用层
    5–15%  → 推荐档 RAG_GENERAL_ABILITY_MODE=office（T1+T2）
    > 15% 且通识占比高 → 数据支撑下再议 T3（full）
"""

import argparse
import sys
import time
from collections import Counter

SQL_DAILY = """
SELECT DATE(created_at) AS d,
       COUNT(*) AS total,
       SUM(answer_status='NO_RESULT') AS no_result,
       SUM(answer_status='REFUSAL')  AS refusal,
       SUM(answer_status='BLOCKED')  AS blocked,
       ROUND(SUM(answer_status IN ('NO_RESULT','REFUSAL'))/COUNT(*), 4) AS refusal_rate
FROM fuling_operation.qa_session_log
WHERE created_at >= NOW() - INTERVAL %s DAY
GROUP BY d ORDER BY d
"""

SQL_SAMPLE = """
SELECT query_text, answer_status, user_dept, conversation_type,
       opensearch_hit_count, top_score
FROM fuling_operation.qa_session_log
WHERE answer_status IN ('NO_RESULT','REFUSAL')
  AND created_at >= NOW() - INTERVAL %s DAY
ORDER BY RAND() LIMIT %s
"""

SQL_FEEDBACK = """
SELECT l.answer_status, COUNT(*) AS cnt
FROM fuling_operation.qa_session_log l
JOIN fuling_operation.user_feedback f ON f.message_id = l.message_id
WHERE f.feedback_type = 'downvote'
  AND l.created_at >= NOW() - INTERVAL %s DAY
GROUP BY l.answer_status
"""


def classify_offline(query: str) -> str:
    """与线上同源的确定性粗分（intent_router 词典/句式；未知留给 --llm-triage）。"""
    from opensearch_pipeline.intent_router import (
        _OFFICE_PATTERNS,
        _REALTIME_PATTERNS,
        _SMALLTALK_MAX_LEN,
        _SMALLTALK_PATTERNS,
        has_company_signal,
        hits_sensitive,
    )
    q = (query or "").strip()
    if not q:
        return "unknown"
    if hits_sensitive(q):
        return "sensitive"
    if has_company_signal(q):
        return "enterprise"
    if any(p.search(q) for p in _REALTIME_PATTERNS):
        return "realtime"
    if len(q) <= _SMALLTALK_MAX_LEN and any(p.match(q) for _, p in _SMALLTALK_PATTERNS):
        return "smalltalk"
    if any(p.search(q) for p in _OFFICE_PATTERNS):
        return "office"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description="拒答意图构成分析（Step-0，只读）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--sample", type=int, default=500)
    ap.add_argument("--llm-triage", action="store_true",
                    help="对 unknown 桶跑线上同款分诊 prompt（需 key，~0.6s/条限速）")
    ap.add_argument("--sql-only", action="store_true", help="只打印 SQL 不连库")
    ap.add_argument("--out", default="", help="Markdown 报告输出路径（可选）")
    args = ap.parse_args()

    if args.sql_only:
        print("═══ (a) 拒答规模 ═══", SQL_DAILY % args.days, sep="\n")
        print("═══ (b) 拒答样本 ═══", SQL_SAMPLE % (args.days, args.sample), sep="\n")
        print("═══ (c) 拒答×差评 ═══", SQL_FEEDBACK % args.days, sep="\n")
        return 0

    from opensearch_pipeline.prod_access import get_prod_readonly_conn

    lines = [f"# 拒答意图构成分析（近 {args.days} 天，样本 {args.sample}）",
             "", "> 只读分析（PROD-RO）；分类口径与线上 intent_router 同源。", ""]
    conn = get_prod_readonly_conn()
    try:
        with conn.cursor() as cur:
            # (a) 规模
            cur.execute(SQL_DAILY, (args.days,))
            rows = cur.fetchall()
            lines += ["## (a) 拒答规模按日", "",
                      "| 日期 | 总量 | NO_RESULT | REFUSAL | BLOCKED | 拒答率 |",
                      "|---|---|---|---|---|---|"]
            total_all = refusal_all = 0
            for r in rows:
                lines.append(f"| {r['d']} | {r['total']} | {r['no_result']} | "
                             f"{r['refusal']} | {r.get('blocked') or 0} | {r['refusal_rate']} |")
                total_all += int(r["total"])
                refusal_all += int(r["no_result"]) + int(r["refusal"])
            overall = (refusal_all / total_all) if total_all else 0.0
            lines += ["", f"**总量 {total_all}，拒答 {refusal_all}（{overall:.1%}）**", ""]

            # (b) 构成
            cur.execute(SQL_SAMPLE, (args.days, args.sample))
            samples = cur.fetchall()
            buckets = Counter()
            unknown_qs = []
            for s in samples:
                b = classify_offline(s["query_text"] or "")
                buckets[b] += 1
                if b == "unknown":
                    unknown_qs.append(s["query_text"])
            if args.llm_triage and unknown_qs:
                from opensearch_pipeline.intent_router import triage_failed_query
                buckets.pop("unknown", None)
                for q in unknown_qs:
                    buckets[f"llm:{triage_failed_query(q)}"] += 1
                    time.sleep(0.6)   # 限速，别打爆配额
            n = len(samples) or 1
            lines += ["## (b) 拒答样本构成", "", "| 类别 | 条数 | 占样本 |", "|---|---|---|"]
            for b, c in buckets.most_common():
                lines.append(f"| {b} | {c} | {c / n:.1%} |")

            # 可救回占比外推（决策规则输入）：样本中非 enterprise/sensitive/unknown 的
            # 占比 × 全量拒答率 = 可救回问答占全流量比例
            rescuable = sum(c for b, c in buckets.items()
                            if b.replace("llm:", "") in
                            ("smalltalk", "office", "general", "realtime"))
            rescuable_traffic = (rescuable / n) * overall
            lines += ["", f"**可救回占样本 {rescuable / n:.1%} → 占全流量约 "
                          f"{rescuable_traffic:.1%}**", ""]
            verdict = ("只上 RAG_GUIDED_REFUSAL（话术升级），暂缓通用层"
                       if rescuable_traffic < 0.05 else
                       "推荐档 RAG_GENERAL_ABILITY_MODE=office（T1+T2）"
                       if rescuable_traffic <= 0.15 else
                       "office 起步；通识占比高则数据支撑下再议 T3")
            lines += [f"**预注册决策规则判定 → {verdict}**", ""]

            # (c) 差评关联
            cur.execute(SQL_FEEDBACK, (args.days,))
            lines += ["## (c) 拒答 × 差评", "", "| answer_status | 差评数 |", "|---|---|"]
            for r in cur.fetchall():
                lines.append(f"| {r['answer_status']} | {r['cnt']} |")
    finally:
        conn.close()

    report = "\n".join(lines)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\n报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

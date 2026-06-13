#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_figure_ref_phrases.py — 校准 mm_answer_metrics._FIGURE_REF_RE 漏覆盖率

口径:近 30 天 SUCCESS qa_session_log 答案里,所有"图"字附近 8 字窗口高频串,
     与现行 _FIGURE_REF_RE 命中清单对照,列出真实漏字。
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples/scripts"))

from mm_answer_metrics import _FIGURE_REF_RE


def main():
    from opensearch_pipeline.prod_access import get_prod_readonly_conn

    conn = get_prod_readonly_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT answer_text
            FROM fuling_operation.qa_session_log
            WHERE answer_status='SUCCESS'
              AND created_at >= NOW() - INTERVAL 30 DAY
              AND answer_text LIKE %s
            LIMIT 5000
        """, ('%图%',))
        rows = cur.fetchall()
    conn.close()

    print(f"扫描答案数: {len(rows)}")

    # 抓所有"图"附近 ±4 字窗口
    counter: Counter[str] = Counter()
    re_tu = re.compile(r"(?:.{0,4}图.{0,4})")
    for r in rows:
        t = r["answer_text"] or ""
        for m in re_tu.findall(t):
            # 去重首尾空白和标点干扰
            s = m.strip()
            if 3 <= len(s) <= 12:
                counter[s] += 1

    # 按真实出现频次取 top
    top = counter.most_common(120)

    print(f"\n── '图' 附近 8 字窗口 top 120 (按频次) ──")
    not_covered = []
    covered = []
    for phrase, n in top:
        # 只看是否命中 _FIGURE_REF_RE(即 dangling 检测会用这条短语)
        if _FIGURE_REF_RE.search(phrase):
            covered.append((phrase, n))
        else:
            not_covered.append((phrase, n))

    print(f"\n✓ 已被正则命中 ({len(covered)} 条):")
    for p, n in covered[:30]:
        print(f"   {n:6d}  {p}")

    print(f"\n✗ 未被命中但高频 ({len(not_covered)} 条 top 50):")
    for p, n in not_covered[:50]:
        print(f"   {n:6d}  {p}")

    # 智能筛:从 not_covered 里只挑出像"图指代"语义的(含"图"字 + 动词/位置词)
    fig_indicator = re.compile(r"(参见|参阅|参考|看|示意|附图|图片|图示|附|可见|位于|查看|展示|显示)")
    candidate_misses = [(p, n) for p, n in not_covered if fig_indicator.search(p) and p.count("图") >= 1]
    print(f"\n🔴 疑似漏覆盖的图指代语义 ({len(candidate_misses)} 条 top 30):")
    for p, n in candidate_misses[:30]:
        print(f"   {n:6d}  {p}")


if __name__ == "__main__":
    main()

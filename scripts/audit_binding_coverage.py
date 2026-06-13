#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_binding_coverage.py — 生产 step_card binding 覆盖率仪表板(只读)

输出:
  1. 逐格式表 — docs / docs_with_step_card / docs_with_image_refs /
                step_cards / step_cards_with_refs / 覆盖率
  2. 抽样 5 个 doc_id(各格式良绑/差绑/零 step_card 样本)

形态:只读 SQL 经 prod_access,严禁写;无 GT 依赖,仅看现状分布。
用途:Day 2 决定 binding_jaccard_pdf 等阈值时,据本表真实分布校准而不是纸面拍。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def fmt_pct(num: int, den: int) -> str:
    if not den:
        return "n/a"
    return f"{100 * num / den:.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "scratch", "binding_audit_20260612.md"),
                    help="输出 markdown 文件路径")
    ap.add_argument("--quiet", action="store_true", help="只写文件不打印表")
    args = ap.parse_args()

    from opensearch_pipeline.prod_access import get_prod_readonly_conn
    conn = get_prod_readonly_conn()

    # ── Q1:逐格式覆盖率(主表)─────────────────────────────────
    # 口径:用 document_version.file_ext (file_ext 直接给后缀);
    # 取每 doc 的"当前生效版本"(document_meta.current_version_no = document_version.version_no);
    # 排除 status != 'active' 的文档(已退役/审核未过);
    # image_refs_json 非空 = NOT NULL AND NOT IN ('[]', 'null', '')。
    sql_main = """
    SELECT LOWER(IFNULL(dv.file_ext, 'unknown'))                                                     AS fmt,
           COUNT(DISTINCT dm.doc_id)                                                                 AS docs,
           COUNT(DISTINCT CASE WHEN cm.chunk_type = 'step_card' THEN dm.doc_id END)                  AS docs_with_step,
           COUNT(DISTINCT CASE WHEN cm.image_refs_json IS NOT NULL
                                AND cm.image_refs_json NOT IN ('[]', 'null', '') THEN dm.doc_id END) AS docs_with_refs,
           SUM(CASE WHEN cm.chunk_type = 'step_card' THEN 1 ELSE 0 END)                              AS step_cards,
           SUM(CASE WHEN cm.chunk_type = 'step_card'
                     AND cm.image_refs_json IS NOT NULL
                     AND cm.image_refs_json NOT IN ('[]', 'null', '') THEN 1 ELSE 0 END)             AS step_cards_with_refs
    FROM fuling_knowledge.document_meta dm
    JOIN fuling_knowledge.document_version dv
      ON dv.doc_id = dm.doc_id AND dv.version_no = dm.current_version_no
    LEFT JOIN fuling_knowledge.chunk_meta cm
      ON cm.doc_id = dm.doc_id AND cm.is_active = 1
    WHERE dm.status = 'active'
    GROUP BY fmt
    ORDER BY docs DESC
    """

    with conn.cursor() as cur:
        cur.execute(sql_main)
        rows = list(cur.fetchall())

    # ── Q2:每格式抽样 — 良绑(step_card_with_refs 最多)+ 差绑(有 step_card 但全无图)+ 零 step_card ──
    samples_by_fmt: dict[str, dict] = {}
    for r in rows:
        fmt = r["fmt"]
        if fmt in ("unknown", None):
            continue
        with conn.cursor() as cur:
            # 良绑:step_card_with_refs >= 3 ORDER BY DESC LIMIT 1
            cur.execute(
                f"""
                SELECT dm.doc_id, dm.original_filename, dm.title,
                       SUM(CASE WHEN cm.chunk_type='step_card' THEN 1 ELSE 0 END) AS step_cards,
                       SUM(CASE WHEN cm.chunk_type='step_card'
                                 AND cm.image_refs_json IS NOT NULL
                                 AND cm.image_refs_json NOT IN ('[]','null','') THEN 1 ELSE 0 END) AS step_with_refs
                FROM fuling_knowledge.document_meta dm
                JOIN fuling_knowledge.document_version dv
                  ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no
                JOIN fuling_knowledge.chunk_meta cm
                  ON cm.doc_id=dm.doc_id AND cm.is_active=1
                WHERE dm.status='active' AND LOWER(IFNULL(dv.file_ext,''))=%s
                GROUP BY dm.doc_id
                HAVING step_with_refs >= 3
                ORDER BY step_with_refs DESC
                LIMIT 1
                """, (fmt,))
            well = cur.fetchone()

            # 差绑:有 step_card 但全无 image_refs
            cur.execute(
                f"""
                SELECT dm.doc_id, dm.original_filename, dm.title,
                       SUM(CASE WHEN cm.chunk_type='step_card' THEN 1 ELSE 0 END) AS step_cards,
                       SUM(CASE WHEN cm.chunk_type='step_card'
                                 AND cm.image_refs_json IS NOT NULL
                                 AND cm.image_refs_json NOT IN ('[]','null','') THEN 1 ELSE 0 END) AS step_with_refs
                FROM fuling_knowledge.document_meta dm
                JOIN fuling_knowledge.document_version dv
                  ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no
                JOIN fuling_knowledge.chunk_meta cm
                  ON cm.doc_id=dm.doc_id AND cm.is_active=1
                WHERE dm.status='active' AND LOWER(IFNULL(dv.file_ext,''))=%s
                GROUP BY dm.doc_id
                HAVING step_cards > 0 AND step_with_refs = 0
                ORDER BY step_cards DESC
                LIMIT 1
                """, (fmt,))
            poor = cur.fetchone()

            # 零 step_card:有 chunk 但完全没走 step 模式
            cur.execute(
                f"""
                SELECT dm.doc_id, dm.original_filename, dm.title, COUNT(*) AS chunks
                FROM fuling_knowledge.document_meta dm
                JOIN fuling_knowledge.document_version dv
                  ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no
                JOIN fuling_knowledge.chunk_meta cm
                  ON cm.doc_id=dm.doc_id AND cm.is_active=1
                WHERE dm.status='active' AND LOWER(IFNULL(dv.file_ext,''))=%s
                  AND dm.doc_id NOT IN (
                    SELECT DISTINCT doc_id FROM fuling_knowledge.chunk_meta
                    WHERE is_active=1 AND chunk_type='step_card'
                  )
                GROUP BY dm.doc_id
                ORDER BY chunks DESC
                LIMIT 1
                """, (fmt,))
            zero = cur.fetchone()

        samples_by_fmt[fmt] = {"well": well, "poor": poor, "zero": zero}

    conn.close()

    # ── 渲染 markdown ──────────────────────────────────────────
    L: list[str] = []
    L.append("# 生产 binding 覆盖率仪表板（Day 1 Step 1）\n")
    L.append(f"- **生成时间:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    L.append(f"- **形态:** 只读,直连生产 RDS(rm-bp15j7wekd5738f093o…)")
    L.append(f"- **口径:** document_meta JOIN document_version(当前生效版本) LEFT JOIN chunk_meta(is_active=1)")
    L.append(f"- **筛选:** document_meta.status='active'(已退役不计)\n")

    # 表 1:逐格式
    L.append("## 1) 逐格式覆盖率\n")
    L.append("| fmt | docs | docs_with_step_card | docs_with_image_refs | step_cards | step_cards_with_refs | step_card 覆盖率 | image_refs 覆盖率 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    total = {"docs": 0, "docs_with_step": 0, "docs_with_refs": 0,
             "step_cards": 0, "step_cards_with_refs": 0}
    for r in rows:
        for k in total:
            total[k] += int(r[k] or 0)
        step_cov = fmt_pct(int(r["docs_with_step"]), int(r["docs"]))
        ref_cov = fmt_pct(int(r["step_cards_with_refs"]), int(r["step_cards"]))
        L.append(f"| {r['fmt']} | {r['docs']} | {r['docs_with_step']} | {r['docs_with_refs']} | "
                 f"{r['step_cards']} | {r['step_cards_with_refs']} | {step_cov} | {ref_cov} |")
    L.append(f"| **TOTAL** | **{total['docs']}** | **{total['docs_with_step']}** | "
             f"**{total['docs_with_refs']}** | **{total['step_cards']}** | "
             f"**{total['step_cards_with_refs']}** | "
             f"**{fmt_pct(total['docs_with_step'], total['docs'])}** | "
             f"**{fmt_pct(total['step_cards_with_refs'], total['step_cards'])}** |")

    # 表 2:抽样
    L.append("\n## 2) 每格式抽样 doc_id\n")
    L.append("| fmt | 良绑(step_with_refs ↓) | 差绑(有 step 无图) | 零 step_card |")
    L.append("|---|---|---|---|")

    def cell(s):
        if not s:
            return "—"
        fn = (s.get("original_filename") or s.get("title") or s.get("doc_id"))[:40]
        sc = s.get("step_cards")
        sr = s.get("step_with_refs")
        ck = s.get("chunks")
        meta_bits = []
        if sc is not None: meta_bits.append(f"step={sc}")
        if sr is not None: meta_bits.append(f"with_img={sr}")
        if ck is not None: meta_bits.append(f"chunks={ck}")
        return f"`{s['doc_id'][-20:]}` ({fn})<br>{' '.join(meta_bits)}"

    for fmt, smp in samples_by_fmt.items():
        L.append(f"| **{fmt}** | {cell(smp['well'])} | {cell(smp['poor'])} | {cell(smp['zero'])} |")

    md = "\n".join(L) + "\n"

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    open(args.out, "w").write(md)

    if not args.quiet:
        print(md)
    print(f"\n✓ 已写 {args.out}")


if __name__ == "__main__":
    main()

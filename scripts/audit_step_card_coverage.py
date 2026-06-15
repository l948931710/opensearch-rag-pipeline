#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_step_card_coverage.py — HA3 step_card 七维覆盖率审计（prod_ro 只读）

D1 RDS↔HA3 active step_card 数对账（chunk_id set-diff）
D2 image_refs 覆盖率（按 file_ext 分桶）
D3 SOP 路由命中但 0 step_card 的候选漏 chunk 名单（cat/title 关键字侧近似）
D4 孤儿 step_card（parent_chunk_id 无法解析到 active procedure_parent）
D5 step_no 连续性（max - distinct gap）
D6 image_refs JSON shape 合规率（oss_key/image_index/xlsx anchor_row）
D7 procedure_parent 平衡（每文档应有且仅有 1 个 active procedure_parent）

Usage:
  RAG_ENV=prod_ro RAG_READONLY=true \\
  RAG_ALLOW_REMOTE_DB=read_only_ack RAG_ALLOW_REMOTE_SEARCH=read_only_ack \\
    python scripts/audit_step_card_coverage.py --out docs/audits/ha3_step_card_coverage_2026-06-14.md

只读约束：走 opensearch_pipeline.prod_access.get_prod_readonly_conn()（SET SESSION
TRANSACTION READ ONLY），HA3 走 retriever._get_ha3_client() 的 query()（只读 API）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ──────────────────────────────────────────────────────────────
# 辅助
# ──────────────────────────────────────────────────────────────

def pct(num: int, den: int) -> str:
    return "n/a" if not den else f"{100.0 * num / den:.2f}%"


def section(title: str) -> str:
    return f"\n## {title}\n"


# ──────────────────────────────────────────────────────────────
# D1 — RDS↔HA3 active step_card drift
# ──────────────────────────────────────────────────────────────

def d1_rds_count_and_ids(conn) -> Tuple[int, set, int, int]:
    """返回 (active_count, chunk_id_set, indexed_count, version_success_count)。

    - active_count: chunk_meta WHERE chunk_type='step_card' AND is_active=1
    - chunk_id_set: 同上的 chunk_id 集合（用于 set-diff）
    - indexed_count: 同上 AND chunk_meta.index_status='INDEXED'（HA3 push 真完成）
    - version_success_count: 同上 AND document_version.index_status='SUCCESS'（更宽屏障）
    """
    with conn.cursor() as cur:
        cur.execute("""
          SELECT chunk_id, doc_id, version_no, index_status
          FROM fuling_knowledge.chunk_meta
          WHERE chunk_type='step_card' AND is_active=1
        """)
        rows = list(cur.fetchall())
        active_count = len(rows)
        chunk_id_set = {r["chunk_id"] for r in rows}
        indexed_count = sum(1 for r in rows if r["index_status"] == "INDEXED")

        cur.execute("""
          SELECT COUNT(*) AS n
          FROM fuling_knowledge.chunk_meta cm
          JOIN fuling_knowledge.document_version dv
            ON dv.doc_id=cm.doc_id AND dv.version_no=cm.version_no
          WHERE cm.chunk_type='step_card' AND cm.is_active=1
            AND dv.index_status='SUCCESS'
        """)
        version_success_count = int(cur.fetchone()["n"])

    return active_count, chunk_id_set, indexed_count, version_success_count


def d1_ha3_step_card_ids(top_k: int = 10000) -> Tuple[set, int, Optional[str]]:
    """从 HA3 拉所有 chunk_type='step_card' 的 chunk_id 集合。

    策略：用零向量 + filter 一次性 top_k 大查（HA3 滤前剪枝，分数不重要）。
    若返回 == top_k，提示可能截断；这时回退到按 doc_id 前缀分桶或采样。
    返回 (chunk_id_set, total_returned, warning_or_None)。
    """
    from opensearch_pipeline.retriever import _get_ha3_client, _parse_ha3_response
    from opensearch_pipeline.config import get_config
    from alibabacloud_ha3engine_vector.models import QueryRequest

    cfg = get_config().alibaba_vector
    client = _get_ha3_client()

    # 1024 维零向量 — 配合 filter，HA3 滤前剪枝，分数不重要
    zero_vec = [0.0] * 1024

    warning = None
    req = QueryRequest(
        table_name=cfg.table_name,
        vector=zero_vec,
        top_k=top_k,
        include_vector=False,
        output_fields=["chunk_id", "doc_id", "version_no", "chunk_type", "is_active"],
        filter='chunk_type="step_card"',
    )
    t0 = time.time()
    resp = client.query(req)
    results = _parse_ha3_response(resp)
    dt = time.time() - t0

    chunk_ids = {r.get("chunk_id") for r in results if r.get("chunk_id")}
    returned = len(results)
    print(f"[D1] HA3 step_card query: top_k={top_k}, returned={returned}, dt={dt:.1f}s")

    if returned >= top_k:
        warning = (f"HA3 returned {returned} >= top_k {top_k} — 可能截断！"
                   f"建议改用分桶/采样策略。本次结果仅作下界。")

    return chunk_ids, returned, warning


# ──────────────────────────────────────────────────────────────
# D2 — image_refs coverage by file_ext
# ──────────────────────────────────────────────────────────────

def d2_image_binding_by_ext(conn) -> List[Dict[str, Any]]:
    """逐 ext: docs / docs_with_step / step_cards / step_cards_with_refs。"""
    with conn.cursor() as cur:
        cur.execute("""
          SELECT LOWER(IFNULL(dv.file_ext, 'unknown')) AS fmt,
                 COUNT(DISTINCT dm.doc_id) AS docs,
                 COUNT(DISTINCT CASE WHEN cm.chunk_type='step_card' THEN dm.doc_id END) AS docs_with_step,
                 SUM(CASE WHEN cm.chunk_type='step_card' THEN 1 ELSE 0 END) AS step_cards,
                 SUM(CASE WHEN cm.chunk_type='step_card'
                           AND cm.image_refs_json IS NOT NULL
                           AND cm.image_refs_json NOT IN ('[]','null','') THEN 1 ELSE 0 END) AS step_cards_with_refs
          FROM fuling_knowledge.document_meta dm
          JOIN fuling_knowledge.document_version dv
            ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no
          LEFT JOIN fuling_knowledge.chunk_meta cm
            ON cm.doc_id=dm.doc_id AND cm.is_active=1
          WHERE dm.status='active'
          GROUP BY fmt
          ORDER BY docs DESC
        """)
        return list(cur.fetchall())


# ──────────────────────────────────────────────────────────────
# D3 — SOP 路由命中但 0 step_card 的候选漏 chunk 名单
# ──────────────────────────────────────────────────────────────

# 与 pipeline_nodes.py:1612-1668 _detect_step_patterns 的 cat/title 侧关键字对齐
# 注意：runtime 还有 _STEP_DETECT_RE >=2 文本侧检查，SQL 无法复刻 → 候选名单非定罪
_D3_ROUTED_SQL = """
SELECT dm.doc_id, dm.title, dm.category_l1, dm.category_l2,
       dv.file_ext, dm.original_filename
FROM fuling_knowledge.document_meta dm
JOIN fuling_knowledge.document_version dv
  ON dv.doc_id=dm.doc_id AND dv.version_no=dm.current_version_no
WHERE dm.status='active'
  AND (
       LOWER(IFNULL(dm.category_l1,'')) REGEXP '(sop|manual|guide)'
    OR LOWER(IFNULL(dm.category_l2,'')) REGEXP '(sop|manual|guide)'
    OR LOWER(IFNULL(dm.title,''))        REGEXP '(sop|manual|guide|wi[-_ ]?[0-9])'
    OR dm.category_l1 LIKE '%操作%' OR dm.category_l2 LIKE '%操作%' OR dm.title LIKE '%操作%'
    OR dm.category_l1 LIKE '%手册%' OR dm.category_l2 LIKE '%手册%' OR dm.title LIKE '%手册%'
    OR dm.title LIKE '%作业指导%'
  )
  AND NOT (LOWER(IFNULL(dm.category_l1,'')) LIKE '%faq%'
        OR LOWER(IFNULL(dm.category_l2,'')) LIKE '%faq%')
  AND NOT (LOWER(IFNULL(dm.category_l1,'')) REGEXP '(policy|standard|regulation)'
        OR LOWER(IFNULL(dm.category_l2,'')) REGEXP '(policy|standard|regulation)'
        OR dm.title LIKE '%制度%' OR dm.title LIKE '%规定%' OR dm.title LIKE '%规范%')
"""


def d3_routed_zero_step_card(conn) -> Tuple[int, int, List[Dict[str, Any]]]:
    """返回 (routed_total, routed_with_step, miss_list)。"""
    with conn.cursor() as cur:
        cur.execute(_D3_ROUTED_SQL)
        routed = list(cur.fetchall())
    routed_total = len(routed)

    miss_list = []
    routed_with_step = 0
    with conn.cursor() as cur:
        for r in routed:
            cur.execute("""
              SELECT
                SUM(CASE WHEN chunk_type='step_card' THEN 1 ELSE 0 END) AS step_cards,
                COUNT(*) AS total_chunks
              FROM fuling_knowledge.chunk_meta
              WHERE doc_id=%s AND is_active=1
            """, (r["doc_id"],))
            row = cur.fetchone()
            sc = int(row["step_cards"] or 0)
            tc = int(row["total_chunks"] or 0)
            if sc > 0:
                routed_with_step += 1
            elif tc > 0:
                miss_list.append({**r, "total_chunks": tc})
    return routed_total, routed_with_step, miss_list


# ──────────────────────────────────────────────────────────────
# D4 — 孤儿 step_card
# ──────────────────────────────────────────────────────────────

def d4_orphan_step_cards(conn) -> Tuple[int, Counter, List[Dict[str, Any]]]:
    """返回 (orphan_count, reason_counter, top20_sample)。"""
    with conn.cursor() as cur:
        cur.execute("""
          SELECT sc.chunk_id AS step_card_id, sc.doc_id, sc.version_no,
                 sc.parent_chunk_id, sc.step_no,
                 CASE
                   WHEN sc.parent_chunk_id IS NULL THEN 'NULL_PARENT'
                   WHEN pp.chunk_id IS NULL THEN 'PARENT_NOT_FOUND'
                   WHEN pp.is_active = 0 THEN 'PARENT_DEACTIVATED'
                   WHEN pp.chunk_type <> 'procedure_parent' THEN CONCAT('PARENT_WRONG_TYPE:', pp.chunk_type)
                   ELSE 'OK'
                 END AS orphan_reason
          FROM fuling_knowledge.chunk_meta sc
          LEFT JOIN fuling_knowledge.chunk_meta pp
            ON pp.chunk_id = sc.parent_chunk_id
          WHERE sc.chunk_type='step_card' AND sc.is_active=1
          HAVING orphan_reason <> 'OK'
        """)
        rows = list(cur.fetchall())
    counter = Counter(r["orphan_reason"] for r in rows)
    return len(rows), counter, rows[:20]


# ──────────────────────────────────────────────────────────────
# D5 — step_no 连续性
# ──────────────────────────────────────────────────────────────

def d5_step_continuity(conn) -> Tuple[int, int, List[Dict[str, Any]]]:
    """返回 (total_parents, gap_parents, top20_gap_sample)。

    注意：子步 3.1/3.2 都映射 step_no=3，distinct < total 是合法情况；
    用 max(step_no) - count(distinct step_no) 检测连续性 gap。
    """
    with conn.cursor() as cur:
        cur.execute("""
          SELECT sc.parent_chunk_id, sc.doc_id,
                 MIN(sc.step_no) AS min_step,
                 MAX(sc.step_no) AS max_step,
                 COUNT(DISTINCT sc.step_no) AS distinct_steps,
                 COUNT(*) AS total_cards
          FROM fuling_knowledge.chunk_meta sc
          WHERE sc.chunk_type='step_card' AND sc.is_active=1
            AND sc.step_no IS NOT NULL
            AND sc.parent_chunk_id IS NOT NULL
          GROUP BY sc.parent_chunk_id, sc.doc_id
        """)
        rows = list(cur.fetchall())

    total = len(rows)
    gaps = [
        {**r, "missing_count": int(r["max_step"] or 0) - int(r["distinct_steps"] or 0)}
        for r in rows
        if (int(r["max_step"] or 0) - int(r["distinct_steps"] or 0)) > 0
           or int(r["min_step"] or 0) != 1
    ]
    # null step_no 也统计一下作为补充诊断
    with conn.cursor() as cur:
        cur.execute("""
          SELECT COUNT(*) AS n
          FROM fuling_knowledge.chunk_meta
          WHERE chunk_type='step_card' AND is_active=1 AND step_no IS NULL
        """)
        null_step_no = int(cur.fetchone()["n"])

    gaps.sort(key=lambda r: r["missing_count"], reverse=True)
    return total, len(gaps), gaps[:20], null_step_no  # type: ignore


# ──────────────────────────────────────────────────────────────
# D6 — image_refs JSON shape 合规
# ──────────────────────────────────────────────────────────────

def d6_image_ref_shape(conn) -> Dict[str, Any]:
    """逐 chunk parse image_refs_json，验证 oss_key / image_index / xlsx 的 (filename, anchor_row)。"""
    with conn.cursor() as cur:
        cur.execute("""
          SELECT cm.chunk_id, cm.doc_id, cm.version_no,
                 LOWER(IFNULL(dv.file_ext, 'unknown')) AS file_ext,
                 cm.image_refs_json
          FROM fuling_knowledge.chunk_meta cm
          JOIN fuling_knowledge.document_version dv
            ON dv.doc_id=cm.doc_id AND dv.version_no=cm.version_no
          WHERE cm.chunk_type='step_card' AND cm.is_active=1
            AND cm.image_refs_json IS NOT NULL
            AND cm.image_refs_json NOT IN ('[]','null','')
        """)
        rows = list(cur.fetchall())

    total_chunks = len(rows)
    total_entries = 0
    bad_json_chunks = 0
    chunk_compliant_all = 0          # 整 chunk 所有 entry 都合规
    entry_oss_key_present = 0
    entry_image_index_present = 0
    entry_source_image_present = 0
    entry_visual_summary_present = 0
    xlsx_entries = 0
    xlsx_anchor_row_present = 0
    xlsx_filename_present = 0
    per_ext = Counter()
    per_ext_compliant = Counter()
    bad_chunk_examples: List[Dict[str, Any]] = []

    for r in rows:
        ext = r["file_ext"]
        per_ext[ext] += 1
        try:
            raw = r["image_refs_json"]
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            if isinstance(raw, str):
                refs = json.loads(raw)
            else:
                refs = raw
            if not isinstance(refs, list):
                refs = [refs]
        except Exception:
            bad_json_chunks += 1
            if len(bad_chunk_examples) < 5:
                bad_chunk_examples.append({"chunk_id": r["chunk_id"], "reason": "bad_json"})
            continue

        all_ok = True
        for ent in refs:
            if not isinstance(ent, dict):
                all_ok = False
                continue
            total_entries += 1
            has_oss = bool(str(ent.get("oss_key") or "").strip())
            has_idx = isinstance(ent.get("image_index"), int)
            has_src = bool(str(ent.get("source_image") or "").strip())
            has_vs = bool(str(ent.get("visual_summary") or "").strip())
            entry_oss_key_present += int(has_oss)
            entry_image_index_present += int(has_idx)
            entry_source_image_present += int(has_src)
            entry_visual_summary_present += int(has_vs)

            entry_ok = has_oss and has_idx
            if ext == "xlsx":
                xlsx_entries += 1
                has_fn = bool(str(ent.get("filename") or "").strip())
                has_ar = ent.get("anchor_row") is not None
                xlsx_filename_present += int(has_fn)
                xlsx_anchor_row_present += int(has_ar)
                entry_ok = entry_ok and has_fn and has_ar
            if not entry_ok:
                all_ok = False

        if all_ok and refs:
            chunk_compliant_all += 1
            per_ext_compliant[ext] += 1
        elif len(bad_chunk_examples) < 10:
            missing_bits = []
            for ent in refs:
                if not isinstance(ent, dict):
                    continue
                if not str(ent.get("oss_key") or "").strip():
                    missing_bits.append("oss_key")
                if not isinstance(ent.get("image_index"), int):
                    missing_bits.append("image_index")
                if ext == "xlsx":
                    if not str(ent.get("filename") or "").strip():
                        missing_bits.append("xlsx.filename")
                    if ent.get("anchor_row") is None:
                        missing_bits.append("xlsx.anchor_row")
            if missing_bits:
                bad_chunk_examples.append({
                    "chunk_id": r["chunk_id"], "file_ext": ext,
                    "missing": ",".join(sorted(set(missing_bits))),
                })

    return {
        "total_chunks": total_chunks,
        "total_entries": total_entries,
        "bad_json_chunks": bad_json_chunks,
        "chunk_compliant_all": chunk_compliant_all,
        "entry_oss_key_present": entry_oss_key_present,
        "entry_image_index_present": entry_image_index_present,
        "entry_source_image_present": entry_source_image_present,
        "entry_visual_summary_present": entry_visual_summary_present,
        "xlsx_entries": xlsx_entries,
        "xlsx_filename_present": xlsx_filename_present,
        "xlsx_anchor_row_present": xlsx_anchor_row_present,
        "per_ext": dict(per_ext),
        "per_ext_compliant": dict(per_ext_compliant),
        "bad_chunk_examples": bad_chunk_examples,
    }


# ──────────────────────────────────────────────────────────────
# D7 — procedure_parent 平衡
# ──────────────────────────────────────────────────────────────

def d7_parent_balance(conn) -> Tuple[int, int, List[Dict[str, Any]]]:
    """返回 (missing_parent_docs, duplicate_parent_docs, top20_anomaly)。"""
    with conn.cursor() as cur:
        cur.execute("""
          SELECT doc_id, version_no,
                 SUM(CASE WHEN chunk_type='step_card' THEN 1 ELSE 0 END) AS step_card_count,
                 SUM(CASE WHEN chunk_type='procedure_parent' THEN 1 ELSE 0 END) AS procedure_parent_count
          FROM fuling_knowledge.chunk_meta
          WHERE is_active=1
          GROUP BY doc_id, version_no
          HAVING step_card_count > 0 AND procedure_parent_count <> 1
          ORDER BY step_card_count DESC
        """)
        rows = list(cur.fetchall())
    missing = sum(1 for r in rows if int(r["procedure_parent_count"] or 0) == 0)
    duplicate = sum(1 for r in rows if int(r["procedure_parent_count"] or 0) > 1)
    return missing, duplicate, rows[:20]


# ──────────────────────────────────────────────────────────────
# 报告渲染
# ──────────────────────────────────────────────────────────────

def render_markdown(payload: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append("# HA3 step_card 覆盖率审计\n")
    L.append(f"- **生成时间**: {payload['generated_at']}")
    L.append(f"- **形态**: 只读 — `RAG_ENV=prod_ro`, `SET SESSION TRANSACTION READ ONLY`")
    L.append(f"- **RDS**: `{payload.get('rds_host_hint','(see banner)')}` / db=`fuling_knowledge`")
    L.append(f"- **HA3 table**: `{payload.get('ha3_table_name','(see banner)')}`")
    L.append("")

    # ── D1 ──
    d1 = payload["D1"]
    L.append(section("D1 — RDS↔HA3 active step_card drift"))
    L.append(f"- RDS active step_card 数: **{d1['rds_active']}**")
    L.append(f"  - 其中 `chunk_meta.index_status='INDEXED'`: **{d1['rds_indexed']}**")
    L.append(f"  - 其中 `document_version.index_status='SUCCESS'`: **{d1['rds_version_success']}**")
    L.append(f"- HA3 返回 chunk_id（filter chunk_type='step_card'）: **{d1['ha3_returned']}**")
    L.append(f"- HA3 unique chunk_id: **{d1['ha3_unique']}**")
    if d1.get("ha3_warning"):
        L.append(f"- ⚠️ {d1['ha3_warning']}")
    L.append(f"- **RDS \\ HA3**（RDS 有但 HA3 缺）: **{d1['only_in_rds']}**")
    L.append(f"- **HA3 \\ RDS**（HA3 有但 RDS 已停）: **{d1['only_in_ha3']}**")
    L.append(f"- 交集: **{d1['intersect']}** / 对称差: **{d1['sym_diff']}**")
    L.append(f"- 漂移率 = sym_diff / rds_active = **{pct(d1['sym_diff'], d1['rds_active'])}**")
    if d1.get("only_in_rds_sample"):
        L.append("\n<details><summary>RDS\\HA3 前 20 样本</summary>\n")
        L.append("```\n" + "\n".join(d1["only_in_rds_sample"][:20]) + "\n```\n</details>")
    if d1.get("only_in_ha3_sample"):
        L.append("\n<details><summary>HA3\\RDS 前 20 样本</summary>\n")
        L.append("```\n" + "\n".join(d1["only_in_ha3_sample"][:20]) + "\n```\n</details>")

    # ── D2 ──
    L.append(section("D2 — image_refs 覆盖率（按 file_ext）"))
    L.append("| ext | docs | docs_with_step | step_cards | step_cards_with_refs | step 覆盖率 | image 覆盖率 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    tot = {"docs": 0, "docs_with_step": 0, "step_cards": 0, "step_cards_with_refs": 0}
    for r in payload["D2"]:
        for k in tot:
            tot[k] += int(r[k] or 0)
        L.append(f"| {r['fmt']} | {r['docs']} | {r['docs_with_step']} | {r['step_cards']} | "
                 f"{r['step_cards_with_refs']} | {pct(int(r['docs_with_step']), int(r['docs']))} | "
                 f"{pct(int(r['step_cards_with_refs']), int(r['step_cards']))} |")
    L.append(f"| **TOTAL** | **{tot['docs']}** | **{tot['docs_with_step']}** | "
             f"**{tot['step_cards']}** | **{tot['step_cards_with_refs']}** | "
             f"**{pct(tot['docs_with_step'], tot['docs'])}** | "
             f"**{pct(tot['step_cards_with_refs'], tot['step_cards'])}** |")

    # ── D3 ──
    d3 = payload["D3"]
    L.append(section("D3 — SOP 路由命中但 0 step_card 的候选漏 chunk"))
    L.append(f"- **限制**：SQL 只复刻了 `_detect_step_patterns` 的 cat/title 关键字侧，"
             "未复刻 `_STEP_DETECT_RE >=2` 文本侧检查 → 候选名单 ≠ 定罪名单。")
    L.append(f"- 路由命中: **{d3['routed_total']}** doc")
    L.append(f"- 其中产 ≥1 step_card: **{d3['routed_with_step']}**（{pct(d3['routed_with_step'], d3['routed_total'])}）")
    L.append(f"- 候选漏 chunk: **{len(d3['miss_list'])}**")
    if d3["miss_list"]:
        L.append("\n| doc_id | file_ext | title | chunks |")
        L.append("|---|---|---|---:|")
        for r in d3["miss_list"][:30]:
            t = (r.get("title") or r.get("original_filename") or "")[:40]
            L.append(f"| `{(r['doc_id'] or '')[:30]}` | {r.get('file_ext','')} | {t} | {r.get('total_chunks',0)} |")
        if len(d3["miss_list"]) > 30:
            L.append(f"\n_…还有 {len(d3['miss_list']) - 30} 个未列出_")

    # ── D4 ──
    d4 = payload["D4"]
    L.append(section("D4 — 孤儿 step_card"))
    L.append(f"- 孤儿数: **{d4['orphan_count']}**（pass gate = 0）")
    if d4["reason_counter"]:
        L.append("- 按原因：")
        for k, v in sorted(d4["reason_counter"].items(), key=lambda x: -x[1]):
            L.append(f"  - `{k}`: {v}")
    if d4["sample"]:
        L.append("\n| step_card_id | doc_id | parent_chunk_id | step_no | reason |")
        L.append("|---|---|---|---:|---|")
        for r in d4["sample"][:20]:
            L.append(f"| `{(r['step_card_id'] or '')[:24]}` | `{(r['doc_id'] or '')[:30]}` | "
                     f"`{(r.get('parent_chunk_id') or '')[:24]}` | {r.get('step_no','-')} | {r['orphan_reason']} |")

    # ── D5 ──
    d5 = payload["D5"]
    L.append(section("D5 — step_no 连续性"))
    L.append(f"- 总 parent 数（active step_card 有 step_no 且有 parent_chunk_id）: **{d5['total']}**")
    L.append(f"- 有 gap 或 min_step≠1 的 parent: **{d5['gap_count']}**（{pct(d5['gap_count'], d5['total'])}）")
    L.append(f"- step_no IS NULL 的 active step_card: **{d5['null_step_no']}**")
    L.append("- **解读**：子步 3.1/3.2 都映射 step_no=3 是合法的；这里用 `max(step_no) - count(distinct step_no)` 捕获真正的 gap。")
    if d5["sample"]:
        L.append("\n| doc_id | parent_chunk_id | min~max | distinct | total | missing |")
        L.append("|---|---|---|---:|---:|---:|")
        for r in d5["sample"]:
            L.append(f"| `{(r['doc_id'] or '')[:30]}` | `{(r['parent_chunk_id'] or '')[:24]}` | "
                     f"{r['min_step']}~{r['max_step']} | {r['distinct_steps']} | {r['total_cards']} | "
                     f"{r['missing_count']} |")

    # ── D6 ──
    d6 = payload["D6"]
    L.append(section("D6 — image_refs JSON shape 合规"))
    L.append(f"- 带 refs 的 step_card 总数: **{d6['total_chunks']}**")
    L.append(f"- 总 ref entry 数: **{d6['total_entries']}**")
    L.append(f"- JSON parse 失败的 chunk: **{d6['bad_json_chunks']}**（pass gate = 0）")
    L.append(f"- 整 chunk 全部 entry 合规（oss_key + image_index + xlsx anchor）: **{d6['chunk_compliant_all']}** "
             f"= {pct(d6['chunk_compliant_all'], d6['total_chunks'])}")
    L.append("- **逐字段 entry 级覆盖率**：")
    te = d6['total_entries']
    L.append(f"  - oss_key 非空：{d6['entry_oss_key_present']}/{te} = {pct(d6['entry_oss_key_present'], te)}")
    L.append(f"  - image_index 为 int：{d6['entry_image_index_present']}/{te} = {pct(d6['entry_image_index_present'], te)}")
    L.append(f"  - source_image 非空：{d6['entry_source_image_present']}/{te} = {pct(d6['entry_source_image_present'], te)}")
    L.append(f"  - visual_summary 非空：{d6['entry_visual_summary_present']}/{te} = {pct(d6['entry_visual_summary_present'], te)}")
    if d6["xlsx_entries"]:
        xe = d6['xlsx_entries']
        L.append(f"- **xlsx 子集**（{xe} entry）：")
        L.append(f"  - filename 非空：{d6['xlsx_filename_present']}/{xe} = {pct(d6['xlsx_filename_present'], xe)}")
        L.append(f"  - anchor_row 非空：{d6['xlsx_anchor_row_present']}/{xe} = {pct(d6['xlsx_anchor_row_present'], xe)}")
    L.append("- 逐 ext compliant chunk:")
    for ext in sorted(d6["per_ext"]):
        L.append(f"  - {ext}: {d6['per_ext_compliant'].get(ext, 0)}/{d6['per_ext'][ext]} "
                 f"= {pct(d6['per_ext_compliant'].get(ext, 0), d6['per_ext'][ext])}")
    if d6["bad_chunk_examples"]:
        L.append("\n<details><summary>不合规样本前 10</summary>\n")
        for ex in d6["bad_chunk_examples"]:
            L.append(f"- `{ex.get('chunk_id','?')}` ({ex.get('file_ext','?')}) missing: {ex.get('missing', ex.get('reason'))}")
        L.append("</details>")

    # ── D7 ──
    d7 = payload["D7"]
    L.append(section("D7 — procedure_parent 平衡"))
    L.append(f"- 有 step_card 但 0 procedure_parent 的 doc: **{d7['missing']}**（pass gate = 0）")
    L.append(f"- procedure_parent > 1 的 doc: **{d7['duplicate']}**")
    if d7["sample"]:
        L.append("\n| doc_id | version_no | step_cards | procedure_parents |")
        L.append("|---|---:|---:|---:|")
        for r in d7["sample"]:
            L.append(f"| `{(r['doc_id'] or '')[:30]}` | {r['version_no']} | "
                     f"{r['step_card_count']} | {r['procedure_parent_count']} |")

    # ── 结论 ──
    L.append(section("结论 & 建议"))
    L.append(payload.get("verdict", "(by reader)"))
    return "\n".join(L) + "\n"


def synthesize_verdict(payload: Dict[str, Any]) -> str:
    bits: List[str] = []
    d1 = payload["D1"]
    drift = d1["sym_diff"] / max(d1["rds_active"], 1) * 100
    if drift < 0.5:
        bits.append(f"- ✅ **D1 PASS**: RDS↔HA3 drift {drift:.2f}% < 0.5%")
    else:
        bits.append(f"- ⚠️ **D1 FAIL**: drift {drift:.2f}% ≥ 0.5%，需要触发 `reconcile_stranded_versions` 排查。")
    if payload["D4"]["orphan_count"] == 0:
        bits.append("- ✅ **D4 PASS**: 无孤儿 step_card")
    else:
        bits.append(f"- ⚠️ **D4 FAIL**: {payload['D4']['orphan_count']} 个孤儿 step_card，分布看 reason_counter")
    d7 = payload["D7"]
    if d7["missing"] == 0 and d7["duplicate"] == 0:
        bits.append("- ✅ **D7 PASS**: 每 doc 恰好 1 个 procedure_parent")
    else:
        bits.append(f"- ⚠️ **D7**: missing={d7['missing']}, duplicate={d7['duplicate']}")
    d5 = payload["D5"]
    gap_rate = d5["gap_count"] / max(d5["total"], 1) * 100
    bits.append(f"- D5 gap 率: {gap_rate:.2f}% （子步合并导致的非 gap 不计）")
    d6 = payload["D6"]
    bits.append(f"- D6 chunk 全合规率: {pct(d6['chunk_compliant_all'], d6['total_chunks'])}")
    return "\n".join(bits)


# ──────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        ROOT, "docs", "audits",
        f"ha3_step_card_coverage_{datetime.now().strftime('%Y-%m-%d')}.md"))
    ap.add_argument("--skip-ha3", action="store_true", help="只跑 RDS 维度（D2-D7），跳过 D1")
    ap.add_argument("--ha3-top-k", type=int, default=10000)
    args = ap.parse_args()

    from opensearch_pipeline.prod_access import get_prod_readonly_conn
    conn = get_prod_readonly_conn()

    print("[D1] RDS active step_card 计数 + chunk_id 收集 ...")
    rds_active, rds_ids, rds_indexed, rds_version_success = d1_rds_count_and_ids(conn)
    print(f"     RDS active={rds_active}, indexed={rds_indexed}, vsuccess={rds_version_success}")

    if args.skip_ha3:
        ha3_ids = set()
        ha3_returned = 0
        ha3_warning = "已通过 --skip-ha3 跳过 D1 HA3 侧。"
    else:
        print("[D1] HA3 step_card 拉取 ...")
        try:
            ha3_ids, ha3_returned, ha3_warning = d1_ha3_step_card_ids(top_k=args.ha3_top_k)
        except Exception as e:
            print(f"[D1] HA3 拉取失败：{e}")
            ha3_ids = set()
            ha3_returned = 0
            ha3_warning = f"HA3 拉取异常：{e}"

    only_in_rds = rds_ids - ha3_ids
    only_in_ha3 = ha3_ids - rds_ids
    intersect = rds_ids & ha3_ids

    d1_payload = {
        "rds_active": rds_active,
        "rds_indexed": rds_indexed,
        "rds_version_success": rds_version_success,
        "ha3_returned": ha3_returned,
        "ha3_unique": len(ha3_ids),
        "ha3_warning": ha3_warning,
        "only_in_rds": len(only_in_rds),
        "only_in_ha3": len(only_in_ha3),
        "intersect": len(intersect),
        "sym_diff": len(only_in_rds) + len(only_in_ha3),
        "only_in_rds_sample": sorted(only_in_rds)[:20],
        "only_in_ha3_sample": sorted(only_in_ha3)[:20],
    }

    print("[D2] image_refs 覆盖率（按 ext）...")
    d2 = d2_image_binding_by_ext(conn)

    print("[D3] SOP 路由命中但 0 step_card ...")
    routed_total, routed_with_step, miss_list = d3_routed_zero_step_card(conn)

    print("[D4] 孤儿 step_card ...")
    orphan_count, orphan_counter, orphan_sample = d4_orphan_step_cards(conn)

    print("[D5] step_no 连续性 ...")
    d5_total, d5_gap, d5_sample, d5_null = d5_step_continuity(conn)

    print("[D6] image_refs JSON shape ...")
    d6 = d6_image_ref_shape(conn)

    print("[D7] procedure_parent 平衡 ...")
    d7_missing, d7_dup, d7_sample = d7_parent_balance(conn)

    conn.close()

    from opensearch_pipeline.config import get_config
    cfg_ha3 = get_config().alibaba_vector

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ha3_table_name": cfg_ha3.table_name,
        "D1": d1_payload,
        "D2": d2,
        "D3": {
            "routed_total": routed_total,
            "routed_with_step": routed_with_step,
            "miss_list": miss_list,
        },
        "D4": {
            "orphan_count": orphan_count,
            "reason_counter": dict(orphan_counter),
            "sample": orphan_sample,
        },
        "D5": {
            "total": d5_total,
            "gap_count": d5_gap,
            "sample": d5_sample,
            "null_step_no": d5_null,
        },
        "D6": d6,
        "D7": {
            "missing": d7_missing,
            "duplicate": d7_dup,
            "sample": d7_sample,
        },
    }
    payload["verdict"] = synthesize_verdict(payload)

    md = render_markdown(payload)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)

    json_path = args.out.replace(".md", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n✓ Markdown 报告：{args.out}")
    print(f"✓ JSON 原始数据：{json_path}")
    print("\n──── VERDICT ────")
    print(payload["verdict"])


if __name__ == "__main__":
    main()

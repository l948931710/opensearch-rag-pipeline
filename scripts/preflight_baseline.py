#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preflight_baseline.py — 重灌前/后生产状态快照（只读 prod_ro）

拍一张生产侧的完整快照,供重灌期间 monitoring 对账 + 重灌后回归对比。
全只读(fuling_ro + SESSION READ ONLY),零写。

捕获:
  - RDS chunk_meta: 总数 / 按 chunk_type / is_active 分布
  - RDS document_meta: 按 status
  - RDS document_version: 按 chunk_status / index_status
  - HA3: chunk_type=step_card / procedure_parent / text_chunk / image 各计数
  - qa_session_log: 近 24h answer_status 分布 + error rate + 带图率
  - bulk_job: 按 status

用法:
  RAG_ENV=prod_ro RAG_READONLY=true \\
  RAG_ALLOW_REMOTE_DB=read_only_ack RAG_ALLOW_REMOTE_SEARCH=read_only_ack \\
    python scripts/preflight_baseline.py --label before_pilot
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _ha3_count(client, cfg, chunk_type: str, top_k: int = 10000) -> tuple[int, bool]:
    """HA3 按 chunk_type 计数（无 native count，用零向量 + filter + top_k）。"""
    from alibabacloud_ha3engine_vector.models import QueryRequest
    from opensearch_pipeline.retriever import _parse_ha3_response
    req = QueryRequest(
        table_name=cfg.table_name, vector=[0.0] * 1024, top_k=top_k,
        include_vector=False, output_fields=["chunk_id"],
        filter=f'chunk_type="{chunk_type}" AND is_active=1',
    )
    res = _parse_ha3_response(client.query(req))
    return len(res), len(res) >= top_k


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="snapshot", help="快照标签 (before_pilot / after_pilot 等)")
    ap.add_argument("--skip-ha3", action="store_true")
    args = ap.parse_args()

    from opensearch_pipeline.prod_access import get_prod_readonly_conn
    snap: dict = {"label": args.label, "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    conn = get_prod_readonly_conn()
    with conn.cursor() as cur:
        # chunk_meta
        cur.execute("SELECT COUNT(*) n FROM fuling_knowledge.chunk_meta")
        snap["chunk_meta_total"] = cur.fetchone()["n"]
        cur.execute("""SELECT chunk_type, is_active, COUNT(*) n
                       FROM fuling_knowledge.chunk_meta GROUP BY chunk_type, is_active""")
        snap["chunk_meta_by_type"] = [
            {"chunk_type": r["chunk_type"], "is_active": r["is_active"], "n": r["n"]}
            for r in cur.fetchall()
        ]
        # document_meta
        cur.execute("SELECT status, COUNT(*) n FROM fuling_knowledge.document_meta GROUP BY status")
        snap["document_meta_by_status"] = {r["status"]: r["n"] for r in cur.fetchall()}
        # document_version
        cur.execute("""SELECT chunk_status, index_status, COUNT(*) n
                       FROM fuling_knowledge.document_version GROUP BY chunk_status, index_status""")
        snap["document_version_status"] = [
            {"chunk_status": r["chunk_status"], "index_status": r["index_status"], "n": r["n"]}
            for r in cur.fetchall()
        ]
        # bulk_job
        cur.execute("""SELECT status, COUNT(*) n, SUM(total_chunks) tc
                       FROM fuling_knowledge.opensearch_bulk_job GROUP BY status""")
        snap["bulk_job_by_status"] = [
            {"status": r["status"], "n": r["n"], "total_chunks": int(r["tc"] or 0)}
            for r in cur.fetchall()
        ]
        # qa_session_log 近 24h
        cur.execute("""SELECT answer_status, COUNT(*) n
                       FROM fuling_operation.qa_session_log
                       WHERE created_at > NOW() - INTERVAL 24 HOUR
                       GROUP BY answer_status""")
        qa = {r["answer_status"]: r["n"] for r in cur.fetchall()}
        snap["qa_24h_by_status"] = qa
        total_qa = sum(qa.values())
        err = qa.get("LLM_ERROR", 0) + qa.get("RETRIEVAL_ERROR", 0)
        snap["qa_24h_total"] = total_qa
        snap["qa_24h_error_rate"] = round(err / total_qa, 4) if total_qa else None
        cur.execute("""SELECT
                         SUM(CASE WHEN answer_text LIKE '%<<IMG:%' THEN 1 ELSE 0 END) img,
                         COUNT(*) tot
                       FROM fuling_operation.qa_session_log
                       WHERE created_at > NOW() - INTERVAL 7 DAY""")
        r = cur.fetchone()
        snap["qa_7d_img_rate"] = round((r["img"] or 0) / r["tot"], 4) if r["tot"] else None
        snap["qa_7d_total"] = r["tot"]
    conn.close()

    # HA3
    if not args.skip_ha3:
        try:
            from opensearch_pipeline.config import get_config
            from opensearch_pipeline.retriever import _get_ha3_client
            cfg = get_config().alibaba_vector
            client = _get_ha3_client()
            snap["ha3"] = {}
            for ct in ("step_card", "procedure_parent", "text_chunk", "image"):
                n, truncated = _ha3_count(client, cfg, ct)
                snap["ha3"][ct] = {"count": n, "truncated": truncated}
        except Exception as e:
            snap["ha3_error"] = str(e)

    # 输出
    out_dir = os.path.join(ROOT, "scratch")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"baseline_{args.label}_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2, default=str)

    # 摘要打印
    print(f"=== Baseline snapshot: {args.label} ===")
    print(f"chunk_meta total: {snap['chunk_meta_total']}")
    print(f"document_meta: {snap['document_meta_by_status']}")
    print(f"qa 24h: total={snap['qa_24h_total']} error_rate={snap['qa_24h_error_rate']}")
    print(f"qa 7d img rate: {snap['qa_7d_img_rate']} (n={snap['qa_7d_total']})")
    if "ha3" in snap:
        print("HA3:", {k: v["count"] for k, v in snap["ha3"].items()})
    print(f"\n✓ 快照存 {out}")


if __name__ == "__main__":
    main()

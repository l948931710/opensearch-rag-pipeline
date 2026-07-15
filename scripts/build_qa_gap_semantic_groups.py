# -*- coding: utf-8 -*-
"""build_qa_gap_semantic_groups.py（原 scratch/build_qa_gap_semantic_groups_20260715.py，纳管入 scripts/） — 生成缺口相似问法语义组（schema/040，dry-run 默认）。

流程：拉近 N 天 NO_RESULT/REFUSAL 提问 → Python 按 question_hash 去重计权（与 kb_gaps
同口径）→ 逐问法取 dense embedding（retriever.get_query_embedding，text-embedding-v4；
SIM 下为哈希向量可离线试跑）→ 贪心归组（qa_gap_groups.greedy_group，阈值默认 0.90 保守，
宁漏并不错并）→ dry-run 打印多成员组预览；--commit REPLACE 全量重写映射表。

预注册边界（schema/040 头注）：产物仅供 kb_gaps 展示层归并（RAG_QA_GAP_SEMANTIC 门控），
绝不驱动缺口自动关闭。全量重写幂等，重跑安全；建议每次批量回答缺口后重跑一遍。

用法：
  RAG_ENV=<env> python scratch/build_qa_gap_semantic_groups.py [--days 365]
      [--threshold 0.90] [--sleep 0.2] [--commit]
生产写表走 env_guard（RAG_METADATA_PROD_ACK 同日 ack）；embedding 需 DashScope key。
"""
import argparse
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser(description="缺口相似问法语义归组（schema/040）")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--threshold", type=float, default=0.0, help="0=取 env/默认 0.90")
    ap.add_argument("--sleep", type=float, default=0.2, help="embedding 限速（秒/条）")
    ap.add_argument("--commit", action="store_true", help="真写映射表；缺省 dry-run 打印预览")
    args = ap.parse_args()

    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.contribution import question_hash
    from opensearch_pipeline.db import _get_db_conn
    from opensearch_pipeline.qa_logger import _op_db
    from opensearch_pipeline.qa_gap_groups import greedy_group, semantic_threshold

    threshold = args.threshold if 0.0 < args.threshold <= 1.0 else semantic_threshold()
    conn = _get_db_conn()
    try:
        # 1) 候选问法（与 kb_gaps 同源过滤；不依赖 039 是否已 backfill——hash 现算）
        buckets = {}   # hash -> {"text", "weight"}
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT query_text FROM {_op_db()}.qa_session_log"
                " WHERE answer_status IN ('NO_RESULT','REFUSAL') AND query_text IS NOT NULL"
                "   AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)", (args.days,))
            for (qtext,) in cur.fetchall() or []:
                h = question_hash(qtext or "")
                if not h or not (qtext or "").strip():
                    continue
                b = buckets.setdefault(h, {"text": qtext, "weight": 0})
                b["weight"] += 1
        print(f"近 {args.days} 天拒答问法（去重后）: {len(buckets)} 条，阈值 {threshold}")
        if not buckets:
            return 0

        # 2) embedding（dense；SIM=哈希向量可离线试跑）
        from opensearch_pipeline.retriever import get_query_embedding
        members = []
        for h, b in buckets.items():
            try:
                dense, _si, _sv = get_query_embedding(b["text"])
            except Exception as e:   # noqa: BLE001 — 单条失败跳过（自成一组也不错并）
                print(f"  ! embedding 失败跳过 {h[:12]}…: {e}")
                continue
            members.append({"hash": h, "text": b["text"], "vector": dense, "weight": b["weight"]})
            if args.sleep > 0:
                time.sleep(args.sleep)

        # 3) 贪心归组 + 预览
        rows = greedy_group(members, threshold)
        by_group = {}
        for r in rows:
            by_group.setdefault(r["group_id"], []).append(r)
        multi = {g: ms for g, ms in by_group.items() if len(ms) > 1}
        print(f"归组结果: {len(by_group)} 组（多成员组 {len(multi)} 个）")
        for g, ms in sorted(multi.items(), key=lambda kv: -len(kv[1])):
            rep = next((m for m in ms if m["question_hash"] == g), ms[0])
            print(f"  ◆ [{len(ms)} 问法] {rep['representative_text'][:60]}")
            for m in ms:
                if m["question_hash"] != g:
                    print(f"      · sim={m['similarity']:.3f}")
        if not args.commit:
            print("dry-run 结束（--commit 写 qa_gap_semantic_group）")
            return 0

        # 4) 全量重写（REPLACE 幂等；先清出组外的陈旧行再写现值）
        from opensearch_pipeline.env_guard import assert_metadata_write_allowed
        assert_metadata_write_allowed("qa_gap_semantic_group_build",
                                      get_config().rds.host, kind="rds")
        model = get_config().embedding.model
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_op_db()}.qa_gap_semantic_group")
            for r in rows:
                cur.execute(
                    f"REPLACE INTO {_op_db()}.qa_gap_semantic_group"
                    " (question_hash, group_id, representative_text, similarity, model)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (r["question_hash"], r["group_id"],
                     (r["representative_text"] or "")[:512], r["similarity"], model))
        conn.commit()
        print(f"已写入 {len(rows)} 行映射（模型 {model}）")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

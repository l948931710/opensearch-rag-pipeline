# -*- coding: utf-8 -*-
"""run_gap_groups_commit_20260718.py — 040 映射表生产 commit 一次性执行器。

背景：scripts/build_qa_gap_semantic_groups.py 的 --commit 走 config._get_db_conn，
本地对生产只有 PROD-RO(只读会话) 档——铁律要求生产写走 prod_access 唯一入口。
本 runner 镜像脚本语义（候选 SELECT → junk 过滤 → embedding → greedy_group →
DELETE 全表 + REPLACE 逐行），读走 get_prod_readonly_conn、写走 get_prod_rw_conn
（当日 PROD-RW token）+ env_guard 当日 RAG_METADATA_PROD_ACK 双闸。

Sam 2026-07-18 逐次授权（本会话「授权我带 token 跑」）。幂等可重跑（全量重写）。
用法：RAG_ENV=local python scratch/run_gap_groups_commit_20260718.py [--days 365]
（config 用 local 只取 DashScope 凭证做真 embedding；生产 DB 读写全走 prod_access，
 不经 config——prod_ro 形态会因 RAG_READONLY=true 被 env_guard 整会话拒写，属预期。）
"""
import argparse
import os
import sys
import time
from datetime import date


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()
    today = f"{date.today():%Y-%m-%d}"
    # env_guard 双闸（op:date 当日制）；embedding/config 走 prod_ro overlay（只读侧无写能力）
    os.environ.setdefault("RAG_METADATA_PROD_ACK", f"qa_gap_semantic_group_build:{today}")

    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.contribution import is_junk_question, question_hash
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.prod_access import get_prod_readonly_conn, get_prod_rw_conn
    from opensearch_pipeline.qa_gap_groups import greedy_group, semantic_threshold
    from opensearch_pipeline.retriever import get_query_embedding

    op_db = get_config().rds.operation_database
    threshold = semantic_threshold()

    # 1) 候选（只读连接；dict cursor——prod_access 默认 DictCursor，按键取列）
    ro = get_prod_readonly_conn()
    buckets = {}
    try:
        with ro.cursor() as cur:
            cur.execute(
                f"SELECT query_text FROM {op_db}.qa_session_log"
                " WHERE answer_status IN ('NO_RESULT','REFUSAL') AND query_text IS NOT NULL"
                "   AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)", (args.days,))
            for row in cur.fetchall() or []:
                qtext = row["query_text"] if isinstance(row, dict) else row[0]
                if is_junk_question(qtext):
                    continue
                h = question_hash(qtext or "")
                if not h or not (qtext or "").strip():
                    continue
                b = buckets.setdefault(h, {"text": qtext, "weight": 0})
                b["weight"] += 1
    finally:
        ro.close()
    print(f"近 {args.days} 天拒答问法（去重后）: {len(buckets)} 条，阈值 {threshold}")
    if not buckets:
        print("无候选，不写表")
        return 0

    # 2) embedding + 归组
    members = []
    for h, b in buckets.items():
        try:
            dense, _si, _sv = get_query_embedding(b["text"])
        except Exception as e:   # noqa: BLE001 — 单条失败自成一组，不错并
            print(f"  ! embedding 失败跳过 {h[:12]}…: {e}")
            continue
        members.append({"hash": h, "text": b["text"], "vector": dense, "weight": b["weight"]})
        if args.sleep > 0:
            time.sleep(args.sleep)
    rows = greedy_group(members, threshold)
    groups = {}
    for r in rows:
        groups.setdefault(r["group_id"], []).append(r)
    multi = {g: ms for g, ms in groups.items() if len(ms) > 1}
    print(f"归组结果: {len(groups)} 组（多成员组 {len(multi)} 个），共 {len(rows)} 行映射")

    # 3) 双闸 + RW 写（DELETE 全表 + REPLACE 逐行，与 scripts/build 同语义）。
    # host 用【真实写目标】（prod env 的 RAG_RDS_HOST），不是 config（local=localhost）——
    # env_guard 的 prod 指纹判定必须对着真目标。
    from opensearch_pipeline.prod_access import load_prod_env
    prod_host = load_prod_env().get("RAG_RDS_HOST", "")
    assert_metadata_write_allowed("qa_gap_semantic_group_build", prod_host, kind="rds")
    model = get_config().embedding.model
    rw = get_prod_rw_conn(ack=f"PROD-RW:{today}")
    try:
        with rw.cursor() as cur:
            cur.execute(f"DELETE FROM {op_db}.qa_gap_semantic_group")
            for r in rows:
                cur.execute(
                    f"REPLACE INTO {op_db}.qa_gap_semantic_group"
                    " (question_hash, group_id, representative_text, similarity, model)"
                    " VALUES (%s,%s,%s,%s,%s)",
                    (r["question_hash"], r["group_id"],
                     (r["representative_text"] or "")[:512], r["similarity"], model))
        rw.commit()
        # 4) 同连接回读验证
        with rw.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM {op_db}.qa_gap_semantic_group")
            row = cur.fetchone()
            total = row["n"] if isinstance(row, dict) else row[0]
            cur.execute(
                f"SELECT COUNT(*) AS n FROM (SELECT group_id FROM {op_db}.qa_gap_semantic_group"
                " GROUP BY group_id HAVING COUNT(*) > 1) t")
            row = cur.fetchone()
            multi_db = row["n"] if isinstance(row, dict) else row[0]
    finally:
        rw.close()
    print(f"✅ 已写入并回读验证：{total} 行映射 / 多成员组 {multi_db} 个（模型 {model}）")
    if int(total) != len(rows) or int(multi_db) != len(multi):
        print("⚠️ 回读与本地计算不一致，请人工核查")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

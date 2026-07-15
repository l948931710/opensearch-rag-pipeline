# -*- coding: utf-8 -*-
"""backfill_qa_question_hash.py（原 scratch/backfill_qa_question_hash_20260715.py，纳管入 scripts/） — schema/039 存量回填（dry-run 默认）。

qa_session_log.question_hash 对【已落库（已脱敏）query_text】计算 contribution.question_hash
—— 与写侧 qa_logger、读侧 kb_gaps Python 归并完全同口径。分批 SELECT→计算→UPDATE，
幂等（WHERE question_hash IS NULL 双保险），可中断续跑。

用法（先 apply schema/039）：
  RAG_ENV=<env> python scripts/backfill_qa_question_hash.py            # dry-run：只报计数
  RAG_ENV=<env> python scripts/backfill_qa_question_hash.py --commit   # 分批回填
生产：RAG_ENV 缺省（SAE 同款注入）或 prod + 同日 RAG_METADATA_PROD_ACK（env_guard 门）。
"""
import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description="qa_session_log.question_hash 存量回填（schema/039）")
    ap.add_argument("--commit", action="store_true", help="真写；缺省 dry-run 只报计数")
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--max-batches", type=int, default=0, help="0=不限（跑到清零）")
    args = ap.parse_args()

    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.contribution import question_hash
    from opensearch_pipeline.db import _get_db_conn
    from opensearch_pipeline.qa_logger import _op_db

    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_op_db()}.qa_session_log"
                        " WHERE question_hash IS NULL AND query_text IS NOT NULL")
            pending = int((cur.fetchone() or (0,))[0] or 0)
        print(f"待回填行数: {pending}")
        if not args.commit:
            print("dry-run 结束（--commit 执行回填）")
            return 0

        from opensearch_pipeline.env_guard import assert_metadata_write_allowed
        assert_metadata_write_allowed("qa_question_hash_backfill", get_config().rds.host, kind="rds")

        done = batches = 0
        while True:
            with conn.cursor() as cur:
                cur.execute(f"SELECT id, query_text FROM {_op_db()}.qa_session_log"
                            " WHERE question_hash IS NULL AND query_text IS NOT NULL"
                            " ORDER BY id LIMIT %s", (args.batch,))
                rows = cur.fetchall() or []
                if not rows:
                    break
                for rid, qtext in rows:
                    cur.execute(f"UPDATE {_op_db()}.qa_session_log SET question_hash=%s"
                                " WHERE id=%s AND question_hash IS NULL",
                                (question_hash(qtext or ""), rid))
            conn.commit()
            done += len(rows)
            batches += 1
            print(f"  batch {batches}: +{len(rows)}（累计 {done}/{pending}）")
            if args.max_batches and batches >= args.max_batches:
                print("达到 --max-batches 上限，可续跑")
                break
        print(f"回填完成: {done} 行")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

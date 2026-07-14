# -*- coding: utf-8 -*-
"""backfill_normalized_title.py — 038 存量回填（C2 复核批次6）。

把 ontology_object.normalized_title IS NULL 的存量行按 normalize.title_key
（去全部空白 + 全半角统一——SQL 无法复刻，必须 Python 侧算）分批 UPDATE。

用法（dry-run 默认，只统计不写）：
    RAG_ENV=local  python scripts/backfill_normalized_title.py
    RAG_ENV=local  python scripts/backfill_normalized_title.py --commit
生产走 prod_access 同款纪律：先 apply schema/038，--commit 前先 dry-run 看量。
幂等：只挑 NULL 行，重跑零变更；批间无锁滞留（每批独立事务）。

**真实播种开闸前置**：038 apply + 本脚本 --commit 跑到 remaining=0
（NULL 行不参与等值召回；过渡期 seeding LIKE 辅臂兜底并 WARNING 报缺口）。
"""
import argparse
import sys

sys.path.insert(0, ".")

from opensearch_pipeline.ontology.normalize import title_key  # noqa: E402
from opensearch_pipeline.ontology.store import RDSOntologyStore  # noqa: E402

BATCH = 500


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="038 normalized_title 存量回填（dry-run 默认）")
    p.add_argument("--commit", action="store_true", help="实际 UPDATE（默认只统计）")
    p.add_argument("--batch", type=int, default=BATCH)
    args = p.parse_args(argv)

    store = RDSOntologyStore()
    db = store._db()
    total_filled = 0
    while True:
        conn = store._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT object_id, title FROM {db}.ontology_object "
                    "WHERE normalized_title IS NULL ORDER BY object_id LIMIT %s",
                    (args.batch,))
                rows = cur.fetchall()
                if not rows:
                    conn.rollback()
                    break
                if not args.commit:
                    cur.execute(f"SELECT COUNT(*) FROM {db}.ontology_object "
                                "WHERE normalized_title IS NULL")
                    n = int(cur.fetchone()[0])
                    conn.rollback()
                    print(f"[dry-run] 待回填 {n} 行（示例：{rows[0][0]} "
                          f"{rows[0][1]!r} → {title_key(rows[0][1])!r}）；--commit 实际执行")
                    return 0
                pairs = [(title_key(t or ""), oid) for oid, t in rows]
                cur.executemany(
                    f"UPDATE {db}.ontology_object SET normalized_title=%s "
                    "WHERE object_id=%s AND normalized_title IS NULL", pairs)
            conn.commit()
            total_filled += len(rows)
            print(f"  ...已回填 {total_filled} 行")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    print(f"✅ 回填完成：本次 {total_filled} 行；remaining=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

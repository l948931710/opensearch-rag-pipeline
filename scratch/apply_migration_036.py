# -*- coding: utf-8 -*-
"""Apply migration 036 — agent_run 增 message_id（U1/U2 答案读回 + 续跑反馈锚定）。

目标库 fuling_operation。DDL 权威文件 schema/036_agent_run_message_id.sql（F-35，
本脚本不内嵌第二份语义，仅按 information_schema 幂等执行同一 ADD COLUMN）。

非破坏性：纯加列（NULL DEFAULT NULL），旧行零影响；先 apply 后部署（run_store.get_run
的 SELECT 引用本列，未迁移库上新代码会整体报错）。2026-07-12 Sam 授权 staging/prod apply
（staging 已经 scripts/apply_migration.py 落定）。

Usage:
  python scratch/apply_migration_036.py --check          # READ-ONLY（prod_ro），不写
  python scratch/apply_migration_036.py --target prod    # prod（当日 PROD-RW token + fuling_admin）
"""
import argparse
import sys
from datetime import date

sys.path.insert(0, ".")

OP = "fuling_operation"
TABLE = "agent_run"
COLUMN = "message_id"
FILENAME = "036_agent_run_message_id.sql"
VERSION = "036"
APPLIED_BY = "scratch/apply_migration_036.py"
NOTES = ("agent_run 增 message_id（qa_session_log 答案行锚定键：submit 回填/resume 复用；"
         "U1 答案读回 + U2 反馈不悬空，2026-07-11 重审计 §5）")

DDL = (f"ALTER TABLE `{OP}`.`{TABLE}` "
       "ADD COLUMN `message_id` VARCHAR(64) NULL DEFAULT NULL "
       "COMMENT 'qa_session_log 答案行锚定键（submit 回填；resume 复用；036）' "
       "AFTER `conversation_id`")


def tbl_exists(cur, db, table):
    cur.execute("SELECT COUNT(*) FROM information_schema.TABLES "
                "WHERE table_schema=%s AND table_name=%s", (db, table))
    return cur.fetchone()[0] > 0


def col_exists(cur, db, table, column):
    cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS "
                "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                (db, table, column))
    return cur.fetchone()[0] > 0


def probe(cur):
    if not tbl_exists(cur, OP, TABLE):
        return {"table": False}
    ledger = tbl_exists(cur, OP, "schema_migrations")
    has_036 = False
    if ledger:
        cur.execute(f"SELECT COUNT(*) FROM {OP}.schema_migrations WHERE filename=%s",
                    (FILENAME,))
        has_036 = cur.fetchone()[0] > 0
    return {"table": True, "column": col_exists(cur, OP, TABLE, COLUMN),
            "ledger": ledger, "ledger_has_036": has_036}


def show(cur, label):
    d = probe(cur)
    if not d["table"]:
        print(f"[{label}] {OP}.{TABLE} 不存在（022 未 apply？abort 前提不成立）")
        return d
    print(f"[{label}] {OP}.{TABLE}: {COLUMN}_present={d['column']}  "
          f"ledger_has_036={d['ledger_has_036']}")
    return d


def apply(cur):
    d = probe(cur)
    if not d["table"]:
        raise SystemExit(f"[ABORT] {OP}.{TABLE} 不存在——先确认 022 已 apply。")
    if d["column"]:
        print(f"[APPLY] {COLUMN} 已存在 → no-op（幂等）")
    else:
        cur.execute(DDL)
        print(f"[APPLY] ADD COLUMN {COLUMN} VARCHAR(64) NULL  OK")
    if d["ledger"]:
        cur.execute(f"INSERT IGNORE INTO {OP}.schema_migrations "
                    "(filename, version, applied_by, notes) VALUES (%s,%s,%s,%s)",
                    (FILENAME, VERSION, APPLIED_BY, NOTES))
        print(f"[LEDGER] {OP}.schema_migrations += ({FILENAME})  rowcount={cur.rowcount}")
    else:
        print(f"[LEDGER] ⚠️ {OP}.schema_migrations 不存在（先跑 apply_migration_011_012.py）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["prod"])
    ap.add_argument("--check", action="store_true",
                    help="read-only 诊断（prod 只读会话），绝不写")
    args = ap.parse_args()

    if args.check:
        from opensearch_pipeline.prod_access import get_prod_readonly_conn
        conn = get_prod_readonly_conn(dict_cursor=False)
        try:
            with conn.cursor() as cur:
                show(cur, "CHECK")
        finally:
            conn.close()
        return

    if not args.target:
        ap.error("either --check or --target prod is required")

    from opensearch_pipeline.prod_access import get_prod_rw_conn
    conn = get_prod_rw_conn(ack=f"PROD-RW:{date.today().isoformat()}", dict_cursor=False)
    try:
        with conn.cursor() as cur:
            show(cur, "BEFORE")
            apply(cur)
        conn.commit()
        with conn.cursor() as cur:
            show(cur, "AFTER")
    finally:
        conn.close()
    print("[done] migration 036 applied to prod")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_migration.py — schema 迁移 apply 工具（WS0-4；替代 gitignored 的一次性 scratch 脚本）

纪律（schema/README + schema/011 台账）：
  - **--dry-run 默认**：只预览语句 + information_schema 现状对比，绝不写库；--commit 才落库。
  - **环境守卫**：local(host=localhost 或 environment=dev/test) / staging(库名 *_stg) 允许 --commit；
    生产库须显式 --prod-ack <token>，且经 prod_access.get_prod_rw_conn（复用四账号 + 当日 RW 纪律）。
  - **幂等**：迁移文件用 CREATE TABLE IF NOT EXISTS；应用前后对 information_schema 对比、只报差异。
  - **台账**：成功后向目标库 schema_migrations INSERT 一行（缺表则告警不崩，提示先 apply 011）。

连库尊重 RAG_ENV overlay（get_config().rds）：本地/staging 直连；生产走 prod_access。

用法：
  RAG_ENV=staging python scripts/apply_migration.py schema/022_agent_runtime.sql --db operation --dry-run
  RAG_ENV=staging python scripts/apply_migration.py schema/022_agent_runtime.sql --db operation --commit
  # 双库文件（如 018）分别对 --db knowledge 与 --db operation 各跑一次。
"""
import argparse
import os
import re
import sys

# 允许 `python scripts/apply_migration.py` 直接跑：把仓库根加进 path（scripts/ 的父目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _split_statements(sql_text):
    """按 ; 切分语句；剔除整行 -- 注释，内联注释留给 MySQL 解析；跳过空块。"""
    out = []
    for chunk in sql_text.split(";"):
        code = "\n".join(ln for ln in chunk.splitlines() if not ln.strip().startswith("--")).strip()
        if code:
            out.append(code)
    return out


def _table_names(statements):
    names = []
    for s in statements:
        m = re.search(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?", s, re.IGNORECASE)
        if m:
            names.append(m.group(1))
    return names


def _existing_tables(conn, dbname, tables):
    if not tables:
        return set()
    ph = ",".join(["%s"] * len(tables))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema=%s AND table_name IN ({ph})",
            tuple([dbname] + tables))
        return {r[0] for r in cur.fetchall()}


def _connect(cfg, prod_ack, is_prod):
    if is_prod:
        from opensearch_pipeline.prod_access import get_prod_rw_conn
        return get_prod_rw_conn(prod_ack)          # ack + 四账号纪律在 prod_access 内
    import pymysql
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, charset="utf8mb4", connect_timeout=10)


def main():
    ap = argparse.ArgumentParser(description="apply 一个 schema 迁移文件到目标库（staging 先行）")
    ap.add_argument("migration_file")
    ap.add_argument("--db", choices=["operation", "knowledge"], required=True,
                    help="目标逻辑库（operation=fuling_operation[_stg] / knowledge=fuling_knowledge[_stg]）")
    ap.add_argument("--commit", action="store_true", help="实际执行（默认 dry-run 只预览）")
    ap.add_argument("--dry-run", action="store_true", help="只预览（默认行为；显式传更清晰，与 --commit 互斥）")
    ap.add_argument("--prod-ack", default=None, help="生产 apply 确认令牌（对齐 prod_access.get_prod_rw_conn）")
    ap.add_argument("--applied-by", default="scripts/apply_migration.py")
    args = ap.parse_args()
    if args.dry_run and args.commit:
        ap.error("--dry-run 与 --commit 互斥")

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    host = cfg.rds.host
    dbname = cfg.rds.operation_database if args.db == "operation" else cfg.rds.database
    environment = getattr(cfg, "environment", "development")

    is_local = host in ("localhost", "127.0.0.1", "::1") or environment in ("development", "test")
    is_staging = dbname.endswith("_stg")
    is_prod = not is_local and not is_staging
    env_label = "local" if is_local else ("staging" if is_staging else "PRODUCTION")

    fn = os.path.basename(args.migration_file)
    version = (re.match(r"(\d+[a-z]?)", fn).group(1) if re.match(r"(\d+[a-z]?)", fn) else fn)
    statements = _split_statements(open(args.migration_file, encoding="utf-8").read())
    tables = _table_names(statements)

    print(f"迁移文件 : {fn}  (version={version})")
    print(f"目标     : {dbname}  (逻辑 {args.db})  host={host}  环境={env_label}")
    print(f"语句/表  : {len(statements)} 条 / 表 {tables}")
    print(f"模式     : {'COMMIT（真写）' if args.commit else 'DRY-RUN（只预览）'}")

    if is_prod and not args.prod_ack:
        print("\n❌ 目标为**生产库**，须显式 --prod-ack <当日 RW 令牌>（经 prod_access）。中止。")
        sys.exit(2)

    conn = _connect(cfg, args.prod_ack, is_prod)
    try:
        existing = _existing_tables(conn, dbname, tables)
        print(f"apply 前   : 已存在 {sorted(existing)} | 待建 {[t for t in tables if t not in existing]}")

        if not args.commit:
            print("\n[DRY-RUN] 不写库。语句预览：")
            for i, s in enumerate(statements, 1):
                head = s.splitlines()[0][:100]
                print(f"  {i:>2}. {head}{'…' if len(s) > 100 else ''}")
            print(f"\n[DRY-RUN] --commit 将：执行以上 {len(statements)} 条 + 向 {dbname}.schema_migrations "
                  f"记 ({fn}, {version})。确认无误后加 --commit。")
            return

        with conn.cursor() as cur:
            cur.execute(f"USE `{dbname}`")
            for s in statements:
                cur.execute(s)
        conn.commit()
        after = _existing_tables(conn, dbname, tables)
        print(f"\n✅ 已 apply {len(statements)} 条。现存表：{sorted(after)}")

        # 台账（独立事务，缺表容错）
        try:
            with conn.cursor() as cur:
                cur.execute(f"USE `{dbname}`")
                cur.execute(
                    "INSERT IGNORE INTO schema_migrations (filename, version, applied_by, notes) "
                    "VALUES (%s,%s,%s,%s)",
                    (fn, version, args.applied_by, f"apply via script ({env_label})"))
            conn.commit()
            print(f"✅ 台账已记：{fn} ({version}) → {dbname}.schema_migrations")
        except Exception as e:   # noqa: BLE001
            print(f"⚠️ 台账未记（{e}）——目标库可能缺 schema_migrations，请先 apply schema/011。DDL 已落定。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

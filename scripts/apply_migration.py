#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_migration.py — schema 迁移 apply 工具（WS0-4；PR-D P0-09 加固）

纪律（schema/README + schema/011 台账 + 外审 P0-09 验收）：
  - **--dry-run 默认**：只预览语句 + information_schema 现状对比，绝不写库，
    **不建 RW 连接**（生产目标 dry-run 走 prod_access 只读会话；连不上降级为纯解析预览）。
  - **物理指纹优先**（P0-09）：环境判定只看 host 指纹 + 库名 _stg 后缀——自报
    environment=development/test **不再**能把生产 host 判成 local（旧洞：dev 标签
    + read_only_ack 远程连接可绕过 prod_access token 路由）。
  - **checksum**（P0-09）：apply 前算文件 SHA-256；台账已有同名不同 checksum 记录
    → **中止**（同版本内容被改过 = 漂移事故预备役，绝不静默择一）。
  - **台账 fail-closed**（P0-09）：DDL 落定但台账写失败 → **exit 3**（非零）——
    "结构已变、台账无记录"必须被调度/操作者看见，不再 warning 后 exit 0。
  - **幂等**：迁移文件用 CREATE TABLE IF NOT EXISTS；应用前后对 information_schema 对比。

用法：
  RAG_ENV=staging python scripts/apply_migration.py schema/022_agent_runtime.sql --db operation --dry-run
  RAG_ENV=staging python scripts/apply_migration.py schema/027_ontology_core.sql --db ontology --commit
  # 双库文件（如 018）分别对 --db knowledge 与 --db operation 各跑一次。
"""
import argparse
import hashlib
import os
import re
import sys

# 允许 `python scripts/apply_migration.py` 直接跑：把仓库根加进 path（scripts/ 的父目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXIT_PROD_NO_ACK = 2
EXIT_LEDGER_FAILED = 3
EXIT_CHECKSUM_MISMATCH = 4


def _split_statements(sql_text):
    """剔除整行 -- 注释后再按 ; 切分（PR 收口修正：注释行内的分号——如 011 头注的
    「USE …; 再 …;」——曾把注释后半截切成"SQL"报 1064）；内联注释留给 MySQL 解析。"""
    body = "\n".join(ln for ln in sql_text.splitlines()
                     if not ln.strip().startswith("--"))
    return [chunk.strip() for chunk in body.split(";") if chunk.strip()]


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


def classify_target(host: str, dbname: str):
    """(is_local, is_staging, is_prod)——**物理指纹优先**（P0-09）：
    生产 host 指纹命中且库名非 _stg → prod，自报 environment 无发言权；
    非本地 host 即便没命中指纹也按 prod 处理（宁严不松——未知远端不给 local 待遇）。"""
    from opensearch_pipeline.config import _LOCAL_HOSTS, is_prod_target
    is_staging = dbname.endswith("_stg")
    is_local = host in _LOCAL_HOSTS
    is_prod = (is_prod_target("rds", host) or not is_local) and not is_staging
    return is_local and not is_prod, is_staging, is_prod


def _connect_rw(cfg, prod_ack, is_prod):
    if is_prod:
        from opensearch_pipeline.prod_access import get_prod_rw_conn
        return get_prod_rw_conn(prod_ack)          # ack + 四账号纪律在 prod_access 内
    import pymysql
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, charset="utf8mb4", connect_timeout=10)


def _connect_ro(cfg, is_prod):
    """dry-run 只读连接（最小权限，P0-09）：prod → prod_access 只读会话（无 RW token）；
    其余直连。任何失败返回 None → 降级为纯解析预览（不因连不上而丢 dry-run 能力）。"""
    try:
        if is_prod:
            from opensearch_pipeline.prod_access import get_prod_readonly_conn
            return get_prod_readonly_conn(dict_cursor=False)
        import pymysql
        return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                               password=cfg.rds.password, charset="utf8mb4",
                               connect_timeout=10)
    except Exception as e:   # noqa: BLE001
        print(f"（只读连接不可用，降级为纯解析预览：{e}）")
        return None


def _ledger_has_checksum_col(conn, dbname) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name='schema_migrations' "
            "AND column_name='checksum'", (dbname,))
        return int(cur.fetchone()[0]) == 1


def _ledger_conflict(conn, dbname, fn, checksum):
    """台账已有同名记录：checksum 一致/无 checksum 列史前记录 → None（幂等重跑）；
    不一致 → 返回旧值（调用方中止）。"""
    try:
        has_col = _ledger_has_checksum_col(conn, dbname)
        with conn.cursor() as cur:
            if has_col:
                cur.execute(f"SELECT checksum FROM `{dbname}`.schema_migrations "
                            "WHERE filename=%s", (fn,))
            else:
                cur.execute(f"SELECT NULL FROM `{dbname}`.schema_migrations "
                            "WHERE filename=%s", (fn,))
            row = cur.fetchone()
    except Exception:   # noqa: BLE001 — 台账表缺失等：交给后面写台账时报
        return None
    if row is None or row[0] is None:
        return None
    return row[0] if row[0] != checksum else None


def main():
    ap = argparse.ArgumentParser(description="apply 一个 schema 迁移文件到目标库（staging 先行）")
    ap.add_argument("migration_file")
    ap.add_argument("--db", choices=["operation", "knowledge", "ontology"], required=True,
                    help="目标逻辑库（operation=fuling_operation[_stg] / "
                         "knowledge=fuling_knowledge[_stg] / ontology=fuling_ontology[_stg]）")
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
    dbname = {"operation": cfg.rds.operation_database,
              "knowledge": cfg.rds.database,
              "ontology": cfg.rds.ontology_database}[args.db]

    is_local, is_staging, is_prod = classify_target(host, dbname)
    env_label = "local" if is_local else ("staging" if is_staging else "PRODUCTION")

    fn = os.path.basename(args.migration_file)
    version = (re.match(r"(\d+[a-z]?)", fn).group(1) if re.match(r"(\d+[a-z]?)", fn) else fn)
    raw = open(args.migration_file, "rb").read()
    checksum = hashlib.sha256(raw).hexdigest()
    statements = _split_statements(raw.decode("utf-8"))
    tables = _table_names(statements)

    print(f"迁移文件 : {fn}  (version={version})")
    print(f"checksum : sha256={checksum}")
    print(f"目标     : {dbname}  (逻辑 {args.db})  host={host}  环境={env_label}（物理指纹判定）")
    print(f"语句/表  : {len(statements)} 条 / 表 {tables}")
    print(f"模式     : {'COMMIT（真写）' if args.commit else 'DRY-RUN（只预览，只读连接）'}")

    if not args.commit:
        conn = _connect_ro(cfg, is_prod)
        try:
            if conn is not None:
                existing = _existing_tables(conn, dbname, tables)
                print(f"apply 前   : 已存在 {sorted(existing)} | "
                      f"待建 {[t for t in tables if t not in existing]}")
                old = _ledger_conflict(conn, dbname, fn, checksum)
                if old:
                    print(f"⚠️ 台账已有 {fn} 且 checksum 不同（旧 {old[:12]}…）——"
                          f"同版本内容被改过，--commit 会中止。")
            print("\n[DRY-RUN] 不写库。语句预览：")
            for i, s in enumerate(statements, 1):
                head = s.splitlines()[0][:100]
                print(f"  {i:>2}. {head}{'…' if len(s) > 100 else ''}")
            print(f"\n[DRY-RUN] --commit 将：执行以上 {len(statements)} 条 + 向 "
                  f"{dbname}.schema_migrations 记 ({fn}, {version}, sha256)。")
        finally:
            if conn is not None:
                conn.close()
        return

    if is_prod and not args.prod_ack:
        print("\n❌ 目标为**生产库**，须显式 --prod-ack <当日 RW 令牌>（经 prod_access）。中止。")
        sys.exit(EXIT_PROD_NO_ACK)

    conn = _connect_rw(cfg, args.prod_ack, is_prod)
    try:
        old = _ledger_conflict(conn, dbname, fn, checksum)
        if old:
            print(f"\n❌ 台账已记 {fn} 且 checksum 不同（台账 {old[:12]}… ≠ 本文件 "
                  f"{checksum[:12]}…）——同版本内容被改过（漂移预备役），中止。"
                  f"修订已发布文件须走 NNNa 修订号（README 铁律 2）。")
            sys.exit(EXIT_CHECKSUM_MISMATCH)
        existing = _existing_tables(conn, dbname, tables)
        print(f"apply 前   : 已存在 {sorted(existing)} | 待建 {[t for t in tables if t not in existing]}")

        with conn.cursor() as cur:
            cur.execute(f"USE `{dbname}`")
            for s in statements:
                cur.execute(s)
        conn.commit()
        after = _existing_tables(conn, dbname, tables)
        print(f"\n✅ 已 apply {len(statements)} 条。现存表：{sorted(after)}")

        # 台账（P0-09 fail-closed）：写失败 → 非零退出——"结构已变、台账无记录"必须被看见
        try:
            has_col = _ledger_has_checksum_col(conn, dbname)
            with conn.cursor() as cur:
                cur.execute(f"USE `{dbname}`")
                if has_col:
                    cur.execute(
                        "INSERT IGNORE INTO schema_migrations "
                        "(filename, version, applied_by, notes, checksum) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (fn, version, args.applied_by,
                         f"apply via script ({env_label})", checksum))
                else:
                    cur.execute(
                        "INSERT IGNORE INTO schema_migrations (filename, version, applied_by, notes) "
                        "VALUES (%s,%s,%s,%s)",
                        (fn, version, args.applied_by,
                         f"apply via script ({env_label}); sha256={checksum}"))
            conn.commit()
            suffix = "" if has_col else "（无 checksum 列——建议先 apply schema/032）"
            print(f"✅ 台账已记：{fn} ({version}) → {dbname}.schema_migrations{suffix}")
        except Exception as e:   # noqa: BLE001
            print(f"❌ DDL 已落定但台账写失败（{e}）——目标库可能缺 schema_migrations"
                  f"（先 apply schema/011）。**exit {EXIT_LEDGER_FAILED}**：请人工补记后再继续。")
            sys.exit(EXIT_LEDGER_FAILED)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

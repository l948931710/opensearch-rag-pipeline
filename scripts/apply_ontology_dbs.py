#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_ontology_dbs.py — ontology 库建库 + 本体迁移族按序 apply（可重放；P1-14 产品化）

背景：2026-07-10 生产首灌用的编排脚本 scratch/apply_ontology_dbs_20260710.py 不入仓
（scratch/ 被 gitignore）→ 生产落库不可从仓库重放。本文件把它产品化进仓库：建库、
迁移序、台账语义对齐 scratch 版；守卫机制**完全复用 scripts/apply_migration.py**。

一次运行 = 对当前 RAG_ENV 指向的 ontology 库做一遍完整重放：
  1. CREATE DATABASE IF NOT EXISTS <cfg.rds.ontology_database>
     （显式 utf8mb4_unicode_ci，schema/README 铁律 4；已存在但 collation 漂移 → 中止）；
  2. 按编号序 apply 本体迁移族：011（台账表）→ 027/028/029/030（本体家族）→ 032
     （台账 checksum 列）。家族成员从 scripts/ci_load_schema.sh 的 MANIFEST（file→DB
     权威矩阵的机器可读形态，tests 强制其覆盖每个 schema/*.sql）**自动发现**
     target=ontology 的文件——后续 03x 本体迁移登记进 MANIFEST 即被本工具捡起；
  3. 全部落定后统一写 schema_migrations 台账（届时 032 已就位 → 全部带 SHA-256）。

守卫（与 apply_migration.py 同源同纪律）：
  - **--dry-run 默认**：只读连接预览（连不上降级纯解析），绝不写；--commit 才执行；
  - 环境判定物理指纹优先（classify_target）：local / _stg 放行；生产 --commit 须
    --prod-ack 当日 RW 令牌（经 prod_access，exit 2）；
  - 幂等：迁移文件自身 IF NOT EXISTS / INSERT IGNORE / information_schema+PREPARE
    守卫（027 尾注「支持重复 apply」），重放即全量重执行、已就位对象 no-op；
  - 台账同名不同 checksum → 中止（同版本内容被改过=漂移，exit 4，README 铁律 2）；
  - 台账 fail-closed：DDL 落定但台账写失败 → exit 3（人工补记后再继续）；
  - 台账缺表容错：schema_migrations 未建时冲突检查按「无记录」处理（011 会建它）。

范围注（scratch 版有、本工具刻意不含的一次性 admin 动作）：
  - 给 fuling_knowledge / fuling_operation 补 032 → 单库单文件走 apply_migration.py；
  - staging 账号 GRANT（fuling_stg on fuling_ontology_stg）→ 管理员通道，不产品化。

用法：
  RAG_ENV=local   python scripts/apply_ontology_dbs.py            # dry-run 预览
  RAG_ENV=staging python scripts/apply_ontology_dbs.py --commit   # staging 重放
  # 生产：--commit --prod-ack 'PROD-RW:<YYYY-MM-DD>'（经 prod_access 四账号纪律）
"""
import argparse
import hashlib
import os
import re
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPTS_DIR)
for _p in (_REPO_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apply_migration as _am  # noqa: E402 — 守卫/切分/连接全部复用，单一事实源

SCHEMA_DIR = os.path.join(_REPO_ROOT, "schema")
CI_LOADER = os.path.join(_SCRIPTS_DIR, "ci_load_schema.sh")
# 台账基建：011 建 schema_migrations 表、032 补 checksum 列——MANIFEST 里 target=both，
# 本体库同样各一份（ci_load_schema.sh 的 both 语义），故固定纳入本工具的重放序列。
LEDGER_FILES = ("011_schema_migrations.sql", "032_schema_migrations_checksum.sql")
REQUIRED_COLLATION = "utf8mb4_unicode_ci"

EXIT_COLLATION_DRIFT = 5
EXIT_BAD_TARGET = 6


def _numeric_key(fn):
    m = re.match(r"(\d+)([a-z]?)", fn)
    return (int(m.group(1)), m.group(2)) if m else (10**9, fn)


def discover_ontology_migrations():
    """从 ci_load_schema.sh 的 MANIFEST 自动发现本体迁移族 + 台账基建，按编号排序。

    fail-closed：loader 不存在 / MANIFEST 解析不出 / 台账基建文件缺登记或 target 不是
    both / 发现的文件在 schema/ 里不存在——一律 RuntimeError（绝不静默按空集跑）。
    """
    try:
        text = open(CI_LOADER, encoding="utf-8").read()
    except OSError as e:
        raise RuntimeError("读不到 %s（MANIFEST 权威来源）：%s" % (CI_LOADER, e))
    m = re.search(r'MANIFEST="\n(.*?)"\n', text, re.S)
    if not m:
        raise RuntimeError("ci_load_schema.sh 里找不到 MANIFEST 块——loader 结构变了？")
    entries = {}
    for line in m.group(1).strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise RuntimeError("MANIFEST 行解析失败: %r" % line)
        entries[parts[0]] = parts[1]

    files = [fn for fn, target in entries.items() if target == "ontology"]
    if not files:
        raise RuntimeError("MANIFEST 里没有任何 target=ontology 的迁移——不可能，中止。")
    for fn in LEDGER_FILES:
        if entries.get(fn) != "both":
            raise RuntimeError(
                "台账基建 %s 应在 MANIFEST 且 target=both（实际 %r）——台账语义变了，中止。"
                % (fn, entries.get(fn)))
        files.append(fn)
    files.sort(key=_numeric_key)
    missing = [fn for fn in files if not os.path.isfile(os.path.join(SCHEMA_DIR, fn))]
    if missing:
        raise RuntimeError("MANIFEST 登记的文件在 schema/ 不存在: %s" % missing)
    return files


def _build_plan(files):
    """[(fn, sha256, statements)]；切分复用 _am._split_statements（引号感知 +
    DELIMITER fail-closed——本体族不用存储过程，撞到即说明文件族变质，照抛）。"""
    plan = []
    for fn in files:
        raw = open(os.path.join(SCHEMA_DIR, fn), "rb").read()
        plan.append((fn, hashlib.sha256(raw).hexdigest(),
                     _am._split_statements(raw.decode("utf-8"))))
    return plan


def _db_collation(conn, dbname):
    """库存在 → 返回 collation；不存在 → None。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
                    "WHERE SCHEMA_NAME=%s", (dbname,))
        row = cur.fetchone()
    return row[0] if row else None


def _ledger_row(conn, dbname, fn):
    """台账现状 (row_exists, checksum|None)；表缺失等异常 → (False, None)（011 会建表）。"""
    try:
        has_col = _am._ledger_has_checksum_col(conn, dbname)
        with conn.cursor() as cur:
            if has_col:
                cur.execute(f"SELECT checksum FROM `{dbname}`.schema_migrations "
                            "WHERE filename=%s", (fn,))
            else:
                cur.execute(f"SELECT NULL FROM `{dbname}`.schema_migrations "
                            "WHERE filename=%s", (fn,))
            row = cur.fetchone()
    except Exception:   # noqa: BLE001 — 缺表/缺库：按无记录（fail-open 仅限读现状）
        return (False, None)
    return (False, None) if row is None else (True, row[0])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="ontology 库建库 + 本体迁移族（011→027..030→032）可重放 apply")
    ap.add_argument("--commit", action="store_true", help="实际执行（默认 dry-run 只预览）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只预览（默认行为；显式传更清晰，与 --commit 互斥）")
    ap.add_argument("--prod-ack", default=None,
                    help="生产 apply 确认令牌（对齐 prod_access.get_prod_rw_conn）")
    ap.add_argument("--applied-by", default="scripts/apply_ontology_dbs.py")
    args = ap.parse_args(argv)
    if args.dry_run and args.commit:
        ap.error("--dry-run 与 --commit 互斥")

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    host = cfg.rds.host
    dbname = cfg.rds.ontology_database
    if "ontology" not in (dbname or ""):
        print(f"❌ cfg.rds.ontology_database={dbname!r} 不含 'ontology'——环境错配"
              f"（会把本体 DDL 灌进别的库），中止。")
        sys.exit(EXIT_BAD_TARGET)

    is_local, is_staging, is_prod = _am.classify_target(host, dbname)
    env_label = "local" if is_local else ("staging" if is_staging else "PRODUCTION")

    files = discover_ontology_migrations()
    plan = _build_plan(files)

    print(f"目标     : {dbname}  host={host}  环境={env_label}（物理指纹判定）")
    print(f"迁移序列 : {' → '.join(fn.split('_')[0] for fn, _, _ in plan)}"
          f"（{len(plan)} 个文件，MANIFEST 自动发现）")
    print(f"模式     : {'COMMIT（真写）' if args.commit else 'DRY-RUN（只预览，只读连接）'}")

    if not args.commit:
        conn = _am._connect_ro(cfg, is_prod)
        try:
            if conn is not None:
                coll = _db_collation(conn, dbname)
                if coll is None:
                    print(f"库现状   : {dbname} 不存在 → --commit 将建库"
                          f"（utf8mb4 / {REQUIRED_COLLATION}）")
                elif coll != REQUIRED_COLLATION:
                    print(f"⚠️ 库现状 : {dbname} 已存在但 collation={coll}"
                          f"（应为 {REQUIRED_COLLATION}，铁律 4）——--commit 会中止。")
                else:
                    print(f"库现状   : {dbname} 已存在（collation ✓）")
            for fn, checksum, statements in plan:
                tables = _am._table_names(statements)
                line = f"  · {fn}  语句 {len(statements)} 条"
                if tables:
                    line += f"  表 {tables}"
                if conn is not None:
                    existing = _am._existing_tables(conn, dbname, tables)
                    todo = [t for t in tables if t not in existing]
                    row_exists, old_ck = _ledger_row(conn, dbname, fn)
                    if row_exists and old_ck and old_ck != checksum:
                        line += f"\n    ⚠️ 台账已记且 checksum 不同（旧 {old_ck[:12]}…）" \
                                f"——--commit 会中止（漂移）。"
                    elif row_exists:
                        line += "  [台账已记——重放为幂等重执行]"
                    if tables:
                        line += f"\n    已存在 {sorted(existing)} | 待建 {todo}"
                print(line)
            print(f"\n[DRY-RUN] 不写库。--commit 将：建库(若缺) + 按序执行以上 {len(plan)} 个"
                  f"文件 + 统一记 {dbname}.schema_migrations（含 SHA-256）。")
        finally:
            if conn is not None:
                conn.close()
        return 0

    if is_prod and not args.prod_ack:
        print("\n❌ 目标为**生产库**，须显式 --prod-ack <当日 RW 令牌>（经 prod_access）。中止。")
        sys.exit(_am.EXIT_PROD_NO_ACK)

    conn = _am._connect_rw(cfg, args.prod_ack, is_prod)
    try:
        # 1) 建库（若缺）+ collation 铁律（已存在但漂移 → 中止，绝不静默沿用错 collation）
        coll = _db_collation(conn, dbname)
        if coll is None:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
                            f"CHARACTER SET utf8mb4 COLLATE {REQUIRED_COLLATION}")
            conn.commit()
            print(f"✅ CREATE DATABASE {dbname}（显式 {REQUIRED_COLLATION}，铁律 4）")
        elif coll != REQUIRED_COLLATION:
            print(f"❌ {dbname} 已存在但 collation={coll}（应为 {REQUIRED_COLLATION}）——"
                  f"跨库 JOIN 会 1267（staging 漂移事故同款）。请先人工修 collation。中止。")
            sys.exit(EXIT_COLLATION_DRIFT)
        else:
            print(f"库已存在 : {dbname}（collation ✓）")

        # 2) 漂移预检（全序列先检后写：中途才发现漂移会留下半截 apply）
        for fn, checksum, _statements in plan:
            row_exists, old_ck = _ledger_row(conn, dbname, fn)
            if row_exists and old_ck and old_ck != checksum:
                print(f"\n❌ 台账已记 {fn} 且 checksum 不同（台账 {old_ck[:12]}… ≠ 本文件 "
                      f"{checksum[:12]}…）——同版本内容被改过（漂移预备役），中止。"
                      f"修订已发布文件须走 NNNa 修订号（README 铁律 2）。")
                sys.exit(_am.EXIT_CHECKSUM_MISMATCH)

        # 3) 按序执行（文件自身幂等：IF NOT EXISTS / INSERT IGNORE / PREPARE 守卫）
        for fn, checksum, statements in plan:
            with conn.cursor() as cur:
                cur.execute(f"USE `{dbname}`")
                for s in statements:
                    cur.execute(s)
            conn.commit()
            print(f"✅ {dbname} ← {fn}（{len(statements)} 条, sha256={checksum[:12]}…）")

        # 4) 台账殿后（032 已就位 → 全部带 checksum；写失败 → exit 3 fail-closed）
        try:
            has_col = _am._ledger_has_checksum_col(conn, dbname)
            with conn.cursor() as cur:
                cur.execute(f"USE `{dbname}`")
                for fn, checksum, _statements in plan:
                    version = fn.split("_", 1)[0]
                    if has_col:
                        cur.execute(
                            "INSERT IGNORE INTO schema_migrations "
                            "(filename, version, applied_by, notes, checksum) "
                            "VALUES (%s,%s,%s,%s,%s)",
                            (fn, version, args.applied_by,
                             f"apply via script ({env_label})", checksum))
                    else:
                        cur.execute(
                            "INSERT IGNORE INTO schema_migrations "
                            "(filename, version, applied_by, notes) VALUES (%s,%s,%s,%s)",
                            (fn, version, args.applied_by,
                             f"apply via script ({env_label}); sha256={checksum}"))
            conn.commit()
            print(f"✅ 台账 {len(plan)} 行 → {dbname}.schema_migrations"
                  f"{'' if has_col else '（无 checksum 列——032 未生效？请人工核查）'}")
        except Exception as e:   # noqa: BLE001
            print(f"❌ DDL 已落定但台账写失败（{e}）——**exit {_am.EXIT_LEDGER_FAILED}**："
                  f"请人工补记后再继续。")
            sys.exit(_am.EXIT_LEDGER_FAILED)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
  - **台账跳过（批次4 P1-02）**：台账已记且 checksum 一致的文件 SKIP 不重执行
    （--force-replay 恢复全量重执行；文件自身仍须幂等守卫，重放才安全）；
  - **NNNa 修订感知（README 铁律 2）**：文件头 `-- revision: NNNa` 声明修订——
    原行 checksum 不同且修订行未记 → 执行守卫化文件并记 `<fn>@NNNa` 台账行
    （不改原行）；修订行已记但 checksum 又不同 → 中止（须再升修订号）；
  - 台账同名不同 checksum 且**无**修订声明 → 中止（漂移，exit 4）；
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
# P1-09（外审核查 2026-07-16）：MANIFEST 权威来源移到 schema/MIGRATION_MANIFEST.tsv
# （机器可读单一来源，ci_load_schema.sh / readiness / 本工具共用）。
MANIFEST_FILE = os.path.join(SCHEMA_DIR, "MIGRATION_MANIFEST.tsv")
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
    """从 schema/MIGRATION_MANIFEST.tsv 自动发现本体迁移族 + 台账基建，按编号排序
    （P1-09：MANIFEST 单一来源从 ci_load_schema.sh 内嵌块移到 schema/ 下机器可读文件）。

    fail-closed：manifest 不存在 / 解析不出 / 台账基建文件缺登记或 target 不是
    both / 发现的文件在 schema/ 里不存在——一律 RuntimeError（绝不静默按空集跑）。
    """
    try:
        text = open(MANIFEST_FILE, encoding="utf-8").read()
    except OSError as e:
        raise RuntimeError("读不到 %s（MANIFEST 权威来源）：%s" % (MANIFEST_FILE, e))
    entries = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise RuntimeError("MANIFEST 行解析失败: %r" % line)
        entries[parts[0]] = parts[1]
    if not entries:
        raise RuntimeError("%s 为空——MANIFEST 权威来源缺失，中止。" % MANIFEST_FILE)

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


_REVISION_RE = re.compile(r"^--\s*revision:\s*([0-9a-zA-Z]+)\s*$", re.M)


def _declared_revision(text):
    """文件头 `-- revision: NNNa` 修订声明（铁律 2）；无声明 → None。"""
    m = _REVISION_RE.search(text)
    return m.group(1) if m else None


def _build_plan(files):
    """[(fn, sha256, statements, revision)]；切分复用 _am._split_statements（引号感知 +
    DELIMITER fail-closed——本体族不用存储过程，撞到即说明文件族变质，照抛）。"""
    plan = []
    for fn in files:
        raw = open(os.path.join(SCHEMA_DIR, fn), "rb").read()
        text = raw.decode("utf-8")
        plan.append((fn, hashlib.sha256(raw).hexdigest(),
                     _am._split_statements(text), _declared_revision(text)))
    return plan


def _db_collation(conn, dbname):
    """库存在 → 返回 collation；不存在 → None。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
                    "WHERE SCHEMA_NAME=%s", (dbname,))
        row = cur.fetchone()
    return row[0] if row else None


def _ledger_rows(conn, dbname, fn):
    """台账现状 {filename: checksum|None}——含原行与全部修订行（fn@NNNa）。
    表缺失等异常 → {}（011 会建表；fail-open 仅限读现状）。"""
    try:
        has_col = _am._ledger_has_checksum_col(conn, dbname)
        with conn.cursor() as cur:
            col = "checksum" if has_col else "NULL"
            cur.execute(f"SELECT filename, {col} FROM `{dbname}`.schema_migrations "
                        "WHERE filename=%s OR filename LIKE %s", (fn, fn + "@%"))
            return {r[0]: r[1] for r in (cur.fetchall() or [])}
    except Exception:   # noqa: BLE001 — 缺表/缺库：按无记录
        return {}


def _plan_action(rows, fn, checksum, rev):
    """裁决单文件动作（批次4 P1-02）。返回 (action, detail)：
    - skip   任一台账行（原行或修订行）checksum 与本文件一致 → 已应用，跳过；
    - apply  无台账行（全新）/ 仅有 pre-032 无 checksum 行（无从校验，按幂等重放）；
    - revise 原行在但内容已修订且声明了 `-- revision:`、该修订行未记
             → 执行守卫化文件并记 `<fn>@<rev>` 行（detail=修订行 filename）；
    - drift  内容变了但无修订声明 / 声明的修订行已记而 checksum 又不同 → 中止 exit 4。"""
    if not rows:
        return "apply", None
    if any(ck == checksum for ck in rows.values() if ck):
        return "skip", None
    if all(ck is None for ck in rows.values()):
        return "apply", "台账行无 checksum（pre-032）——按幂等重放"
    if rev:
        rev_fn = f"{fn}@{rev}"
        if rev_fn in rows:
            return "drift", f"修订 {rev} 已记但 checksum 又不同——须再升修订号（铁律 2）"
        return "revise", rev_fn
    return "drift", None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="ontology 库建库 + 本体迁移族（011→027..030→032）可重放 apply")
    ap.add_argument("--commit", action="store_true", help="实际执行（默认 dry-run 只预览）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只预览（默认行为；显式传更清晰，与 --commit 互斥）")
    ap.add_argument("--prod-ack", default=None,
                    help="生产 apply 确认令牌（对齐 prod_access.get_prod_rw_conn）")
    ap.add_argument("--applied-by", default="scripts/apply_ontology_dbs.py")
    ap.add_argument("--force-replay", action="store_true",
                    help="忽略台账跳过、全量重执行（旧行为；文件自身幂等守卫兜底。"
                         "漂移中止不受本开关影响）")
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
    print(f"迁移序列 : {' → '.join(fn.split('_')[0] for fn, _, _, _ in plan)}"
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
            for fn, checksum, statements, rev in plan:
                tables = _am._table_names(statements)
                line = f"  · {fn}  语句 {len(statements)} 条"
                if tables:
                    line += f"  表 {tables}"
                if conn is not None:
                    existing = _am._existing_tables(conn, dbname, tables)
                    todo = [t for t in tables if t not in existing]
                    action, detail = _plan_action(_ledger_rows(conn, dbname, fn),
                                                  fn, checksum, rev)
                    if action == "drift":
                        line += f"\n    ⚠️ 台账 checksum 漂移（{detail or '无修订声明'}）" \
                                f"——--commit 会中止。"
                    elif action == "skip":
                        line += "  [台账已记 checksum ✓——SKIP（--force-replay 可重执行）]"
                    elif action == "revise":
                        line += f"  [修订 {rev}：将重放守卫化文件并记 {detail}]"
                    if tables:
                        line += f"\n    已存在 {sorted(existing)} | 待建 {todo}"
                print(line)
            print(f"\n[DRY-RUN] 不写库。--commit 将：建库(若缺) + 按上列动作执行 {len(plan)} 个"
                  f"文件（skip 不重执行）+ 记 {dbname}.schema_migrations（含 SHA-256）。")
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

        # 2) 动作裁决预检（全序列先检后写：中途才发现漂移会留下半截 apply）
        actions = {}
        for fn, checksum, _statements, rev in plan:
            action, detail = _plan_action(_ledger_rows(conn, dbname, fn), fn, checksum, rev)
            if action == "drift":
                print(f"\n❌ 台账已记 {fn} 且 checksum 不同（{detail or '同版本内容被改过'}）"
                      f"——中止。修订已发布文件须走 NNNa 修订号 + 文件头 `-- revision:` 声明"
                      f"（README 铁律 2）。")
                sys.exit(_am.EXIT_CHECKSUM_MISMATCH)
            actions[fn] = (action, detail)

        # 3) 按动作执行（skip=台账已记且 checksum 一致，批次4 P1-02；--force-replay 全量重放。
        #    执行路径仍要求文件自身幂等：IF NOT EXISTS / INSERT IGNORE / PREPARE 守卫）
        skipped = 0
        for fn, checksum, statements, _rev in plan:
            action, _detail = actions[fn]
            if action == "skip" and not args.force_replay:
                skipped += 1
                print(f"⏭️  {dbname} ← {fn} SKIP（台账已记 checksum ✓；--force-replay 可重执行）")
                continue
            with conn.cursor() as cur:
                cur.execute(f"USE `{dbname}`")
                for s in statements:
                    cur.execute(s)
            conn.commit()
            tag = "（修订重放）" if action == "revise" else ""
            print(f"✅ {dbname} ← {fn}{tag}（{len(statements)} 条, sha256={checksum[:12]}…）")

        # 4) 台账殿后（032 已就位 → 全部带 checksum；写失败 → exit 3 fail-closed）。
        #    revise 文件记 `<fn>@<rev>` 修订行（version=rev；原行不动，铁律 2）。
        try:
            has_col = _am._ledger_has_checksum_col(conn, dbname)
            with conn.cursor() as cur:
                cur.execute(f"USE `{dbname}`")
                for fn, checksum, _statements, rev in plan:
                    action, detail = actions[fn]
                    if action == "revise":
                        row_fn, version = detail, rev
                    else:
                        row_fn, version = fn, fn.split("_", 1)[0]
                    if has_col:
                        cur.execute(
                            "INSERT IGNORE INTO schema_migrations "
                            "(filename, version, applied_by, notes, checksum) "
                            "VALUES (%s,%s,%s,%s,%s)",
                            (row_fn, version, args.applied_by,
                             f"apply via script ({env_label})", checksum))
                    else:
                        cur.execute(
                            "INSERT IGNORE INTO schema_migrations "
                            "(filename, version, applied_by, notes) VALUES (%s,%s,%s,%s)",
                            (row_fn, version, args.applied_by,
                             f"apply via script ({env_label}); sha256={checksum}"))
            conn.commit()
            print(f"✅ 台账 {len(plan)} 行 → {dbname}.schema_migrations"
                  f"（本轮 SKIP {skipped} 个文件）"
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

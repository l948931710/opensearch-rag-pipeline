# -*- coding: utf-8 -*-
"""schema/ DDL ↔ 代码 列契约 parity（F-35 防复发，2026-07-01）。

010 漂移的根因：代码 INSERT 的列（normalized_gap_query）不在权威 DDL 里，生产表
靠一次性 scratch 脚本悄悄多了一列——按 schema/ 重建的环境（staging/灾备）提交贡献
必 1054。本文件把「代码读写的列必须存在于权威 DDL」钉成测试。
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "schema"


def _ddl_columns(sql_path: Path, table: str) -> set:
    """从 CREATE TABLE 语句提取列名集合（行首标识符启发式，够用且稳定）。"""
    text = sql_path.read_text(encoding="utf-8")
    m = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\((.*?)\)\s*ENGINE",
                  text, re.S | re.I)
    assert m, f"{sql_path.name} 里找不到 CREATE TABLE {table}"
    cols = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        mm = re.match(r"^`?([a-z_][a-z0-9_]*)`?\s", line)
        if mm and mm.group(1).upper() not in (
                "PRIMARY", "UNIQUE", "INDEX", "KEY", "CONSTRAINT", "FOREIGN"):
            cols.add(mm.group(1))
    return cols


def test_kb_contribution_insert_columns_exist_in_ddl():
    """提交端点 INSERT 的每一列都必须在 schema/010 权威 DDL 里（F-35 主修复回归）。"""
    ddl_cols = _ddl_columns(SCHEMA / "010_kb_contribution.sql", "kb_contribution")
    assert "normalized_gap_query" in ddl_cols, "F-35：权威 DDL 必须含 normalized_gap_query"

    src = (REPO / "opensearch_pipeline" / "routes" / "contribution.py").read_text(encoding="utf-8")
    m = re.search(r"INSERT INTO \{_op_db\(\)\}\.kb_contribution\s*\((.*?)\)", src, re.S)
    assert m, "找不到 kb_contribution 的 INSERT 列清单"
    insert_cols = {c.strip() for c in m.group(1).replace("\n", " ").split(",")}
    missing = insert_cols - ddl_cols
    assert not missing, f"代码 INSERT 的列不在权威 DDL：{sorted(missing)}（先改 schema/010 再改代码）"


def test_kb_contribution_select_columns_exist_in_ddl():
    """_CONTRIB_COLS（列表/详情 SELECT 列清单）同样受权威 DDL 约束。"""
    from opensearch_pipeline.routes.contribution import _CONTRIB_COLS
    ddl_cols = _ddl_columns(SCHEMA / "010_kb_contribution.sql", "kb_contribution")
    select_cols = {c.strip() for c in _CONTRIB_COLS.split(",")}
    missing = select_cols - ddl_cols
    assert not missing, f"_CONTRIB_COLS 引用的列不在权威 DDL：{sorted(missing)}"


def test_schema_migrations_ledger_ddl_exists():
    """011 台账 DDL 存在且含关键列（apply 脚本与 README 规程依赖）。"""
    cols = _ddl_columns(SCHEMA / "011_schema_migrations.sql", "schema_migrations")
    for c in ("filename", "version", "applied_at", "applied_by", "notes"):
        assert c in cols, f"schema_migrations 缺列 {c}"


def test_perf_index_migration_exists():
    """012 复合索引迁移在册（性能第一梯队 #1），且列序 = (answer_status, created_at)。"""
    text = (SCHEMA / "012_qa_session_log_perf_index.sql").read_text(encoding="utf-8")
    assert re.search(r"CREATE INDEX idx_status_created ON qa_session_log"
                     r" \(answer_status, created_at\)", text)

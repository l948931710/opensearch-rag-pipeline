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


def _parse_create_block_cols(block: str) -> set:
    """CREATE TABLE 括号体 → 列名集合（行首标识符启发式：列名小写、SQL 关键字大写，够用且稳定）。"""
    cols = set()
    for line in block.splitlines():
        line = line.strip()
        mm = re.match(r"^`?([a-z_][a-z0-9_]*)`?\s", line)
        if mm and mm.group(1).upper() not in (
                "PRIMARY", "UNIQUE", "INDEX", "KEY", "CONSTRAINT", "FOREIGN"):
            cols.add(mm.group(1))
    return cols


def _ddl_columns(sql_path: Path, table: str) -> set:
    """从单文件的 CREATE TABLE 语句提取列名集合。"""
    text = sql_path.read_text(encoding="utf-8")
    m = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\((.*?)\)\s*ENGINE",
                  text, re.S | re.I)
    assert m, f"{sql_path.name} 里找不到 CREATE TABLE {table}"
    return _parse_create_block_cols(m.group(1))


def _table_columns_across_schema(table: str) -> set:
    """表在 schema/ 全集里的权威列集合：CREATE TABLE 基线 ∪ 后续迁移增列
    （002 的 _add_column_if_not_exists 守卫 / 006 式 ALTER TABLE ... ADD COLUMN）。

    列名契约测试用它做「代码引用的列必须存在」的裁判——单文件版 _ddl_columns 看不见
    跨文件演进（qa_session_log 的 top_score 在 002、conversation_id 在 006）。"""
    cols = set()
    for sql_path in sorted(SCHEMA.glob("*.sql")):
        text = sql_path.read_text(encoding="utf-8")
        m = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?`?{table}`?\s*\((.*?)\)\s*ENGINE",
                      text, re.S | re.I)
        if m:
            cols |= _parse_create_block_cols(m.group(1))
        cols |= set(re.findall(
            rf"_add_column_if_not_exists\(\s*'{table}'\s*,\s*'([a-z0-9_]+)'", text))
        cols |= set(re.findall(
            rf"ALTER TABLE\s+`?{table}`?\s+ADD COLUMN\s+`?([a-z_][a-z0-9_]*)`?", text, re.I))
    assert cols, f"schema/ 全集里找不到表 {table} 的任何 DDL"
    return cols


def test_qa_session_log_columns_union_across_files():
    """跨文件联合解析自检 + 2026-07-11 staging P1 事实钉：qa_session_log 的问题文本列
    是 query_text（001 基线），从来没有 question——kb_console 曾 SELECT q.question，
    mock 单测全绿、staging 真库 1054。top_score/conversation_id 证明 002/006 的增列
    形态都被联合集看见。"""
    cols = _table_columns_across_schema("qa_session_log")
    for c in ("query_text", "cited_docs_json", "top_score", "conversation_id"):
        assert c in cols, f"qa_session_log 权威列集缺 {c}——联合解析回归了"
    assert "question" not in cols


def test_kb_contribution_insert_columns_exist_in_ddl():
    """提交端点 INSERT 的每一列都必须在权威 DDL 里（F-35 主修复回归）。

    ⚠️ 判据用**跨文件联合集**而非单看 010（2026-08-07）：kb_contribution 已开始跨文件演进
    （067 加 category_dept_id），单文件版会把「列在 067 里、代码用了」误判成漂移。F-35 要防的
    是「代码读写的列在 schema/ 全集里找不到」，联合集才是那条判据；010 自身的基线断言另置。"""
    ddl_cols = _table_columns_across_schema("kb_contribution")
    assert "normalized_gap_query" in _ddl_columns(
        SCHEMA / "010_kb_contribution.sql", "kb_contribution"), \
        "F-35：权威 DDL 必须含 normalized_gap_query"

    src = (REPO / "opensearch_pipeline" / "routes" / "contribution.py").read_text(encoding="utf-8")
    # findall（非 search）：提交端点自 2026-08-07 起有 **两条**字面 INSERT（组码轴 / node 轴），
    # 只看第一条会让 node 轴那条的列清单逃过 F-35 守卫。
    blocks = re.findall(r"INSERT INTO \{_op_db\(\)\}\.kb_contribution\s*\((.*?)\)", src, re.S)
    assert len(blocks) >= 2, "找不到 kb_contribution 的两条 INSERT 列清单（组码轴 / node 轴）"
    insert_cols = {c.strip() for b in blocks for c in b.replace("\n", " ").split(",")}
    missing = insert_cols - ddl_cols
    assert "category_dept_id" in insert_cols, "node 轴 INSERT 必须写 category_dept_id（方案 M1/M9）"
    assert not missing, f"代码 INSERT 的列不在权威 DDL：{sorted(missing)}（先改 schema/ 再改代码）"


def test_kb_contribution_select_columns_exist_in_ddl():
    """_CONTRIB_COLS（列表/详情 SELECT 列清单）同样受权威 DDL 约束（同上，判据=跨文件联合集）。

    node 轴清单 `_CONTRIB_COLS_NODE` 一并覆盖——它才是 067 已 apply 环境实际下发的 SELECT。"""
    from opensearch_pipeline.routes.contribution import _CONTRIB_COLS, _CONTRIB_COLS_NODE
    ddl_cols = _table_columns_across_schema("kb_contribution")
    select_cols = {c.strip() for c in (_CONTRIB_COLS + "," + _CONTRIB_COLS_NODE).split(",")}
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

# -*- coding: utf-8 -*-
"""tests/test_batch5_ingest_fences.py — ultra 评审批次5：摄取/编排租约栅栏与计数镜像。

覆盖（全 mock，无真库）：
1. stage-1 隔离行 SQL 排除：scanner 认领 SELECT 与 drain 计数共用 ingest_policy 单一
   来源谓词（计数↔认领镜像纪律）；process_quarantine=True 时谓词关闭。
2. DAG-3 失败回滚补 PR-4 租约纪律（源检，与 test_j_rollback_reads_result_ctx 同式）。
3. 分类成功/冻结/贡献三持久化路径 fenced-write：dv 先写带栅栏；LeaseLost ⇒ 弃单文档
   继续批（不 abort 节点、meta 不写、canonicals 剔除）。
4. parity 失败同事务把 dv 从 PROCESSING CAS 回 FAILED（保 PENDING_DELETE 握手）。
"""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("RAG_SIMULATE", "true")

from tests.test_unfrozen_rechunk_guard import (  # noqa: E402 — 复用同一 mock 语义
    DB_PATH,
    LLM_PATH,
    _doc,
    _FakeConn,
    _FakeCursor,
    _good_classification,
)


# ─────────────────────────────────────────────────────────────────────
# 1. stage-1 隔离行排除（scanner 认领 + drain 计数镜像）
# ─────────────────────────────────────────────────────────────────────


def test_quarantine_like_pattern_escapes_underscore():
    from opensearch_pipeline.ingest_policy import stage1_quarantine_like_pattern
    pat = stage1_quarantine_like_pattern()
    assert pat == "%\\_quarantine/%"          # '_' 是 LIKE 单字符通配，必须反斜杠转义


class _ScanCursor(_FakeCursor):
    """记录 SQL+params；SELECT 一律空结果（只验谓词形状）。"""


def _scan_ctx(executed, process_quarantine=None):
    ctx = {"simulate_db": False, "raw_tasks": []}
    if process_quarantine is not None:
        ctx["process_quarantine"] = process_quarantine
    return ctx


@patch(DB_PATH)
def test_scanner_sql_excludes_quarantine_rows(mock_db):
    import opensearch_pipeline.pipeline_nodes as pn
    executed = []
    mock_db.side_effect = lambda *a, **k: _FakeConn(set(), executed)
    pn.node_scan_raw_files(_scan_ctx(executed))
    sels = [(s, p) for s, p in executed if "FROM document_version" in (s or "")]
    assert sels, "scanner 生产分支必须发出认领 SELECT"
    sql, params = sels[0]
    assert "raw_key NOT LIKE %s" in sql
    assert params == ("%\\_quarantine/%",)


@patch(DB_PATH)
def test_scanner_sql_keeps_quarantine_when_flag_on(mock_db):
    """process_quarantine=True 的未来通道保留：SQL 谓词关闭（Python 过滤同门放行）。"""
    import opensearch_pipeline.pipeline_nodes as pn
    executed = []
    mock_db.side_effect = lambda *a, **k: _FakeConn(set(), executed)
    pn.node_scan_raw_files(_scan_ctx(executed, process_quarantine=True))
    sql, params = [(s, p) for s, p in executed if "FROM document_version" in (s or "")][0]
    assert "NOT LIKE" not in sql
    assert not params


def test_count_pending_stage1_mirrors_scanner_predicate(monkeypatch):
    """drain 计数与认领 SELECT 同一谓词（不镜像 = 只剩隔离行时无进展守卫误杀 stage-1）。"""
    from opensearch_pipeline.dataworks_orchestrator import _count_pending_rows
    executed = []

    class _CntCur(_FakeCursor):
        def execute(self, sql, params=None):
            self._executed.append((sql, params))
            self._last = [(7,)]

        def fetchone(self):
            return (7,)

    class _CntConn(_FakeConn):
        def cursor(self, *a, **k):
            return _CntCur(self._existing, self._executed)

    monkeypatch.setattr("opensearch_pipeline.pipeline_nodes._get_db_conn",
                        lambda **k: _CntConn(set(), executed))
    assert _count_pending_rows(1) == 7
    sql, params = executed[0]
    assert "raw_key NOT LIKE %s" in sql
    assert params == ("%\\_quarantine/%",)


# ─────────────────────────────────────────────────────────────────────
# 2. DAG-3 失败回滚租约纪律（源检，与 test_j 同式）
# ─────────────────────────────────────────────────────────────────────


def test_dag3_rollback_carries_lease_discipline():
    import inspect

    from opensearch_pipeline.dataworks_orchestrator import run_stage
    src = inspect.getsource(run_stage)
    rb = src[src.index("Rolling back PROCESSING locks"):]
    assert "fence_where_sql" in rb, "回滚 WHERE 必须带 epoch 栅栏（防覆盖新持有者在跑锁）"
    assert "clear_set_sql" in rb, "回滚 SET 必须清租约列（FAILED 行残留租约=takeover 地雷）"
    assert ".epoch(" in rb and "lease already lost" in rb, \
        "节点内已判 LeaseLost 的 key 必须跳过（归新持有者收敛）"


# ─────────────────────────────────────────────────────────────────────
# 3. 分类持久化 fenced-write（flag on 行为级）
# ─────────────────────────────────────────────────────────────────────


class _LeaseCursor(_FakeCursor):
    """_FakeCursor + 租约登记 SELECT 应答 + 分类 dv UPDATE 的 rowcount 脚本。"""

    def __init__(self, existing, executed, lease_rows, dv_rowcount=1):
        super().__init__(existing, executed)
        self._lease_rows = lease_rows
        self._dv_rowcount = dv_rowcount

    def execute(self, sql, params=None):
        s = (sql or "").strip().upper()
        if s.startswith("SELECT") and "LEASE_EPOCH" in s:
            self._executed.append((sql, params))
            self._last = list(self._lease_rows)
            self.rowcount = len(self._lease_rows)
            return
        super().execute(sql, params)
        if "UPDATE DOCUMENT_VERSION" in s and "CONTENT_CLASSIFIED" in s:
            self.rowcount = self._dv_rowcount
        else:
            self.rowcount = 1


class _LeaseConn(_FakeConn):
    def __init__(self, existing, executed, lease_rows, dv_rowcount=1):
        super().__init__(existing, executed)
        self._lease_rows = lease_rows
        self._dv_rowcount = dv_rowcount

    def cursor(self, *a, **k):
        return _LeaseCursor(self._existing, self._executed, self._lease_rows,
                            self._dv_rowcount)


def _run_classify(monkeypatch, mock_db, mock_llm, dv_rowcount):
    monkeypatch.setenv("RAG_INGEST_LEASE_ENABLE", "true")
    mock_llm.return_value = _good_classification()
    executed = []
    docs = [_doc("d1")]
    mock_db.side_effect = lambda *a, **k: _LeaseConn(
        set(), executed, [("d1", 1, 7)], dv_rowcount)
    ctx = {"canonicals": docs, "simulate_db": False, "simulate_api": False}
    import opensearch_pipeline.pipeline_nodes as pn
    pn.node_classify_and_risk_assess(ctx)
    return ctx, docs, executed


@patch(LLM_PATH)
@patch(DB_PATH)
def test_classify_success_persist_is_fenced_dv_first(mock_db, mock_llm, monkeypatch,
                                                     llm_key_present):
    """批次5（ultra pipeline_nodes:1878）：成功路径 dv 先写带 epoch 栅栏，过验才写 meta。"""
    from opensearch_pipeline.ingest_lease import holder_id
    ctx, docs, executed = _run_classify(monkeypatch, mock_db, mock_llm, dv_rowcount=1)
    dv_ix = [i for i, (s, _p) in enumerate(executed)
             if "UPDATE document_version" in s and "CONTENT_CLASSIFIED" in s]
    meta_ix = [i for i, (s, _p) in enumerate(executed)
               if "UPDATE document_meta" in s and "category_l1" in s]
    assert dv_ix and meta_ix and dv_ix[0] < meta_ix[0], "dv（带栅栏）必须先于 meta 写"
    dv_sql, dv_params = executed[dv_ix[0]]
    assert "lease_holder = %s AND lease_epoch = %s" in dv_sql
    assert dv_params[-2:] == (holder_id(), 7)
    assert [d["doc_id"] for d in ctx["canonicals"]] == ["d1"]   # 成功保留


@patch(LLM_PATH)
@patch(DB_PATH)
def test_classify_lease_lost_abandons_doc_not_node(mock_db, mock_llm, monkeypatch,
                                                   llm_key_present):
    """LeaseLost（栅栏 rowcount=0）⇒ 弃单文档继续批：meta 不写、canonicals 剔除、节点不抛。"""
    ctx, docs, executed = _run_classify(monkeypatch, mock_db, mock_llm, dv_rowcount=0)
    assert not any("UPDATE document_meta" in s and "category_l1" in s
                   for s, _p in executed), "栅栏未过：document_meta 绝不落库"
    assert ctx["canonicals"] == [], "丢租约文档必须从 canonicals 剔除（归新持有者）"


# ─────────────────────────────────────────────────────────────────────
# 4. parity 失败同事务 CAS dv PROCESSING→FAILED
# ─────────────────────────────────────────────────────────────────────


def test_parity_failure_cas_dv_to_failed_same_txn(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn
    executed = []

    class _Cur:
        rowcount = 1

        def execute(self, sql, params=None):
            executed.append((sql, params))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def begin(self):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(pn, "_get_db_conn", lambda **k: _Conn())
    monkeypatch.setattr(pn, "_fail_chunks_with_retry_budget",
                        lambda cur, ids, **k: [])
    monkeypatch.setattr(pn, "_mark_docs_needs_review_for_dead", lambda cur, dead: None)
    ch = SimpleNamespace(chunk_id="c1", doc_id="D1", version_no=2,
                         index_status=None, index_error_code=None, index_error_message=None)
    ctx = {}
    cfg = SimpleNamespace(rds=SimpleNamespace(host="localhost"))
    with pytest.raises(RuntimeError, match="Stage-3 parity"):
        pn._persist_parity_failed_and_raise(ctx, cfg, [ch], [], 2)
    dv_sqls = [" ".join(s.split()) for s, _p in executed if "UPDATE document_version" in s]
    assert any("SET index_status = 'FAILED'" in s
               and "AND index_status = 'PROCESSING'" in s for s in dv_sqls), \
        "parity 失败必须同事务把 dv 从 PROCESSING CAS 回 FAILED（否则卡 2h 失效锁窗）"
    assert ("D1", 2) in ctx["failed_doc_versions"]

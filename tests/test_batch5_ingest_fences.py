# -*- coding: utf-8 -*-
"""tests/test_batch5_ingest_fences.py — 批次5 stage-1 隔离行 SQL 排除（2026-08-02 自 op0 移植）。

只含 op0 同名文件的隔离行族用例（+1 个 main 特有的 node-ACL 1054 回退双臂带参测试）；
op0 文件里的 lease 族用例（DAG-3 回滚纪律/分类 fenced-write/parity CAS）在 main 以
tests/test_ingest_lease_port.py 的**修正语义**另立（R1：verify_for_update 判租不依赖
rowcount；两支对读时以 port 版为 main 权威）。

覆盖：scanner 认领 SELECT 与 drain 计数共用 ingest_policy 单一来源谓词（计数↔认领
镜像纪律）；process_quarantine=True 时谓词关闭；node-ACL 双 SQL seam 两臂同传参数。
"""
import os
from unittest.mock import patch

os.environ.setdefault("RAG_SIMULATE", "true")

from tests.test_unfrozen_rechunk_guard import (  # noqa: E402 — 复用同一 mock 语义
    DB_PATH,
    _FakeConn,
    _FakeCursor,
)


def test_quarantine_like_pattern_escapes_underscore():
    from opensearch_pipeline.ingest_policy import stage1_quarantine_like_pattern
    pat = stage1_quarantine_like_pattern()
    assert pat == "%\\_quarantine/%"          # '_' 是 LIKE 单字符通配，必须反斜杠转义


def _scan_ctx(process_quarantine=None):
    ctx = {"simulate_db": False, "raw_tasks": []}
    if process_quarantine is not None:
        ctx["process_quarantine"] = process_quarantine
    return ctx


@patch(DB_PATH)
def test_scanner_sql_excludes_quarantine_rows(mock_db):
    import opensearch_pipeline.pipeline_nodes as pn
    executed = []
    mock_db.side_effect = lambda *a, **k: _FakeConn(set(), executed)
    pn.node_scan_raw_files(_scan_ctx())
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
    pn.node_scan_raw_files(_scan_ctx(process_quarantine=True))
    sql, params = [(s, p) for s, p in executed if "FROM document_version" in (s or "")][0]
    assert "NOT LIKE" not in sql
    assert not params


@patch(DB_PATH)
def test_scanner_1054_fallback_keeps_quarantine_params_both_arms(mock_db, monkeypatch):
    """main 特有适配（codex 评审要求）：node-ACL 双 SQL seam 下，060 未 apply 的 1054
    回退臂必须同样携带隔离参数——否则降级环境的 scanner 会静默放进隔离行。"""
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(pn, "_NODE_MODE_COLS_MISSING_UNTIL", 0)
    executed = []

    class _1054Cursor(_FakeCursor):
        def execute(self, sql, params=None):
            if "acl_mode" in (sql or "") and "FROM document_version" in (sql or ""):
                self._executed.append((sql, params))
                raise Exception(1054, "Unknown column 'dm.acl_mode'")
            super().execute(sql, params)

    class _1054Conn(_FakeConn):
        def cursor(self, *a, **k):
            return _1054Cursor(self._existing, self._executed)

    mock_db.side_effect = lambda *a, **k: _1054Conn(set(), executed)
    pn.node_scan_raw_files(_scan_ctx())
    sels = [(s, p) for s, p in executed if "FROM document_version" in (s or "")]
    assert len(sels) == 2, "应先试 node 臂（1054）再回退 legacy 臂"
    for sql, params in sels:
        assert "raw_key NOT LIKE %s" in sql
        assert params == ("%\\_quarantine/%",)


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

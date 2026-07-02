# -*- coding: utf-8 -*-
"""retention.py（F-36 日志/审计表留存）回归测试。

覆盖：simulate skip、dry-run 只数不删、RAG_RETENTION_ENABLE 双闸、批量循环终止、
qa_rows 的 rollup 活性守卫、findings 的当前版本守卫 SQL、窗口停用、exit code。
"""
import datetime

import pytest

from opensearch_pipeline import retention
from opensearch_pipeline.config import get_config


class _ScriptedCursor:
    """按 SQL 关键词回放结果的假游标；记录所有执行过的 SQL。"""

    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("SELECT COUNT(*), MAX(metric_date)"):
            self._row = self._conn.rollup_state
        elif s.startswith("SELECT DATEDIFF"):
            self._row = (self._conn.rollup_lag_days,)
        elif s.startswith("SELECT COUNT(*)"):
            self._row = (self._conn.affected,)
        elif s.startswith("SELECT f.id"):
            batch = self._conn.id_batches.pop(0) if self._conn.id_batches else []
            self._rows = [(i,) for i in batch]
            self._row = None
        elif s.startswith(("DELETE", "UPDATE")):
            self.rowcount = self._conn.act_rowcounts.pop(0) if self._conn.act_rowcounts else 0
            self._conn.acts += 1
        return None

    def fetchone(self):
        return getattr(self, "_row", None)

    def fetchall(self):
        return getattr(self, "_rows", [])


class _ScriptedConn:
    def __init__(self, *, affected=0, act_rowcounts=None, rollup_state=(1, datetime.date.today()),
                 rollup_lag_days=0, id_batches=None):
        self.affected = affected
        self.act_rowcounts = list(act_rowcounts or [])
        self.rollup_state = rollup_state
        self.rollup_lag_days = rollup_lag_days
        self.id_batches = list(id_batches or [])
        self.executed = []
        self.acts = 0
        self.commits = 0

    def cursor(self):
        return _ScriptedCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def live_db(monkeypatch):
    """把 config 切出 simulate（retention 才会真跑），host 保持 localhost（非生产目标）。"""
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    return cfg


def test_simulate_mode_skips():
    rep = retention.run_retention()
    assert all(r.get("skipped") == "simulate" for r in rep.values())


def test_dry_run_counts_without_acting(monkeypatch, live_db):
    conn = _ScriptedConn(affected=1234)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(only=["audit"])
    assert rep["audit"]["ok"] and rep["audit"]["dry_run"] and rep["audit"]["affected"] == 1234
    assert conn.acts == 0 and conn.commits == 0, "dry-run 绝不执行 DELETE/UPDATE、绝不 commit"


def test_commit_requires_enable_flag(monkeypatch, live_db):
    monkeypatch.delenv("RAG_RETENTION_ENABLE", raising=False)
    with pytest.raises(RuntimeError, match="RAG_RETENTION_ENABLE"):
        retention.run_retention(commit=True, only=["audit"])


def test_commit_batches_until_drained(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=7, act_rowcounts=[5, 2])   # 两批：5 + 2(<batch) 止
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["audit"], batch=5)
    assert rep["audit"]["ok"] and rep["audit"]["deleted"] == 7 and rep["audit"]["batches"] == 2
    assert conn.commits == 2, "每批一个短事务提交"


def test_qa_rows_blocked_when_rollup_empty(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=100, rollup_state=(0, None))
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["qa_rows"])
    assert rep["qa_rows"].get("blocked") and not rep["qa_rows"]["ok"]
    assert conn.acts == 0, "rollup 从未跑过时绝不删原始 qa 行"


def test_qa_rows_blocked_when_rollup_stale(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=100, rollup_state=(50, datetime.date(2026, 1, 1)),
                         rollup_lag_days=30)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["qa_rows"])
    assert "滞后" in rep["qa_rows"].get("blocked", "")
    assert conn.acts == 0


def test_qa_rows_proceeds_when_rollup_fresh(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=3, act_rowcounts=[3], rollup_lag_days=1)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["qa_rows"], batch=5000)
    assert rep["qa_rows"]["ok"] and rep["qa_rows"]["deleted"] == 3


def test_findings_deletes_by_ids_with_current_version_guard(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=2, id_batches=[[11, 12]], act_rowcounts=[2])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(commit=True, only=["findings"], batch=5000)
    assert rep["findings"]["ok"] and rep["findings"]["deleted"] == 2
    sqls = " || ".join(s for s, _ in conn.executed)
    assert "current_version_no" in sqls, "findings 必须带当前版本守卫（现役版本的 finding 永不删）"
    assert "WHERE id IN (11,12)" in sqls, "多表条件删除走 select-PK-then-delete 两步批"
    assert "CONVERT(f.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci" in sqls, (
        "doc_id JOIN 必须 collation-cast——document_sensitive_finding 是 _0900_ai_ci、"
        "document_meta 是 _unicode_ci，裸 JOIN 生产实测 1267（2026-07-02）")


def test_window_zero_disables_job(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_AUDIT_MONTHS", "0")
    conn = _ScriptedConn(affected=999)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.run_retention(only=["audit"])
    assert rep["audit"]["ok"] and "window<=0" in rep["audit"]["skipped"]
    assert not conn.executed, "停用作业连 COUNT 都不应执行"


def test_qa_blobs_uses_update_null_not_delete(monkeypatch, live_db):
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=1, act_rowcounts=[1])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    retention.run_retention(commit=True, only=["qa_blobs"], batch=5000)
    acts = [s for s, _ in conn.executed if s.strip().startswith("UPDATE")]
    assert acts and "SET content_blocks_json = NULL" in acts[0]
    assert not any(s.strip().startswith("DELETE") for s, _ in conn.executed), \
        "qa_blobs 是瘦身（置 NULL），绝不是删行"


def test_main_exit_codes(monkeypatch, live_db):
    # blocked → 2
    monkeypatch.setenv("RAG_RETENTION_ENABLE", "true")
    conn = _ScriptedConn(affected=100, rollup_state=(0, None))
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    assert retention.main(["--commit", "--only", "qa_rows"]) == 2
    # ok → 0
    conn2 = _ScriptedConn(affected=0)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn2)
    assert retention.main(["--only", "audit"]) == 0
    # error → 3
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    assert retention.main(["--only", "audit"]) == 3

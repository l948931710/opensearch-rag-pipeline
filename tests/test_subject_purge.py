# -*- coding: utf-8 -*-
"""retention.purge_subject（P2-5 数据主体擦除，PIPL 15/47）单测。

不变量：simulate skip；空 user_id 拒绝；dry-run 只数不删；commit 双门
（RAG_SUBJECT_PURGE_ENABLE + env_guard）；删除顺序 qa_retrieved_doc 先于 qa_session_log
（事实表无 user_id、经 message_id 关联，反序即孤儿）；可选表 1146 → skip 不算失败。
"""
import pytest

from opensearch_pipeline import retention
from opensearch_pipeline.config import get_config


class _Cur:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self._conn.raise_1146_on and self._conn.raise_1146_on in sql:
            raise Exception(1146, "Table doesn't exist")
        self._conn.executed.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("SELECT COUNT(*)"):
            self._row = (self._conn.affected,)
        elif s.startswith("DELETE"):
            self.rowcount = (self._conn.act_rowcounts.pop(0)
                             if self._conn.act_rowcounts else 0)
            self._conn.acts += 1

    def fetchone(self):
        return getattr(self, "_row", None)


class _Conn:
    def __init__(self, *, affected=0, act_rowcounts=None, raise_1146_on=None):
        self.affected = affected
        self.act_rowcounts = list(act_rowcounts or [])
        self.raise_1146_on = raise_1146_on
        self.executed = []
        self.acts = 0
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def live_db(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    return cfg


def test_simulate_mode_skips():
    rep = retention.purge_subject("u1")
    assert rep.get("skipped") == "simulate" and rep["ok"]


def test_empty_user_id_rejected():
    with pytest.raises(ValueError, match="user_id"):
        retention.purge_subject("   ")


def test_dry_run_counts_without_deleting(monkeypatch, live_db):
    conn = _Conn(affected=42)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1")
    assert rep["ok"] and rep["dry_run"]
    assert set(rep["tables"]) == {"qa_retrieved_doc", "user_feedback", "escalation_ticket",
                                  "qa_conversation", "qa_session_log"}
    assert all(t["affected"] == 42 and t["dry_run"] for t in rep["tables"].values())
    assert conn.acts == 0 and conn.commits == 0, "dry-run 绝不 DELETE、绝不 commit"


def test_commit_requires_enable_flag(monkeypatch, live_db):
    monkeypatch.delenv("RAG_SUBJECT_PURGE_ENABLE", raising=False)
    with pytest.raises(RuntimeError, match="RAG_SUBJECT_PURGE_ENABLE"):
        retention.purge_subject("u1", commit=True)


def test_commit_deletes_fact_rows_before_session_log(monkeypatch, live_db):
    monkeypatch.setenv("RAG_SUBJECT_PURGE_ENABLE", "true")
    conn = _Conn(affected=3, act_rowcounts=[3, 0, 0, 0, 3])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1", commit=True, batch=100)
    assert rep["ok"]
    assert rep["tables"]["qa_retrieved_doc"]["deleted"] == 3
    assert rep["tables"]["qa_session_log"]["deleted"] == 3
    deletes = [s for s, _ in conn.executed if s.strip().startswith("DELETE")]
    assert len(deletes) == 5
    # ⚠️ 顺序不可倒：事实表（经 message_id 关联）必须先于 qa_session_log 本体
    idx_fact = next(i for i, s in enumerate(deletes) if "qa_retrieved_doc" in s)
    idx_log = next(i for i, s in enumerate(deletes)
                   if "qa_session_log" in s and "qa_retrieved_doc" not in s)
    assert idx_fact < idx_log
    # 事实表删除经 qa_session_log.message_id 关联（表本身无 user_id 列）
    assert "message_id IN" in deletes[idx_fact]


def test_optional_table_1146_is_skipped_not_failed(monkeypatch, live_db):
    monkeypatch.setenv("RAG_SUBJECT_PURGE_ENABLE", "true")
    conn = _Conn(affected=1, act_rowcounts=[1, 1, 1, 1],
                 raise_1146_on="qa_retrieved_doc")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1", commit=True, batch=100)
    assert rep["ok"], "可选迁移（schema/013）未 apply 不算失败"
    assert "不存在" in rep["tables"]["qa_retrieved_doc"]["skipped"]
    assert rep["tables"]["qa_session_log"]["ok"]


def test_non_optional_table_error_fails_report(monkeypatch, live_db):
    monkeypatch.setenv("RAG_SUBJECT_PURGE_ENABLE", "true")
    conn = _Conn(affected=1, act_rowcounts=[1, 1, 1, 1],
                 raise_1146_on="user_feedback")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    rep = retention.purge_subject("u1", commit=True, batch=100)
    assert not rep["ok"], "基础表缺失=部署错库，必须按事故上报"
    assert rep["tables"]["user_feedback"].get("error")


def test_cli_purge_user_dry_run(monkeypatch, live_db, capsys):
    conn = _Conn(affected=7)
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    assert retention.main(["--purge-user", "u1"]) == 0
    assert conn.acts == 0
    assert "dry-run" in capsys.readouterr().out


def test_cli_purge_user_commit_without_enable_exits_3(monkeypatch, live_db):
    monkeypatch.delenv("RAG_SUBJECT_PURGE_ENABLE", raising=False)
    assert retention.main(["--purge-user", "u1", "--commit"]) == 3

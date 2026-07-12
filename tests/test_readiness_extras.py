# -*- coding: utf-8 -*-
"""P0-6 readiness 扩展探针（readiness.py）单元守护。

四项：agent/ontology 表族存在（flag off→skipped）· 工具注册表可构建 ·
schema_migrations checksum 漂移 · DashScope live 探针（默认 config-only）。
全部 TTL 缓存 + 异常报状态词不抛出。
"""
import hashlib
from types import SimpleNamespace

import pytest

from opensearch_pipeline import readiness


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    readiness._reset_cache()
    monkeypatch.delenv("RAG_AGENT_ENABLE", raising=False)
    monkeypatch.delenv("RAG_ONTOLOGY_ENABLE", raising=False)
    monkeypatch.delenv("RAG_READY_DASHSCOPE_LIVE", raising=False)
    yield
    readiness._reset_cache()


def test_flags_off_all_skipped():
    assert readiness.agent_tables_status() == "skipped"
    assert readiness.ontology_tables_status() == "skipped"
    assert readiness.tool_registry_status() == "skipped"


class _Cur:
    def __init__(self, rows_by_sql):
        self._rows_by_sql = rows_by_sql
        self._rows = []

    def execute(self, sql, params=None):
        for key, rows in self._rows_by_sql.items():
            if key in sql:
                if isinstance(rows, Exception):
                    raise rows
                self._rows = rows
                return
        self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows_by_sql):
        self._rows_by_sql = rows_by_sql

    def cursor(self):
        return _Cur(self._rows_by_sql)

    def close(self):
        pass


def _wire_db(monkeypatch, rows_by_sql):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _Conn(rows_by_sql))


def test_agent_tables_ok_and_missing(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    _wire_db(monkeypatch, {"information_schema.tables": [(len(readiness._AGENT_TABLES),)]})
    assert readiness.agent_tables_status() == "ok"
    readiness._reset_cache()
    _wire_db(monkeypatch, {"information_schema.tables": [(len(readiness._AGENT_TABLES) - 1,)]})
    assert readiness.agent_tables_status() == "missing"
    readiness._reset_cache()
    _wire_db(monkeypatch, {"information_schema.tables": RuntimeError("db down")})
    assert readiness.agent_tables_status() == "error"


def test_tool_registry_builds(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    st = readiness.tool_registry_status()
    assert st.startswith("ok(")                        # 代码内声明注册可构建且非空


def test_schema_drift_detection(monkeypatch, tmp_path):
    (tmp_path / "001_demo.sql").write_bytes(b"CREATE TABLE demo(id INT);")
    good = hashlib.sha256(b"CREATE TABLE demo(id INT);").hexdigest()
    monkeypatch.setattr(readiness, "_schema_dir", lambda: tmp_path)
    monkeypatch.setattr("opensearch_pipeline.config.get_config",
                        lambda: SimpleNamespace(rds=SimpleNamespace(
                            database="k", operation_database="o", ontology_database="t")))
    # 台账 checksum 与本地一致 → ok
    _wire_db(monkeypatch, {"schema_migrations": [("001_demo.sql", good)]})
    assert readiness._schema_drift_once() == "ok"
    # 台账 checksum 不同 → drift（同名内容漂移）
    _wire_db(monkeypatch, {"schema_migrations": [("001_demo.sql", "deadbeef" * 8)]})
    assert readiness._schema_drift_once() == "drift"
    # 台账没这个文件 / 本地新增未 apply → 不算漂移
    _wire_db(monkeypatch, {"schema_migrations": [("999_other.sql", "deadbeef" * 8)]})
    assert readiness._schema_drift_once() == "ok"
    # 修订标记 filename@NNNa：取 @ 前基名比对
    _wire_db(monkeypatch, {"schema_migrations": [("001_demo.sql@001a", good)]})
    assert readiness._schema_drift_once() == "ok"
    # 三库台账都查不到（未迁移环境）→ unavailable
    _wire_db(monkeypatch, {"schema_migrations": RuntimeError("no table")})
    assert readiness._schema_drift_once() == "unavailable"


def test_schema_no_local_files(monkeypatch):
    monkeypatch.setattr(readiness, "_schema_dir", lambda: None)
    assert readiness._schema_drift_once() == "no_local_files"


def test_dashscope_config_only_default():
    assert readiness.dashscope_status(None, "m") == "unconfigured"
    assert readiness.dashscope_status("sk-x", "m") == "configured"   # 默认零外呼


def test_dashscope_live_probe(monkeypatch):
    monkeypatch.setenv("RAG_READY_DASHSCOPE_LIVE", "true")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "qwen3.7-plus"}, {"id": "text-embedding-v4"}]}

    class _Sess:
        @staticmethod
        def get(url, **kw):
            return _Resp()

    monkeypatch.setattr("opensearch_pipeline.http_session.get_session", lambda: _Sess())
    assert readiness.dashscope_status("sk-x", "qwen3.7-plus") == "ok"
    readiness._reset_cache()
    assert readiness.dashscope_status("sk-x", "nonexistent-model") == "model_missing"
    readiness._reset_cache()

    class _Boom:
        @staticmethod
        def get(url, **kw):
            raise RuntimeError("network")

    monkeypatch.setattr("opensearch_pipeline.http_session.get_session", lambda: _Boom())
    assert readiness.dashscope_status("sk-x", "m") == "error"


def test_ttl_cache_prevents_probe_amplification(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_ENABLE", "true")
    calls = {"n": 0}

    class _CountConn(_Conn):
        def cursor(self):
            calls["n"] += 1
            return _Cur(self._rows_by_sql)

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn",
                        lambda *a, **k: _CountConn(
                            {"information_schema.tables": [(len(readiness._AGENT_TABLES),)]}))
    assert readiness.agent_tables_status() == "ok"
    assert readiness.agent_tables_status() == "ok"
    assert calls["n"] == 1                            # 第二次命中 TTL 缓存，不再触库

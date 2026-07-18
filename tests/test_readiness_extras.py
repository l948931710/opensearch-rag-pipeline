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
    # B2 flag-conditional 契约探针的 flag（隔离外部 env 泄漏）
    monkeypatch.delenv("RAG_AGENT_DURABLE_DISPATCH", raising=False)
    monkeypatch.delenv("RAG_INGEST_LEASE_ENABLE", raising=False)
    monkeypatch.delenv("RAG_FOLLOWUP_REWRITE", raising=False)
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
    # 批次5 P0-06c：本地存在但台账未记 → unapplied:N（旧语义「不算漂移→ok」正是
    # 「缺 apply 仍绿」的假健康主通道，strict 下应摘流量）
    _wire_db(monkeypatch, {"schema_migrations": [("999_other.sql", "deadbeef" * 8)]})
    assert readiness._schema_drift_once() == "unapplied:1"
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


# ═══════════ B2（生产级外审 2026-07-17 RB-04/RB-06b）：flag-conditional 契约 + 姿态自报 ═══════════


def test_b2_flag_off_contracts_skipped():
    assert readiness.durable_dispatch_contract_status() == "skipped"
    assert readiness.ingest_lease_contract_status() == "skipped"
    assert readiness.followup_rewrite_contract_status() == "skipped"
    assert readiness.write_tool_contract_status() == "skipped"   # 写工具默认关


def test_b2_durable_dispatch_contract(monkeypatch):
    monkeypatch.setenv("RAG_AGENT_DURABLE_DISPATCH", "true")
    _wire_db(monkeypatch, {"COLUMN_TYPE": [("enum('submit','resume')",)],
                           "information_schema.tables": [(1,)]})
    assert readiness.durable_dispatch_contract_status() == "ok"
    readiness._reset_cache()
    # 043 表在、044 ENUM 未扩 resume（MODIFY 未 apply 的蓝绿窗口）
    _wire_db(monkeypatch, {"COLUMN_TYPE": [("enum('submit')",)],
                           "information_schema.tables": [(1,)]})
    st = readiness.durable_dispatch_contract_status()
    assert st.startswith("missing:") and "044" in st
    readiness._reset_cache()
    # 043 表整体缺失
    _wire_db(monkeypatch, {"information_schema.tables": [(0,)]})
    assert readiness.durable_dispatch_contract_status() == \
        "missing:agent_dispatch_command(schema/043)"


def test_b2_ingest_lease_contract(monkeypatch):
    monkeypatch.setenv("RAG_INGEST_LEASE_ENABLE", "true")
    _wire_db(monkeypatch, {"information_schema.columns": [(1,)],
                           "information_schema.statistics": [(1,)]})
    assert readiness.ingest_lease_contract_status() == "ok"
    readiness._reset_cache()
    _wire_db(monkeypatch, {"information_schema.columns": [(0,)]})
    st = readiness.ingest_lease_contract_status()
    assert st.startswith("missing:") and "048" in st
    readiness._reset_cache()
    # 列在、索引缺（048 的 ADD KEY 半途）
    _wire_db(monkeypatch, {"information_schema.columns": [(1,)],
                           "information_schema.statistics": [(0,)]})
    assert "idx_lease_expiry" in readiness.ingest_lease_contract_status()


def test_b2_write_tool_contract(monkeypatch):
    monkeypatch.setattr("opensearch_pipeline.agent_tools.ontology_write_tools_enabled",
                        lambda: True)
    _wire_db(monkeypatch, {"information_schema.tables": [(1,)],
                           "information_schema.columns": [(1,)]})
    assert readiness.write_tool_contract_status() == "ok"
    readiness._reset_cache()
    _wire_db(monkeypatch, {"information_schema.tables": [(0,)]})
    assert readiness.write_tool_contract_status() == \
        "missing:agent_tool_operation(schema/045)"


def test_b2_followup_and_acl_generation_report_only(monkeypatch):
    monkeypatch.setenv("RAG_FOLLOWUP_REWRITE", "true")
    _wire_db(monkeypatch, {"information_schema.columns": [(0,)]})
    assert "050" in readiness.followup_rewrite_contract_status()
    assert "049" in readiness.acl_outbox_generation_status()


def test_b2_security_posture_report(monkeypatch):
    monkeypatch.setenv("RAG_REQUIRE_AUTH", "true")
    monkeypatch.delenv("RAG_ACL_FAIL_CLOSED", raising=False)
    monkeypatch.setenv("RAG_ALLOW_LEGACY_OPEN_PROD", "ack:2026-07-18")
    monkeypatch.delenv("DINGTALK_CARD_CALLBACK_API_SECRET", raising=False)
    rep = readiness.security_posture_report()
    assert rep["require_auth"] == "on"
    assert rep["acl_fail_closed"] == "off"
    assert str(rep["legacy_open_ack"]).startswith("legacy_open(")
    assert rep["card_callback_secret"] == "missing"
    assert "rds_tls" in rep and "schema_strict" in rep
    d1 = rep["config_digest"]
    assert isinstance(d1, str) and len(d1) == 16
    assert all(c in "0123456789abcdef" for c in d1)
    # 值不外泄但可比对：任一 RAG_ 值变化 ⇒ digest 变化
    monkeypatch.setenv("RAG_REQUIRE_AUTH", "false")
    rep2 = readiness.security_posture_report()
    assert rep2["config_digest"] != d1 and rep2["require_auth"] == "off"
    # 明文值绝不出现在报告里
    monkeypatch.setenv("RAG_DASHSCOPE_API_KEY", "sk-super-secret-value")
    rep3 = readiness.security_posture_report()
    assert "sk-super-secret-value" not in str(rep3)

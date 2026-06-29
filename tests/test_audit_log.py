# -*- coding: utf-8 -*-
"""tests/test_audit_log.py — Phase-1 L5: append-only kb_audit_log writer + deactivation wiring.

Revives the previously-dead kb_audit_log (zero writers) into an append-only lineage trail.
Invariants: fail-open (never raises), no-op in simulate, own connection (doesn't poison the
caller's txn), and wired at the irreversible DEACTIVATE transition.
"""
import inspect


def test_audit_trace_id_from_run_provenance():
    from opensearch_pipeline.audit_log import audit_trace_id
    assert audit_trace_id({"run_provenance": {"git_commit": "abc123", "bizdate": "20260616"}}) == "abc123:20260616"
    assert audit_trace_id({"run_provenance": {"git_commit": "abc123"}}) == "abc123"
    assert audit_trace_id({"bizdate": "20260616"}) == "20260616"
    assert audit_trace_id({}) is None
    assert audit_trace_id(None) is None


def test_write_audit_noop_in_simulate(monkeypatch):
    """simulate=True must not touch the DB at all."""
    import opensearch_pipeline.pipeline_nodes as pn

    def _boom(**kw):
        raise AssertionError("_get_db_conn must NOT be called in simulate mode")

    monkeypatch.setattr(pn, "_get_db_conn", _boom)
    # must not raise
    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id="d", version_no=1, action_type="DEACTIVATE", simulate=True)


def test_write_audit_fail_open_on_db_error(monkeypatch, caplog):
    """A DB failure must be swallowed (audit is auxiliary; never break ingest)."""
    import logging
    import opensearch_pipeline.pipeline_nodes as pn

    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    from opensearch_pipeline.audit_log import write_audit
    with caplog.at_level(logging.WARNING, logger="opensearch_pipeline.audit_log"):
        write_audit(doc_id="d", version_no=1, action_type="INDEX", action_result="SUCCESS")  # must not raise
    assert any("kb_audit_log write failed" in r.getMessage() for r in caplog.records)


def test_write_audit_inserts_expected_row(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn

    captured = {}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            captured["committed"] = True

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _Conn())
    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id="DOC_X", version_no=3, action_type="DEACTIVATE", action_result="SUCCESS",
                trace_id="abc:20260616", message="retired", simulate=False)

    assert "INSERT INTO fuling_knowledge.kb_audit_log" in captured["sql"]
    p = captured["params"]
    assert p[0] == "abc:20260616" and p[1] == "DOC_X" and p[2] == 3
    assert p[3] == "DEACTIVATE" and p[4] == "SUCCESS"
    assert captured.get("committed") and captured.get("closed")


def test_write_audit_cursor_path_same_txn_no_new_conn(monkeypatch):
    """cursor 路径（serving）：用调用方游标写、【不】开新连接（同事务原子审计，B1）。"""
    import opensearch_pipeline.pipeline_nodes as pn

    def _boom(**kw):
        raise AssertionError("cursor 路径不得自开连接（应复用调用方事务）")

    monkeypatch.setattr(pn, "_get_db_conn", _boom)
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id="D1", version_no=2, action_type="RETIRE_REQUEST",
                operator_type="user", operator_id="u1", trace_id="t", cursor=_Cur())
    assert "INSERT INTO fuling_knowledge.kb_audit_log" in captured["sql"]
    assert captured["params"][1] == "D1" and captured["params"][3] == "RETIRE_REQUEST"


def test_write_audit_cursor_path_propagates_error():
    """cursor 路径【不吞】异常（区别于 ingestion fail-open）：游标抛错 → 向上传播 → 调用方事务回滚。"""
    import pytest

    class _BadCur:
        def execute(self, *a, **k):
            raise RuntimeError("boom")

    from opensearch_pipeline.audit_log import write_audit
    with pytest.raises(RuntimeError):
        write_audit(doc_id="D1", version_no=1, action_type="KB_ADMIN_REVOKE", cursor=_BadCur())


# ── ACL 策略版本（acl_policy_version 盖进 ACL 审计行）──
def test_acl_policy_version_stable_and_content_addressed():
    from opensearch_pipeline.versions import acl_policy_version
    v = acl_policy_version()
    assert v and v != "unknown" and len(v) == 12   # 真实 12-hex 内容指纹（test 环境映射可 import）
    assert acl_policy_version() == v                # 同进程稳定


def test_acl_policy_version_changes_when_mapping_changes(monkeypatch):
    import opensearch_pipeline.dingtalk_identity as di
    from opensearch_pipeline.versions import acl_policy_version
    before = acl_policy_version()
    patched = dict(di._DEPT_NAME_TO_GROUPS)
    patched["新部门X"] = ["finance"]
    monkeypatch.setattr(di, "_DEPT_NAME_TO_GROUPS", patched)
    assert acl_policy_version() != before           # 映射改动 → 版本自动变（无需手动 bump）


def _capture_conn(monkeypatch, captured):
    import opensearch_pipeline.pipeline_nodes as pn

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): captured["params"] = params

    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _Conn())


def test_write_audit_stamps_acl_policy_on_acl_action(monkeypatch):
    """ACL 授权动作（KB_ADMIN_GRANT 等）→ 消息盖上 [acl_policy=<ver>]，原文保留。"""
    captured = {}
    _capture_conn(monkeypatch, captured)
    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id=None, version_no=None, action_type="KB_ADMIN_GRANT",
                operator_id="admin1", trace_id="t", message="granted kb_admin to u9")
    msg = captured["params"][8]
    assert msg.startswith("[acl_policy=") and "granted kb_admin to u9" in msg


def test_write_audit_no_acl_stamp_on_lifecycle_action(monkeypatch):
    """文档生命周期动作（DEACTIVATE 等）不盖 ACL 策略版本（避免噪声）。"""
    captured = {}
    _capture_conn(monkeypatch, captured)
    from opensearch_pipeline.audit_log import write_audit
    write_audit(doc_id="D", version_no=1, action_type="DEACTIVATE", message="retired")
    assert captured["params"][8] == "retired"


def test_deactivate_wires_audit_write():
    """node_deactivate_old_chunks must emit a DEACTIVATE audit on the irreversible retirement."""
    from opensearch_pipeline.pipeline_nodes import node_deactivate_old_chunks
    src = inspect.getsource(node_deactivate_old_chunks)
    assert "from opensearch_pipeline.audit_log import write_audit" in src
    assert 'action_type="DEACTIVATE"' in src
    assert "simulate=simulate_db" in src, "audit must no-op in simulate (pass simulate=simulate_db)"


def test_register_wires_audit_write():
    """node_register_metadata must emit a REGISTER audit (doc/version lifecycle start)."""
    from opensearch_pipeline.pipeline_nodes import node_register_metadata
    src = inspect.getsource(node_register_metadata)
    assert 'action_type="REGISTER"' in src and "write_audit(" in src
    assert "simulate=simulate_db" in src


def test_chunk_status_closure_wires_audit_write():
    """node_write_chunk_meta status closure must emit a CHUNK audit (DONE/EMPTY) per (doc,version)."""
    from opensearch_pipeline.pipeline_nodes import node_write_chunk_meta
    src = inspect.getsource(node_write_chunk_meta)
    assert 'action_type="CHUNK"' in src and "write_audit(" in src
    assert "simulate=simulate_db" in src


def test_index_status_wires_audit_write():
    """node_update_index_status must emit an INDEX audit (SUCCESS/FAILED) per (doc,version)."""
    from opensearch_pipeline.pipeline_nodes import node_update_index_status
    src = inspect.getsource(node_update_index_status)
    assert 'action_type="INDEX"' in src and "write_audit(" in src
    assert '"FAILED" if (_d, _v) in failed_doc_versions else "SUCCESS"' in src

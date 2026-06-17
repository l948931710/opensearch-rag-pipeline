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

    assert "INSERT INTO kb_audit_log" in captured["sql"]
    p = captured["params"]
    assert p[0] == "abc:20260616" and p[1] == "DOC_X" and p[2] == 3
    assert p[3] == "DEACTIVATE" and p[4] == "SUCCESS"
    assert captured.get("committed") and captured.get("closed")


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

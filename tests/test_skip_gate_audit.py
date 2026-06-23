# -*- coding: utf-8 -*-
"""test_skip_gate_audit.py — fail-open audit of SKIPPED_DUPLICATE re-ingests (_audit_reingest_skip)."""
import opensearch_pipeline.audit_log as al
import opensearch_pipeline.pipeline_nodes as pn


def test_audit_helper_calls_write_audit(monkeypatch):
    captured = {}
    monkeypatch.setattr(al, "write_audit", lambda **kw: captured.update(kw))
    monkeypatch.setattr(al, "audit_trace_id", lambda ctx: "trace-xyz")
    pn._audit_reingest_skip({"trace": 1}, "D", 2, "intra-doc: unchanged vs v1", simulate_db=False)
    assert captured["doc_id"] == "D"
    assert captured["version_no"] == 2
    assert captured["action_type"] == "REINGEST"
    assert captured["action_result"] == "SKIPPED_DUPLICATE"
    assert captured["trace_id"] == "trace-xyz"
    assert "unchanged vs v1" in captured["message"]
    assert captured["simulate"] is False


def test_audit_helper_is_fail_open(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("audit table down")
    monkeypatch.setattr(al, "write_audit", _boom)
    monkeypatch.setattr(al, "audit_trace_id", lambda ctx: None)
    # must NOT raise — audit failure can never affect the skip
    pn._audit_reingest_skip({}, "D", 2, "msg", simulate_db=False)

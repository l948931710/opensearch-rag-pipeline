# -*- coding: utf-8 -*-
"""tests/test_pipeline_run.py — Phase-1 L6prov: per-run provenance header writer.

Invariants: fail-open (never raises), no-op in simulate, own connection, RUNNING→terminal,
and wired into the orchestrator main() around run_stage_drained.
"""
import inspect


def test_make_run_id_format():
    from opensearch_pipeline.pipeline_run import make_run_id
    rid = make_run_id({"stage": 2, "bizdate": "20260616", "git_commit": "abc123"})
    assert rid.startswith("s2_20260616_abc123_") and len(rid.split("_")[-1]) == 6


def test_run_start_noop_in_simulate(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn

    def _boom(**kw):
        raise AssertionError("_get_db_conn must NOT be called in simulate mode")

    monkeypatch.setattr(pn, "_get_db_conn", _boom)
    from opensearch_pipeline.pipeline_run import run_start
    assert run_start({"stage": 1, "bizdate": "x"}, simulate=True) is None


def test_run_start_fail_open(monkeypatch, caplog):
    import logging
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    from opensearch_pipeline.pipeline_run import run_start
    with caplog.at_level(logging.WARNING, logger="opensearch_pipeline.pipeline_run"):
        rid = run_start({"stage": 1, "bizdate": "x", "git_commit": "c"}, simulate=False)  # must not raise
    assert rid is None
    assert any("pipeline_run start failed" in r.getMessage() for r in caplog.records)


class _CaptureConn:
    def __init__(self):
        self.calls = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def commit(self):
        pass

    def close(self):
        pass


def test_run_start_inserts_running_row(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn
    cap = _CaptureConn()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: cap)
    from opensearch_pipeline.pipeline_run import run_start
    prov = {"stage": 3, "bizdate": "20260616", "git_commit": "abc123",
            "extractor_version": "1.0.0", "chunker_version": "1.0.0", "detector_version": "1.0.0",
            "embedding_model": "text-embedding-v4", "embedding_model_version": "text-embedding-v4",
            "llm_model": "qwen3.6-plus"}
    rid = run_start(prov, simulate=False)
    assert rid and rid.startswith("s3_20260616_abc123_")
    sql, params = cap.calls[-1]
    assert "INSERT INTO pipeline_run" in sql and "'RUNNING'" in sql
    assert params[0] == rid and params[1] == 3 and params[3] == "abc123"


def test_run_finish_updates_terminal(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn
    cap = _CaptureConn()
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: cap)
    from opensearch_pipeline.pipeline_run import run_finish
    run_finish("s3_x_c_abcdef", "SUCCESS", docs_processed=5, chunks_written=42, simulate=False)
    sql, params = cap.calls[-1]
    assert "UPDATE pipeline_run" in sql and "finished_at=NOW()" in sql
    assert params[0] == "SUCCESS" and params[1] == 5 and params[2] == 42 and params[-1] == "s3_x_c_abcdef"


def test_run_finish_noop_without_run_id(monkeypatch):
    import opensearch_pipeline.pipeline_nodes as pn

    def _boom(**kw):
        raise AssertionError("must not touch DB when run_id is None")

    monkeypatch.setattr(pn, "_get_db_conn", _boom)
    from opensearch_pipeline.pipeline_run import run_finish
    run_finish(None, "SUCCESS", simulate=False)  # no run_id (start failed/sim) → no-op


def test_orchestrator_main_wires_pipeline_run():
    from opensearch_pipeline.dataworks_orchestrator import main
    src = inspect.getsource(main)
    assert "run_start(" in src and "run_finish(_run_id, \"SUCCESS\"" in src
    assert "run_finish(_run_id, \"FAILED\"" in src, "FAILED terminal must be recorded on exception"

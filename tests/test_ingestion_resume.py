# -*- coding: utf-8 -*-
"""test_ingestion_resume.py — read-only resume/recovery report (ingestion_resume A)."""
import opensearch_pipeline.ingestion_resume as ir


class _FakeCur:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store.append(sql)

    def fetchone(self):
        return (2,)


class _FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCur(self.store)

    def close(self):
        pass


def test_simulate_is_noop():
    rep = ir.build_resume_report(3)            # stack-test default RAG_SIMULATE=true → simulate_db
    assert rep["simulate"] is True
    assert "no-op" in rep["note"]
    assert "simulate" in ir.format_report(rep)


def _force_real(monkeypatch):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config(), "simulate_db", False)


def test_report_stage3_read_only(monkeypatch):
    _force_real(monkeypatch)
    sql_log = []
    import opensearch_pipeline.dataworks_orchestrator as orch
    import opensearch_pipeline.pipeline_nodes as pn
    import opensearch_pipeline.ha3_reconcile as rc
    import opensearch_pipeline.spot_checker as sc
    monkeypatch.setattr(orch, "_count_pending_rows", lambda stage: 5)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _FakeConn(sql_log))
    captured = {}
    monkeypatch.setattr(rc, "reconcile_ha3_orphan_pks",
                        lambda dry_run=False, **kw: captured.update(dry_run=dry_run) or {"stale": 3})

    def _no_write(*a, **k):
        raise AssertionError("report must NOT call reconcile_stranded_versions (write path)")
    monkeypatch.setattr(sc, "reconcile_stranded_versions", _no_write)

    rep = ir.build_resume_report(3)
    assert rep["pending"] == 5
    assert rep["in_flight"] == 2 and rep["stale_locks_2h"] == 2
    assert rep["ha3_orphan_pks_estimate"] == 3
    assert captured["dry_run"] is True                       # only the dry-run reconciler
    # strictly read-only: every SQL the report issued is a SELECT
    assert sql_log and all(s.strip().upper().startswith("SELECT") for s in sql_log)


def test_report_stage1_has_loading_and_note(monkeypatch):
    _force_real(monkeypatch)
    import opensearch_pipeline.dataworks_orchestrator as orch
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(orch, "_count_pending_rows", lambda stage: 0)
    monkeypatch.setattr(pn, "_get_db_conn", lambda **kw: _FakeConn([]))
    rep = ir.build_resume_report(1)
    assert rep["pending"] == 0
    assert rep["in_flight_loading"] == 2
    assert "no age guard" in rep["stale_note"]
    assert "ha3_orphan_pks_estimate" not in rep             # stage-1 has no HA3 estimate
    txt = ir.format_report(rep)
    assert "recovers_from" in txt and "current RDS state" in txt

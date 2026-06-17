# -*- coding: utf-8 -*-
"""tests/test_cs5_outbox.py — CS5: PENDING_DELETE outbox for 2-phase-safe HA3 deletes.

The outbox is the document_version.index_status='PENDING_DELETE' sentinel (no new table) + the existing
reconcile_pending_deletes() drainer. CS5 closes three gaps so the main ingestion deactivation path is
covered, not just the un-scheduled spot-check:
  1. reconcile_pending_deletes also deactivates chunk_meta (self-consistent → no CS3 orphan)
  2. node_deactivate_old_chunks feeds the outbox on HA3-delete failure (additive, before the raise)
  3. the stage-3 drain runs reconcile_pending_deletes every ingestion run
These are DB/HA3-failure-injection paths; verified here at the wiring level (the project's established
inspect.getsource pattern) + the full suite for regression.
"""
import inspect


def test_reconcile_pending_deletes_deactivates_chunk_meta():
    """Edit 1: on successful retry, chunk_meta is set is_active=0 (not just document_version DELETED)."""
    from opensearch_pipeline import spot_checker
    src = inspect.getsource(spot_checker.reconcile_pending_deletes)
    assert "UPDATE\n                        chunk_meta" in src or "UPDATE chunk_meta" in src
    assert "is_active = FALSE" in src
    # still marks the document_version row DELETED
    assert "index_status = 'DELETED'" in src


def test_node_deactivate_feeds_outbox_on_failure():
    """Edit 2: the HA3-delete-failure path queues OLD versions as PENDING_DELETE (additive)."""
    from opensearch_pipeline import pipeline_nodes
    src = inspect.getsource(pipeline_nodes.node_deactivate_old_chunks)
    assert "PENDING_DELETE" in src
    assert "version_no < %s" in src
    # the original fail-safe raise is preserved (never-disappear unchanged)
    assert "Failed to deactivate old chunks in search engine" in src


def test_stage3_drain_runs_pending_delete_reconcile():
    """Edit 3: the stage-3 drain drains the outbox alongside stranded-version reconcile."""
    from opensearch_pipeline import dataworks_orchestrator
    src = inspect.getsource(dataworks_orchestrator.run_stage_drained)
    assert "reconcile_pending_deletes" in src
    assert "reconcile_stranded_versions" in src


def test_reconcile_pending_deletes_returns_shape_on_no_rows(monkeypatch):
    """Sanity: with no PENDING_DELETE rows the drainer returns the {total,success,failed,errors} shape
    and never raises (fail-open). Uses a fake conn so no real DB is needed."""
    from opensearch_pipeline import spot_checker

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass

    monkeypatch.setattr(spot_checker, "_get_db_conn", lambda **k: _Conn())
    out = spot_checker.reconcile_pending_deletes()
    assert out == {"total": 0, "success": 0, "failed": 0, "errors": []}

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
    # still marks the document_version row DELETED (rendered from the DocVersionIndexStatus vocab)
    assert "index_status = '{DocVersionIndexStatus.DELETED}'" in src


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


def test_stage3_drain_runs_orphan_pk_reconcile_dry_run_default():
    """#2：stage-3 drain 挂上 HA3 orphan-PK 对账做安全网（同版本 re-chunk strand 的旧 PK）。

    默认 dry-run 只报数——不可逆 HA3 删除必须显式 RAG_STAGE3_ORPHAN_PURGE=true 才执行。"""
    from opensearch_pipeline import dataworks_orchestrator
    src = inspect.getsource(dataworks_orchestrator.run_stage_drained)
    assert "reconcile_ha3_orphan_pks" in src
    assert "RAG_STAGE3_ORPHAN_PURGE" in src
    assert "dry_run=not _orphan_purge" in src  # 默认 dry-run，未设 flag 绝不真删


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
    assert out == {"total": 0, "success": 0, "failed": 0, "skipped_stale": 0, "errors": []}


def test_deactivate_failure_path_cas_guards_pending_delete():
    """ultra P1（2026-07-17）：HA3 删除失败路径写 document_version='FAILED' 必须 CAS on PROCESSING
    + 清租约（对齐成功路径 6208 / node_update_index_status 7340）。此前无谓词无条件写 FAILED，
    会覆盖控制台中途置的 PENDING_DELETE 握手 → 受限文档以旧 permission 被 stage-3 重推 HA3。"""
    from opensearch_pipeline import pipeline_nodes
    src = inspect.getsource(pipeline_nodes.node_deactivate_old_chunks)
    # 失败路径 FAILED 写带清租约（成功路径是参数化 `%s`，故这段是失败路径独有印记）
    marker = "SET index_status = '{DocVersionIndexStatus.FAILED}'{ingest_lease.clear_set_sql()}"
    assert marker in src, "失败路径 FAILED 写应带 clear_set_sql（清租约）"
    # 紧随其后是 PROCESSING CAS 谓词
    tail = src[src.index(marker): src.index(marker) + 400]
    assert "AND index_status = '{DocVersionIndexStatus.PROCESSING}'" in tail, \
        "失败路径 FAILED 写必须 CAS on PROCESSING，保住 PENDING_DELETE 握手"

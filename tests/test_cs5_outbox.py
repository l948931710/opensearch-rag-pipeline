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



def _drain_source(orch_mod) -> str:
    """B1a：drain 主体已拆进 _run_stage_drained_locked（让 A12 的 try/finally 覆盖全程），
    源码断言必须看两段拼起来的整体。"""
    import inspect as _i
    return (_i.getsource(orch_mod.run_stage_drained)
            + _i.getsource(orch_mod._run_stage_drained_locked))

def test_stage3_drain_runs_pending_delete_reconcile():
    """Edit 3: the stage-3 drain drains the outbox alongside stranded-version reconcile."""
    from opensearch_pipeline import dataworks_orchestrator
    src = _drain_source(dataworks_orchestrator)
    assert "reconcile_pending_deletes" in src
    assert "reconcile_stranded_versions" in src


def test_stage3_drain_orphan_pk_reconcile_is_gated_by_purge_flag():
    """#2：stage-3 drain 挂上 HA3 orphan-PK 对账做安全网（同版本 re-chunk strand 的旧 PK）。

    A6（2026-07-25）改为**未开 flag 即整块跳过**：dry_run 分支在枚举+分类全部完成后才 return，
    所以空跑 dry-run 不省扫描、只白付一次全 id 空间枚举。真删仍需显式 flag（语义不变）。"""
    from opensearch_pipeline import dataworks_orchestrator
    src = _drain_source(dataworks_orchestrator)
    assert "reconcile_ha3_orphan_pks" in src
    assert "RAG_STAGE3_ORPHAN_PURGE" in src
    assert "if _orphan_purge:" in src          # 未开 flag → 零调用（不再空扫）
    assert "dry_run=False" in src              # 开了 flag 才进来，此时必然真删
    assert "dry_run=not _orphan_purge" not in src


def _run_stage3_drain_head(monkeypatch, purge_env):
    """只跑 stage-3 drain 的前置对账块（pending 计数直接给 0 立刻收尾），记录 orphan 调用次数。"""
    from opensearch_pipeline import dataworks_orchestrator as orch
    calls = []
    import opensearch_pipeline.ha3_reconcile as hr
    monkeypatch.setattr(hr, "reconcile_ha3_orphan_pks",
                        lambda **kw: calls.append(kw) or {"stale": 0})
    monkeypatch.setattr(orch, "_count_pending_rows", lambda stage: 0)
    monkeypatch.setattr(orch, "_probe_unclaimable_rows", lambda stage: [])
    for _name in ("reconcile_stranded_versions", "reconcile_pending_deletes"):
        monkeypatch.setattr(f"opensearch_pipeline.spot_checker.{_name}",
                            lambda *a, **kw: {"total": 0, "success": 0, "failed": 0})
    if purge_env is None:
        monkeypatch.delenv("RAG_STAGE3_ORPHAN_PURGE", raising=False)
    else:
        monkeypatch.setenv("RAG_STAGE3_ORPHAN_PURGE", purge_env)
    orch.run_stage_drained(stage=3, bizdate="20260725", simulate=False)
    return calls


def test_orphan_reconcile_not_called_without_purge_flag(monkeypatch):
    assert _run_stage3_drain_head(monkeypatch, None) == [], "未开 flag 时不得发生任何全 id 空间枚举"


def test_orphan_reconcile_called_with_real_delete_when_flag_on(monkeypatch):
    calls = _run_stage3_drain_head(monkeypatch, "true")
    assert calls == [{"dry_run": False}]


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
    # 失败路径 FAILED 写印记（成功路径是参数化 `%s`，故这段是失败路径独有印记；
    # 分支版此处还拼 ingest_lease.clear_set_sql()——main 无租约模块，摘取适配仅保 CAS）
    marker = "SET index_status = '{DocVersionIndexStatus.FAILED}'"
    assert marker in src, "失败路径应写 FAILED"
    # 紧随其后是 PROCESSING CAS 谓词
    tail = src[src.index(marker): src.index(marker) + 400]
    assert "AND index_status = '{DocVersionIndexStatus.PROCESSING}'" in tail, \
        "失败路径 FAILED 写必须 CAS on PROCESSING，保住 PENDING_DELETE 握手"

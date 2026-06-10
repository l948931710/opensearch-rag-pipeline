# -*- coding: utf-8 -*-
"""
tests/test_production_hardening.py — Production Hardening pass regression tests
"""

import pytest
from unittest.mock import MagicMock, patch
from opensearch_pipeline.chunker import Chunk
from opensearch_pipeline.pipeline_nodes import (
    node_write_chunk_meta,
    node_acquire_index_lock,
    node_update_index_status,
    node_deactivate_old_chunks,
)

# Test A: Stage 3 Production Loader Regression Test
def test_a_production_loader_chunk_instantiation():
    """
    Test A: Stage 3 Production Loader Regression Test.
    Verifies that Chunk instantiates successfully with source_oss_key and rag_ready_key,
    raising no TypeErrors and preserving extra values.
    """
    rag_ready_key = "oss://bucket/rag-ready/doc1_v1.json"
    chunk = Chunk(
        chunk_id="doc1_v1_c0001",
        doc_id="doc1",
        version_no=1,
        chunk_index=1,
        chunk_type="text_chunk",
        chunk_text="Test chunk text",
        token_count=10,
        source_oss_key=rag_ready_key,
        extra={"rag_ready_key": rag_ready_key}
    )
    
    assert chunk.source_oss_key == rag_ready_key
    assert chunk.extra.get("rag_ready_key") == rag_ready_key


# Test B: Stage 2 Status Closure Success Test
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_b_stage2_status_closure_success(mock_get_db_conn):
    """
    Test B: Stage 2 Status Closure Success Test.
    Verifies that node_write_chunk_meta performs status closure successfully,
    correctly writing 'DONE' status and the proper chunk_count to the RDS document_version table.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn

    chunk = Chunk(
        chunk_id="doc_test_v1_c0001",
        doc_id="doc_test",
        version_no=1,
        chunk_index=1,
        chunk_type="text_chunk",
        chunk_text="Some text",
        token_count=5
    )
    ctx = {
        "valid_chunks": [chunk],
        "canonicals": [{"doc_id": "doc_test", "version_no": 1}],
        "simulate_db": False
    }

    node_write_chunk_meta(ctx)

    called = False
    for call in mock_cursor.execute.call_args_list:
        sql = call[0][0]
        params = call[0][1]
        if "UPDATE document_version" in sql and "content_process_status = 'DONE'" in sql:
            called = True
            assert params[0] == 1  # chunk_count
            assert params[1] == "doc_test"
            assert params[2] == 1  # version_no
    assert called, "Should have executed status closure update with 'DONE' and chunk_count"


# Test C: Empty Chunk Fix Test
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_c_empty_chunk_fix(mock_get_db_conn):
    """
    Test C: Empty Chunk Fix Test.
    Verifies that if valid_chunks is empty, node_write_chunk_meta updates
    document_version status to 'EMPTY' and 'DONE', and does NOT raise a RuntimeError.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn

    ctx = {
        "valid_chunks": [],
        "canonicals": [{"doc_id": "doc_empty", "version_no": 2}],
        "simulate_db": False
    }

    # Should not raise
    node_write_chunk_meta(ctx)

    called = False
    for call in mock_cursor.execute.call_args_list:
        sql = call[0][0]
        params = call[0][1]
        if "UPDATE document_version" in sql and "chunk_status = 'EMPTY'" in sql:
            called = True
            assert "content_process_status = 'DONE'" in sql
            assert params[0] == "doc_empty"
            assert params[1] == 2
    assert called, "Should have executed status closure update with 'EMPTY' and 'DONE'"


# Test D: Partial OpenSearch Failure Test
def test_d_partial_opensearch_failure():
    """
    Test D: Partial OpenSearch Failure Test.
    Verifies that if any batch experiences failures, node_update_index_status raises RuntimeError
    to prevent subsequent deactivation from executing.
    """
    ctx = {
        "bulk_batches": [{
            "chunks": [],
            "payload": "",
            "payload_size": 0,
            "job_id": "job_1",
            "oss_key": "",
            "result": {"failed": 1, "indexed": 1, "took_ms": 10, "errors": True}
        }],
        "simulate_db": True,
        "dag3_no_work": False
    }
    
    with pytest.raises(RuntimeError) as excinfo:
        node_update_index_status(ctx)
    assert "Index push had 1 failures" in str(excinfo.value)


# Test E: Index Lock Test
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_e_index_lock_preemption(mock_get_db_conn):
    """
    Test E: Index Lock Test.
    Verifies that only documents that successfully acquire the index lock are processed,
    and if no locks are acquired, sets ctx["dag3_no_work"] = True and skip_reason cleanly.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn

    # Case 1: Partial preemption lock
    lock_status = {
        ("doc1", 1): 1,
        ("doc2", 2): 0
    }
    def mock_execute(sql, params=None):
        if params and len(params) >= 2:
            doc_id, ver = params[0], params[1]
            mock_cursor.rowcount = lock_status.get((doc_id, ver), 0)
        else:
            mock_cursor.rowcount = 0
        return None
    mock_cursor.execute.side_effect = mock_execute

    chunk1 = Chunk(chunk_id="c1", doc_id="doc1", version_no=1, chunk_index=0, chunk_type="text", chunk_text="t1", token_count=1)
    chunk2 = Chunk(chunk_id="c2", doc_id="doc2", version_no=2, chunk_index=0, chunk_type="text", chunk_text="t2", token_count=1)

    ctx = {
        "valid_chunks": [chunk1, chunk2],
        "simulate_db": False
    }

    node_acquire_index_lock(ctx)

    assert len(ctx["valid_chunks"]) == 1
    assert ctx["valid_chunks"][0].doc_id == "doc1"
    assert ctx.get("dag3_no_work") is not True

    # Case 2: All locks fail, resulting in no chunks and setting dag3_no_work
    mock_cursor.execute.side_effect = None
    mock_cursor.rowcount = 0
    
    ctx_fail = {
        "valid_chunks": [chunk1],
        "simulate_db": False
    }
    
    node_acquire_index_lock(ctx_fail)
    assert len(ctx_fail["valid_chunks"]) == 0
    assert ctx_fail.get("dag3_no_work") is True
    assert ctx_fail.get("skip_reason") == "No document_version index lock acquired"


# Test F: Deactivate failure should explicitly mark FAILED
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
@patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
def test_f_deactivate_failure_marks_failed(mock_get_client, mock_get_db_conn):
    """
    Test F: Deactivate failure should explicitly mark FAILED.
    Verifies that if deactivate_old_chunks fails, the document_version and chunk_meta
    are explicitly marked as FAILED in the database before raising the error.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn

    mock_client = MagicMock(spec=["delete_by_query", "engine_type"])
    # Simulate a standard OpenSearch failure
    mock_client.delete_by_query.return_value = {"failures": [{"reason": "simulated error"}]}
    mock_client.engine_type = "opensearch"
    mock_get_client.return_value = mock_client

    chunk = Chunk(chunk_id="c1", doc_id="doc1", version_no=2, chunk_index=0, chunk_type="text", chunk_text="test", token_count=1)
    
    ctx = {
        "valid_chunks": [chunk],
        "simulate_db": False,
        "simulate_opensearch": False,  # 显式关掉，避免环境 RAG_SIMULATE=true 时走 SIMULATED 分支
        "dag3_no_work": False
    }

    with pytest.raises(RuntimeError) as excinfo:
        node_deactivate_old_chunks(ctx)
    
    assert "Failed to deactivate old chunks in search engine" in str(excinfo.value)

    doc_failed_called = False
    chunk_failed_called = False
    
    for call in mock_cursor.execute.call_args_list:
        sql = call[0][0]
        if "UPDATE document_version SET index_status = 'FAILED'" in sql:
            doc_failed_called = True
        if "UPDATE chunk_meta SET index_status = 'FAILED'" in sql:
            chunk_failed_called = True

    assert doc_failed_called, "document_version must be explicitly marked FAILED"
    assert chunk_failed_called, "chunk_meta must be explicitly marked FAILED"


# Test G: PROCESSING stale recovery or failure reset (retry_count logic in orchestrator)
def test_g_stale_recovery_failure_reset():
    """
    Test G: PROCESSING stale recovery or failure reset.
    We test this by importing the orchestrator and inspecting its raw SQL query
    to ensure it contains the dv.retry_count < 3 and index_status IN ('NOT_INDEXED', 'FAILED') clauses.
    """
    import inspect
    from opensearch_pipeline.dataworks_orchestrator import run_stage
    
    source = inspect.getsource(run_stage)
    
    # Stage 2 retry logic (now in the atomic UPDATE preemption query, no dv. alias)
    assert "content_process_status = 'FAILED' AND retry_count < 3" in source, "Stage 2 query must reset FAILED status with retry_count < 3 limit"
    
    # Stage 2 fake success fix
    assert "has_load_errors = True" in source, "Must track load errors"
    assert "raise RuntimeError(\"Stage 2 completed but had partial OSS load failures. Failing the DataWorks task.\")" in source, "Must raise RuntimeError at end of Stage 2 to prevent fake success"
    
    # Stage 3 explicitly handling FAILED chunk_meta (cm. alias prefix after JOIN fix)
    assert "cm.index_status IN ('NOT_INDEXED', 'FAILED')" in source, "Stage 3 query must retry explicitly FAILED chunks"
    # Stage 3 PROCESSING timeout lease filter (prevents orphaned lock starvation after OOMKill)
    assert "dv.index_status != 'PROCESSING'" in source, "Stage 3 query must filter out PROCESSING documents"
    assert "INTERVAL 2 HOUR" in source, "Stage 3 query must have 2-hour timeout lease for stale PROCESSING recovery"


# Test H: node_acquire_index_lock must be able to TAKE OVER a stale PROCESSING lock.
# Without this arm, the loader re-admits a >2h-stale PROCESSING doc but the lock claim
# (NOT_INDEXED/FAILED/SUCCESS only) can never re-acquire it → permanent wedge.
def test_h_stale_processing_lock_takeover():
    import inspect
    source = inspect.getsource(node_acquire_index_lock)
    assert "index_status = 'PROCESSING'" in source, "lock claim must touch PROCESSING state"
    assert "updated_at < NOW() - INTERVAL 2 HOUR" in source, (
        "node_acquire_index_lock must take over stale (>2h) PROCESSING locks, "
        "matching the orchestrator loader's admission window"
    )


# Test H2 (behavioral): the stale-takeover UPDATE must actually CHANGE the row.
# Test H only greps the source, so it kept passing while the takeover arm was a SQL
# no-op: SET index_status='PROCESSING' on a row already in PROCESSING changes nothing,
# MySQL reports changed-rows=0 (the pool sets no CLIENT_FOUND_ROWS, so pymysql's
# rowcount counts changed rows), ON UPDATE CURRENT_TIMESTAMP doesn't fire, and the
# crashed doc stays wedged forever. This fake cursor emulates exactly those semantics.
class _FakeDocVersionCursor:
    """One in-memory document_version row + MySQL changed-rows semantics for the
    three lock-claim UPDATE shapes issued by node_acquire_index_lock."""

    def __init__(self, row):
        self.row = row  # {"index_status": str, "updated_at": datetime}
        self.rowcount = -1

    def execute(self, sql, params=None):
        import datetime
        now = datetime.datetime.now()
        row = self.row

        if "index_status IN ('NOT_INDEXED', 'FAILED')" in sql:
            matched = row["index_status"] in ("NOT_INDEXED", "FAILED")
        elif "index_status = 'SUCCESS'" in sql:
            matched = row["index_status"] == "SUCCESS"
        elif "index_status = 'PROCESSING'" in sql and "INTERVAL 2 HOUR" in sql:
            matched = (
                row["index_status"] == "PROCESSING"
                and row["updated_at"] < now - datetime.timedelta(hours=2)
            )
        else:
            raise AssertionError(f"unexpected SQL in lock claim: {sql}")

        if not matched:
            self.rowcount = 0
            return

        # changed-rows: the row counts only if some assigned column's value changes
        set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
        changed = False
        if row["index_status"] != "PROCESSING":
            row["index_status"] = "PROCESSING"
            changed = True
        if "updated_at" in set_clause:
            row["updated_at"] = now  # explicit assignment always changes a stale value
            changed = True
        elif changed:
            row["updated_at"] = now  # ON UPDATE CURRENT_TIMESTAMP, only on real change
        self.rowcount = 1 if changed else 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_h2_stale_processing_takeover_changes_row():
    import datetime

    def make_chunk():
        return Chunk(
            chunk_id="doc_stale_v3_c0001",
            doc_id="doc_stale",
            version_no=3,
            chunk_index=1,
            chunk_type="text_chunk",
            chunk_text="wedged chunk",
            token_count=3,
        )

    row = {
        "index_status": "PROCESSING",
        "updated_at": datetime.datetime.now() - datetime.timedelta(hours=3),
    }
    conn = MagicMock()
    conn.cursor.return_value = _FakeDocVersionCursor(row)

    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=conn):
        ctx = {"valid_chunks": [make_chunk()], "simulate_db": False}
        node_acquire_index_lock(ctx)

    assert ("doc_stale", 3) in ctx["preempted_doc_versions"], (
        "stale (>2h) PROCESSING doc must be taken over; a same-value UPDATE reports "
        "changed-rows=0, so the SET clause must also refresh updated_at"
    )
    assert len(ctx["valid_chunks"]) == 1, "chunks of the taken-over doc must survive the filter"
    assert row["updated_at"] > datetime.datetime.now() - datetime.timedelta(minutes=5), (
        "takeover must refresh updated_at (restart the 2h staleness clock)"
    )

    # The refreshed clock makes the lock non-stale again: a second run must NOT steal it.
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=conn):
        ctx2 = {"valid_chunks": [make_chunk()], "simulate_db": False}
        node_acquire_index_lock(ctx2)
    assert ctx2["preempted_doc_versions"] == set(), (
        "a freshly taken-over PROCESSING lock must not be stolen by a second run"
    )
    assert ctx2.get("dag3_no_work") is True


# Test I: the drain-loop must RAISE (non-zero exit) on a stall, not silently break to success.
def test_i_drain_loop_raises_on_no_progress():
    import opensearch_pipeline.dataworks_orchestrator as orch

    calls = {"run_stage": 0}

    def fake_run_stage(stage, bizdate, simulate):
        calls["run_stage"] += 1  # no-op: never drains the queue

    # remaining stays > 0 and never decreases → no-progress guard
    with patch.object(orch, "_count_pending_rows", lambda stage: 5), \
         patch.object(orch, "run_stage", fake_run_stage):
        with pytest.raises(RuntimeError) as excinfo:
            orch.run_stage_drained(stage=3, bizdate="20260609", simulate=False)
    assert "no progress" in str(excinfo.value).lower()


# Test J: stage-3 rollback must read the DAG's returned context (result_ctx), not the
# original ctx (which dag.run copies), or the PROCESSING-lock rollback is dead code.
def test_j_rollback_reads_result_ctx():
    import inspect
    from opensearch_pipeline.dataworks_orchestrator import run_stage
    source = inspect.getsource(run_stage)
    # Exact assignment prefixes (avoids the result_ctx-contains-ctx substring overlap).
    assert 'preempted = result_ctx.get("preempted_doc_versions"' in source, (
        "rollback must read preempted_doc_versions from result_ctx (the dict dag.run mutates)"
    )
    assert 'preempted = ctx.get("preempted_doc_versions"' not in source, (
        "reading preempted_doc_versions from the original ctx is the dead-code bug"
    )


# Test K: embedding-FAILED chunks must be excluded from the HA3 payload AND counted as
# failures so the DAG aborts before deactivating old versions (no silent recall loss).
def test_k_embedding_failed_chunks_excluded_and_block_deactivation():
    from opensearch_pipeline.pipeline_nodes import (
        node_build_opensearch_payload,
        node_update_index_status,
    )

    def mk(cid, status, vec):
        c = Chunk(chunk_id=cid, doc_id="docX", version_no=2, chunk_index=0,
                  chunk_type="text_chunk", chunk_text=f"text-{cid}", token_count=1)
        c.embedding_status = status
        c.embedding_vector = [0.1, 0.2, 0.3] if vec else None
        return c

    ok = mk("c_ok", "DONE", True)
    bad = mk("c_bad", "FAILED", False)
    ctx = {"embedded_chunks": [ok, bad], "dag3_no_work": False}

    node_build_opensearch_payload(ctx)

    # The vectorless FAILED chunk is excluded from the payload and recorded separately.
    assert ctx["embedding_failed_chunks"] == [bad]
    pushed_ids = [c.chunk_id for b in ctx["bulk_batches"] for c in b["chunks"]]
    assert pushed_ids == ["c_ok"], pushed_ids

    # Even with the pushed chunk fully successful, update_index_status must raise (total_failed>0)
    # so node_deactivate_old_chunks never runs for docX.
    ctx["simulate_db"] = True
    for b in ctx["bulk_batches"]:
        b["result"] = {"failed": 0, "indexed": len(b["chunks"]), "took_ms": 1, "errors": False}
    with pytest.raises(RuntimeError) as excinfo:
        node_update_index_status(ctx)
    assert "deactivat" in str(excinfo.value).lower()


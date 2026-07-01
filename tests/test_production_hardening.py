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

    # Status closure UPDATE is now parametrized (chunk_status = %s, content_process_status = %s,
    # content_process_error = %s, ... doc_id, ver). A genuinely-empty doc (no text / ocr_status /
    # warnings) must still close to EMPTY / DONE (graceful — the doc is legitimately empty).
    called = False
    for call in mock_cursor.execute.call_args_list:
        sql = call[0][0]
        params = call[0][1]
        if "UPDATE document_version" in sql and "chunk_status = %s" in sql:
            called = True
            assert params[0] == "EMPTY"
            assert params[1] == "DONE"
            assert params[-2] == "doc_empty"
            assert params[-1] == 2
    assert called, "Should have executed status closure update with 'EMPTY' and 'DONE'"


# Test C2: Suspected-failure 0-chunk doc → NEEDS_REVIEW / FAILED (not silently DONE)
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_c2_suspected_failure_needs_review(mock_get_db_conn):
    """A doc that yields 0 chunks but shows failure signals (OCR failed, or real text that
    produced no chunks, or a failure warning) must NOT close as DONE/EMPTY (which masquerades
    as success and lets a broken SOP vanish from search). It must close as NEEDS_REVIEW / FAILED
    with a reason — and must NOT raise."""
    for canon, why in [
        ({"doc_id": "d_ocrfail", "version_no": 1, "ocr_status": "FAILED", "text_length": 0}, "ocr_failed"),
        ({"doc_id": "d_text0", "version_no": 1, "text_length": 5000}, "text_present"),
        ({"doc_id": "d_warn", "version_no": 1, "text_length": 0,
          "warnings": ["Failed to open DOCX: bad zip"]}, "warn"),
    ]:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_get_db_conn.return_value = mock_conn

        node_write_chunk_meta({"valid_chunks": [], "canonicals": [canon], "simulate_db": False})

        seen = False
        for call in mock_cursor.execute.call_args_list:
            sql, params = call[0][0], call[0][1]
            if "UPDATE document_version" in sql and "chunk_status = %s" in sql:
                seen = True
                assert params[0] == "NEEDS_REVIEW", f"{why}: {params[0]}"
                assert params[1] == "FAILED", f"{why}: {params[1]}"
                assert params[2]  # non-empty reason
                # MUST bump retry_count so the orchestrator's (FAILED AND retry_count<3) re-claim
                # predicate parks deterministically-broken docs after ≤3 tries (no infinite loop).
                assert "retry_count = retry_count + 1" in sql, f"{why}: retry_count not incremented"
        assert seen, f"{why}: expected a status-closure UPDATE"


# Test C4 (F-14): classify fail-safe FAILED write must bump retry_count (else deterministically
# broken docs re-claim forever → stage-2 no-progress guard raises every day).
def test_c4_classify_failsafe_bumps_retry_count():
    """F-14：node_classify_and_risk_assess 的 fail-safe（LLM 400/持续 429/缺 key）写 FAILED 时，
    必须自增 retry_count —— 与 orchestrator 的 (FAILED AND retry_count<3) 认领谓词配套，N 次后
    自然停在 FAILED 等人工，不再每日整轮 no-progress raise。"""
    import inspect, re
    import opensearch_pipeline.pipeline_nodes as pn
    src = inspect.getsource(pn.node_classify_and_risk_assess)
    # 唯一定位 fail-safe UPDATE：含 classification_confidence = 0.0 + content_process_status = 'FAILED'
    m = re.search(r"classification_confidence = 0\.0.*?WHERE doc_id = %s AND version_no = %s",
                  src, re.DOTALL)
    assert m, "未找到 classify fail-safe 的 document_version FAILED UPDATE"
    block = m.group(0)
    assert "content_process_status = 'FAILED'" in block
    assert "retry_count = retry_count + 1" in block, "fail-safe FAILED 未自增 retry_count（F-14 回归）"


# Test C3: QUARANTINE 0-chunk doc keeps EMPTY/DONE (suspected-failure guard must not re-grade it)
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_c3_quarantine_not_regraded(mock_get_db_conn):
    """A PII/cost-quarantined doc skips chunking (0 chunks) and has real text — but it is NOT a
    failure. The suspected-failure guard must leave its prior EMPTY/DONE closure untouched."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn

    node_write_chunk_meta({
        "valid_chunks": [],
        "canonicals": [{"doc_id": "d_quar", "version_no": 1, "text_length": 9000,
                        "redaction_action": "QUARANTINE"}],
        "simulate_db": False,
    })

    seen = False
    for call in mock_cursor.execute.call_args_list:
        sql, params = call[0][0], call[0][1]
        if "UPDATE document_version" in sql and "chunk_status = %s" in sql:
            seen = True
            assert params[0] == "EMPTY"
            assert params[1] == "DONE"
    assert seen, "expected a status-closure UPDATE for quarantined doc"


# ── node_write_chunk_meta full-replacement (anti-strand) regression suite ──────────────────────
# Regression for the 2026-06-15 50-doc batch: the node deleted only the NEW chunk_ids (retry-
# idempotency), so a doc re-chunked 8 -> 4 left old idx 4-7 active+INDEXED forever (strand). The fix
# is full-replace DELETE-by-(doc_id, version_no), guarded against partial-doc batches.

class _FakeChunkMetaConn:
    """Stateful in-memory chunk_meta table to prove full-replacement semantics + tx rollback.
    Ignores non-chunk_meta SQL (e.g. document_version status closure updates)."""
    def __init__(self, seed_rows=None, fail_on_insert=False):
        self.rows = [dict(r) for r in (seed_rows or [])]
        self._snapshot = [dict(r) for r in self.rows]
        self.fail_on_insert = fail_on_insert
        self.rowcount = 0

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = sql.strip()
        if s.startswith("DELETE FROM chunk_meta") and "doc_id" in s and "version_no" in s:
            pairs, it = set(), iter(params or [])
            for d in it:
                pairs.add((d, next(it)))
            self.rows = [r for r in self.rows if (r["doc_id"], r["version_no"]) not in pairs]
        elif s.startswith("DELETE FROM chunk_meta") and "chunk_id IN" in s:  # the OLD buggy form
            ids = set(params or [])
            self.rows = [r for r in self.rows if r["chunk_id"] not in ids]
        # else: ignore (UPDATE document_version, etc.)
        self.rowcount = 0

    def executemany(self, sql, rows):
        if "INSERT INTO chunk_meta" in sql:
            if self.fail_on_insert:
                raise RuntimeError("simulated INSERT failure")
            for r in rows:
                self.rows.append({"chunk_id": r[0], "doc_id": r[1], "version_no": r[2], "chunk_index": r[3]})

    def commit(self):
        self._snapshot = [dict(r) for r in self.rows]

    def rollback(self):
        self.rows = [dict(r) for r in self._snapshot]

    def close(self):
        pass


def _seed(doc, ver, n):
    return [{"chunk_id": f"{doc}_v{ver}_c{i:04d}_X", "doc_id": doc, "version_no": ver, "chunk_index": i}
            for i in range(n)]


def _mk(doc, ver, idx):
    return Chunk(chunk_id=f"{doc}_v{ver}_c{idx:04d}_X", doc_id=doc, version_no=ver,
                 chunk_index=idx, chunk_type="clause_chunk", chunk_text=f"body {idx}", token_count=5)


def _idxs(fake, doc):
    return sorted(r["chunk_index"] for r in fake.rows if r["doc_id"] == doc)


def test_rechunk_shrink_8_to_4_old_high_chunks_gone():
    fake = _FakeChunkMetaConn(_seed("d", 1, 8))  # old: idx 0..7
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=fake):
        ctx = {"valid_chunks": [_mk("d", 1, i) for i in range(4)],
               "canonicals": [{"doc_id": "d", "version_no": 1}], "simulate_db": False}
        node_write_chunk_meta(ctx)
    assert _idxs(fake, "d") == [0, 1, 2, 3], "old high chunks idx 4-7 must be gone after shrink"


def test_rechunk_grow_4_to_8_all_new_written():
    fake = _FakeChunkMetaConn(_seed("d", 1, 4))  # old: idx 0..3
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=fake):
        ctx = {"valid_chunks": [_mk("d", 1, i) for i in range(8)],
               "canonicals": [{"doc_id": "d", "version_no": 1}], "simulate_db": False}
        node_write_chunk_meta(ctx)
    assert _idxs(fake, "d") == list(range(8)), "all 8 new chunks must be present after growth"


def test_rechunk_same_count_retry_is_idempotent():
    fake = _FakeChunkMetaConn(_seed("d", 1, 4))
    for _ in range(2):  # run twice -> identical result
        with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=fake):
            ctx = {"valid_chunks": [_mk("d", 1, i) for i in range(4)],
                   "canonicals": [{"doc_id": "d", "version_no": 1}], "simulate_db": False}
            node_write_chunk_meta(ctx)
    assert _idxs(fake, "d") == [0, 1, 2, 3] and len(fake.rows) == 4, "retry must be idempotent (no dup)"


def test_rechunk_multi_doc_no_cross_impact():
    fake = _FakeChunkMetaConn(_seed("A", 1, 3) + _seed("B", 1, 5))
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=fake):
        ctx = {"valid_chunks": [_mk("A", 1, i) for i in range(2)] + [_mk("B", 1, i) for i in range(6)],
               "canonicals": [{"doc_id": "A", "version_no": 1}, {"doc_id": "B", "version_no": 1}],
               "simulate_db": False}
        node_write_chunk_meta(ctx)
    assert _idxs(fake, "A") == [0, 1], "A shrinks to 2, independent of B"
    assert _idxs(fake, "B") == [0, 1, 2, 3, 4, 5], "B grows to 6, independent of A"


def test_rechunk_insert_failure_rolls_back_delete():
    fake = _FakeChunkMetaConn(_seed("d", 1, 4), fail_on_insert=True)
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=fake):
        ctx = {"valid_chunks": [_mk("d", 1, i) for i in range(2)],
               "canonicals": [{"doc_id": "d", "version_no": 1}], "simulate_db": False}
        with pytest.raises(RuntimeError):
            node_write_chunk_meta(ctx)
    # DELETE must have been rolled back together with the failed INSERT -> original 4 intact
    assert _idxs(fake, "d") == [0, 1, 2, 3], "INSERT failure must roll back the DELETE (no data loss)"


def test_rechunk_partial_doc_batch_rejected_without_delete():
    # a (doc,version) whose chunks arrive WITHOUT that doc being a fully-chunked canonical of this run
    # is a partial / foreign batch — we cannot prove we hold its complete set, so we must NOT
    # full-replace-DELETE it. Guard raises BEFORE any DELETE; the existing rows stay intact.
    fake = _FakeChunkMetaConn(_seed("d", 1, 4))
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=fake):
        ctx = {"valid_chunks": [_mk("d", 1, 0), _mk("d", 1, 1)],
               "canonicals": [{"doc_id": "other", "version_no": 1}], "simulate_db": False}
        with pytest.raises(RuntimeError, match="canonicals"):
            node_write_chunk_meta(ctx)
    assert _idxs(fake, "d") == [0, 1, 2, 3], "partial/foreign batch must NOT delete anything"

    # also rejected when canonicals is empty (no evidence of full chunking this run)
    fake2 = _FakeChunkMetaConn(_seed("d", 1, 4))
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=fake2):
        ctx = {"valid_chunks": [_mk("d", 1, 0)], "canonicals": [], "simulate_db": False}
        with pytest.raises(RuntimeError):
            node_write_chunk_meta(ctx)
    assert _idxs(fake2, "d") == [0, 1, 2, 3], "empty-canonicals batch must NOT delete anything"


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


# Test K2 (P0 regression, 2026-06-16): a partial DashScope embedding response omits a text_index,
# so embedding_client returns None for that slot and node_generate_embeddings leaves the chunk at
# embedding_status="NOT_STARTED" with NO vector — NOT "FAILED". It must be treated exactly like
# FAILED: excluded from the payload AND counted as a failure so the DAG aborts before deactivating
# old versions. Before the fix the payload filter only excluded "== FAILED", so this vectorless
# NOT_STARTED chunk was pushed kNN-invisible, marked INDEXED, and the old version deactivated →
# silent, non-deterministic recall loss. Test K covers FAILED; this covers the NOT_STARTED/None slot.
def test_k2_embedding_not_started_none_slot_excluded_and_blocks_deactivation():
    from opensearch_pipeline.pipeline_nodes import (
        node_build_opensearch_payload,
        node_update_index_status,
    )

    def mk(cid, status, vec):
        c = Chunk(chunk_id=cid, doc_id="docY", version_no=3, chunk_index=0,
                  chunk_type="text_chunk", chunk_text=f"text-{cid}", token_count=1)
        c.embedding_status = status
        c.embedding_vector = [0.1, 0.2, 0.3] if vec else None
        return c

    ok = mk("c_ok", "DONE", True)
    none_slot = mk("c_none", "NOT_STARTED", False)  # the omitted-text_index / None-slot case
    ctx = {"embedded_chunks": [ok, none_slot], "dag3_no_work": False}

    node_build_opensearch_payload(ctx)

    # The vectorless NOT_STARTED chunk is excluded from the payload and recorded as failed.
    assert ctx["embedding_failed_chunks"] == [none_slot]
    pushed_ids = [c.chunk_id for b in ctx["bulk_batches"] for c in b["chunks"]]
    assert pushed_ids == ["c_ok"], pushed_ids

    # Even with the pushed chunk fully successful, update_index_status must raise (total_failed>0)
    # so node_deactivate_old_chunks never runs for docY.
    ctx["simulate_db"] = True
    for b in ctx["bulk_batches"]:
        b["result"] = {"failed": 0, "indexed": len(b["chunks"]), "took_ms": 1, "errors": False}
    with pytest.raises(RuntimeError) as excinfo:
        node_update_index_status(ctx)
    assert "deactivat" in str(excinfo.value).lower()


# Test K3 (non-regression for the broadened "!= DONE" filter): a fully-DONE batch must NOT be
# flagged — every chunk is pushed and update_index_status does not raise. Guards against the
# fix over-excluding legitimate chunks (e.g. if a chunk type were ever left non-DONE by design).
def test_k3_all_done_batch_not_flagged_by_broadened_filter():
    from opensearch_pipeline.pipeline_nodes import (
        node_build_opensearch_payload,
        node_update_index_status,
    )

    def mk(cid):
        c = Chunk(chunk_id=cid, doc_id="docZ", version_no=1, chunk_index=0,
                  chunk_type="text_chunk", chunk_text=f"text-{cid}", token_count=1)
        c.embedding_status = "DONE"
        c.embedding_vector = [0.1, 0.2, 0.3]
        return c

    chunks = [mk("c1"), mk("c2")]
    ctx = {"embedded_chunks": chunks, "dag3_no_work": False}
    node_build_opensearch_payload(ctx)

    assert ctx["embedding_failed_chunks"] == []
    pushed_ids = sorted(c.chunk_id for b in ctx["bulk_batches"] for c in b["chunks"])
    assert pushed_ids == ["c1", "c2"], pushed_ids

    # All DONE + zero push failures → must NOT raise (normal deactivation may proceed).
    ctx["simulate_db"] = True
    for b in ctx["bulk_batches"]:
        b["result"] = {"failed": 0, "indexed": len(b["chunks"]), "took_ms": 1, "errors": False}
    node_update_index_status(ctx)


# Test L: real-mode push must REJECT the mock client string, never fake INDEXED.
# A ctx/config simulate mismatch used to fake INDEXED and then let deactivation
# delete the old version for real (split-brain).
@patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
def test_l_push_rejects_mock_client_in_real_mode(mock_get_client):
    from opensearch_pipeline.pipeline_nodes import node_push_to_opensearch

    mock_get_client.return_value = "MOCK_HA3_CLIENT"
    chunk = Chunk(chunk_id="c1", doc_id="doc1", version_no=1, chunk_index=0,
                  chunk_type="text_chunk", chunk_text="t", token_count=1)
    ctx = {
        "bulk_batches": [{"chunks": [chunk], "payload": "x", "payload_size": 1,
                          "job_id": "J1", "oss_key": ""}],
        "simulate": False,
        "simulate_opensearch": False,
        "dag3_no_work": False,
    }
    with pytest.raises(RuntimeError) as excinfo:
        node_push_to_opensearch(ctx)
    assert "MOCK_HA3_CLIENT" in str(excinfo.value)
    assert chunk.index_status != "INDEXED", "mock client must never produce a fake INDEXED status"


# Test M: real-mode deactivation must reject the mock client BEFORE any RDS
# deactivation write (the FAILED fail-safe writes are allowed; is_active=FALSE is not).
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
@patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
def test_m_deactivate_rejects_mock_client_in_real_mode(mock_get_client, mock_get_db_conn):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(101,), (102,)]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn
    mock_get_client.return_value = "MOCK_HA3_CLIENT"

    chunk = Chunk(chunk_id="c1", doc_id="doc1", version_no=2, chunk_index=0,
                  chunk_type="text_chunk", chunk_text="t", token_count=1)
    chunk.index_status = "INDEXED"
    chunk.embedding_status = "DONE"  # 真正成功索引的当前 chunk 必为 DONE（否则即 P0 僵尸，被第三层护栏拦下）
    ctx = {
        "valid_chunks": [chunk],
        "preempted_doc_versions": {("doc1", 2)},
        "simulate_db": False,
        "simulate_opensearch": False,
        "dag3_no_work": False,
    }
    with pytest.raises(RuntimeError):
        node_deactivate_old_chunks(ctx)

    for call in mock_cursor.execute.call_args_list:
        sql = call[0][0]
        assert "is_active = FALSE" not in sql, (
            "a mock client in real mode must never lead to real RDS old-version deactivation"
        )


# Test M2 (P0 layer-3, 2026-06-16): node_deactivate_old_chunks must REFUSE to deactivate when a
# current-version chunk is a "zombie" — index_status==INDEXED but embedding_status!=DONE (pushed
# vectorless). This positive invariant is independent of ctx["embedding_failed_chunks"]/failed_doc_versions,
# so it catches any path reaching deactivation while the new version is incompletely indexed even when
# those negative sets were never populated (e.g. a future reconcile/refactor bypassing the raise gate).
def test_m2_deactivate_refuses_indexed_but_not_embedded_zombie():
    from opensearch_pipeline.pipeline_nodes import node_deactivate_old_chunks

    good = Chunk(chunk_id="g", doc_id="docZ", version_no=2, chunk_index=0,
                 chunk_type="text_chunk", chunk_text="t", token_count=1)
    good.embedding_status = "DONE"; good.index_status = "INDEXED"
    zombie = Chunk(chunk_id="z", doc_id="docZ", version_no=2, chunk_index=1,
                   chunk_type="text_chunk", chunk_text="t", token_count=1)
    zombie.embedding_status = "NOT_STARTED"; zombie.index_status = "INDEXED"  # pushed vectorless (the P0)

    ctx = {
        "valid_chunks": [good, zombie],
        "preempted_doc_versions": {("docZ", 2)},
        "existing_opensearch_chunks": [{"chunk_id": "old", "doc_id": "docZ", "version_no": 1}],
        # deliberately NOT populating embedding_failed_chunks/failed_doc_versions —
        # the layer-3 invariant must catch the zombie WITHOUT the negative sets.
        "simulate_db": True,
        "simulate_opensearch": True,
        "dag3_no_work": False,
    }
    with pytest.raises(RuntimeError) as ei:
        node_deactivate_old_chunks(ctx)
    assert "zombie" in str(ei.value).lower() or "INDEXED without a DONE" in str(ei.value)


# Test N: _get_opensearch_client must resolve the simulate flag from ctx
# (granular > global > config), like _get_oss_bucket already does.
def test_n_client_getter_resolves_ctx_flag(monkeypatch):
    import inspect
    from opensearch_pipeline.pipeline_nodes import _get_opensearch_client
    from opensearch_pipeline.config import get_config

    config = get_config()
    monkeypatch.setattr(config, "simulate_opensearch", False)
    # ctx 全局 simulate=True 必须压过 config=False，返回 mock（不 import 任何 SDK）
    assert _get_opensearch_client({"simulate": True}) == "MOCK_HA3_CLIENT"
    assert _get_opensearch_client({"simulate_opensearch": True}) == "MOCK_HA3_CLIENT"
    # 解析必须走统一的 _resolve_simulate（防再次漂移成手写三层取值）
    assert "_resolve_simulate" in inspect.getsource(_get_opensearch_client)


# Test O: the orchestrator must propagate the granular simulate flags into ctx —
# they used to be dead config under the scheduling entrypoint.
def test_o_orchestrator_ctx_propagates_granular_flags():
    import opensearch_pipeline.dataworks_orchestrator as orch

    captured = {}
    fake_dag = MagicMock()

    def fake_run(ctx):
        captured.update(ctx)
        return dict(ctx)

    fake_dag.run.side_effect = fake_run
    fake_dag.nodes = {}
    with patch.object(orch, "build_dag1_raw_to_canonical", return_value=fake_dag):
        orch.run_stage(1, "20260610", simulate=True)

    for key in ("simulate_db", "simulate_oss", "simulate_opensearch", "simulate_api"):
        # --simulate 运行必须全模拟：细粒度键优先级最高，不强制 True 会做真实 I/O
        assert captured.get(key) is True, f"ctx must carry {key}=True under --simulate"


# Test P: stage 3 must refuse to run with mixed real/mock between RDS and the
# search store (guaranteed split-brain: DAG 3 writes both).
def test_p_stage3_rejects_mixed_db_opensearch_simulation(monkeypatch):
    import opensearch_pipeline.dataworks_orchestrator as orch
    from opensearch_pipeline.config import get_config

    config = get_config()
    monkeypatch.setattr(config, "simulate_db", False)
    monkeypatch.setattr(config, "simulate_opensearch", True)

    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn",
               side_effect=AssertionError("must not touch DB before the guard")):
        with pytest.raises(RuntimeError) as excinfo:
            orch.run_stage(3, "20260610", simulate=False)
    assert "split-brain" in str(excinfo.value)


# Test Q: the stage-2 stale-lock takeover SQL must reset both unguarded claim
# states with the house 2h TTL, count the retry, and refresh updated_at.
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
def test_q_stale_stage2_lock_reset_sql(mock_get_db_conn):
    import opensearch_pipeline.dataworks_orchestrator as orch

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.rowcount = 2
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn

    n = orch._reset_stale_stage2_locks()

    assert n == 2
    sql = mock_cursor.execute.call_args[0][0]
    assert "IN ('LOADING', 'PROCESSING')" in sql
    assert "INTERVAL 2 HOUR" in sql
    assert "retry_count = retry_count + 1" in sql
    assert "updated_at = NOW()" in sql
    assert "'FAILED'" in sql
    mock_conn.commit.assert_called_once()


# Test R: the drain loop must run the stage-2 lock reset BEFORE each pending-count
# (reset rows become FAILED&retry<3 and are visible to the same iteration's count),
# and must not run it for stage 3 or in simulate mode.
def test_r_drain_loop_resets_stage2_before_count():
    import opensearch_pipeline.dataworks_orchestrator as orch

    order = []
    with patch.object(orch, "_reset_stale_stage2_locks",
                      side_effect=lambda: order.append("reset") or 0), \
         patch.object(orch, "_count_pending_rows",
                      side_effect=lambda stage: order.append("count")
                      or (1 if order.count("count") == 1 else 0)), \
         patch.object(orch, "run_stage",
                      side_effect=lambda *a, **k: order.append("run")):
        orch.run_stage_drained(stage=2, bizdate="20260610", simulate=False)
    assert order[:2] == ["reset", "count"], order
    assert "run" in order

    order2 = []
    with patch.object(orch, "_reset_stale_stage2_locks",
                      side_effect=lambda: order2.append("reset") or 0), \
         patch.object(orch, "_count_pending_rows", side_effect=lambda stage: 0), \
         patch.object(orch, "run_stage", MagicMock()), \
         patch("opensearch_pipeline.spot_checker.reconcile_stranded_versions",
               return_value={"total": 0, "success": 0, "failed": 0, "errors": []}):
        orch.run_stage_drained(stage=3, bizdate="20260610", simulate=False)
    assert "reset" not in order2, "stage-2 lock reset must not run for stage 3"

    with patch.object(orch, "_reset_stale_stage2_locks") as mock_reset, \
         patch.object(orch, "_count_pending_rows") as mock_count, \
         patch.object(orch, "run_stage", MagicMock()):
        orch.run_stage_drained(stage=2, bizdate="20260610", simulate=True)
    mock_reset.assert_not_called()
    mock_count.assert_not_called()


# Test S: the deactivation node's per-doc defense filter — known-failed docs are
# excluded from old-version deactivation even when their in-memory chunks look
# clean (the embedding-FAILED case: chunks stay NOT_INDEXED, failed_counts can't
# see them), while healthy docs in the same run still deactivate, and the failed
# doc still gets document_version='FAILED'.
@patch("opensearch_pipeline.pipeline_nodes._get_db_conn")
@patch("opensearch_pipeline.pipeline_nodes._get_opensearch_client")
def test_s_deactivate_skips_known_failed_docs(mock_get_client, mock_get_db_conn):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(101,)]
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    mock_get_db_conn.return_value = mock_conn

    mock_client = MagicMock(spec=["push_documents"])
    resp = MagicMock()
    resp.status_code = 200
    resp.body = ""
    resp.text = ""
    mock_client.push_documents.return_value = resp
    mock_get_client.return_value = mock_client

    def mk(cid, doc, status):
        c = Chunk(chunk_id=cid, doc_id=doc, version_no=2, chunk_index=0,
                  chunk_type="text_chunk", chunk_text="t", token_count=1)
        c.index_status = status
        c.embedding_status = "DONE" if status == "INDEXED" else "FAILED"  # INDEXED ⇒ DONE（非僵尸）
        return c

    ok_chunk = mk("ca", "docA", "INDEXED")
    bad_chunk = mk("cb", "docB", "NOT_INDEXED")  # embedding-FAILED：内存状态看不出失败
    ctx = {
        "valid_chunks": [ok_chunk, bad_chunk],
        "embedding_failed_chunks": [bad_chunk],
        "preempted_doc_versions": {("docA", 2), ("docB", 2)},
        "simulate_db": False,
        "simulate_opensearch": False,
        "dag3_no_work": False,
    }

    node_deactivate_old_chunks(ctx)

    deactivate_docs = []
    doc_status = {}
    for call in mock_cursor.execute.call_args_list:
        sql = call[0][0]
        params = call[0][1] if len(call[0]) > 1 else None
        if "is_active = FALSE" in sql and params:
            deactivate_docs.append(params[0])
        if "UPDATE document_version" in sql and "SET index_status = %s" in sql and params:
            doc_status[params[1]] = params[0]

    assert deactivate_docs == ["docA"], (
        f"only the healthy doc may deactivate old versions, got {deactivate_docs}"
    )
    assert doc_status.get("docA") == "SUCCESS"
    assert doc_status.get("docB") == "FAILED", (
        "the embedding-failed doc must still be written FAILED (not silent SUCCESS)"
    )


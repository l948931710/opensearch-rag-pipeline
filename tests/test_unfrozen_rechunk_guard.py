"""Unfrozen re-chunk guard (node_classify_and_risk_assess).

A re-chunk of an already-chunked doc that runs WITHOUT a freeze would re-roll the LLM classification
and can flip the chunk family run-to-run (the PRODUCTION_14DFDF 79-vs-47 incident). The guard
fail-closes on that, while leaving first ingest / version bumps / frozen maintenance untouched.

These tests run with simulate_db=False (so the guard's chunk_meta lookup + the normal write path
execute) against a query-aware fake DB connection — conftest forbids touching prod, so nothing real is
written. See plan: .claude/plans/gpt-llm-classification-functional-hare.md
"""
from datetime import date
from unittest.mock import patch

import pytest

from opensearch_pipeline.pipeline_nodes import node_classify_and_risk_assess
from opensearch_pipeline.reindex_states import docset_hash

LLM_PATH = "opensearch_pipeline.pipeline_nodes.run_gemini_classification"
DB_PATH = "opensearch_pipeline.pipeline_nodes._get_db_conn"
TODAY = date.today().isoformat()


def _doc(doc_id, version_no=1, cat1="sop", cat2="safety_sop", text="第一条 内容。\n第二条 内容。"):
    return {"doc_id": doc_id, "version_no": version_no, "text": text,
            "source_key": f"public/{doc_id}.docx"}


# ── query-aware fake DB connection ───────────────────────────────────────────
# Models chunk_meta as a set of (doc_id, version_no) that already have rows. The guard's
# `SELECT ... FROM chunk_meta WHERE (doc_id=%s AND version_no=%s) OR ...` returns only the targets
# present in that set; every other statement (preempt UPDATE, classify write UPDATEs) is a recorded
# no-op. `executed` is shared so a test can assert the preempt UPDATE never ran (guard blocked first).

class _FakeCursor:
    def __init__(self, existing, executed):
        self._existing = existing
        self._executed = executed
        self._last = []
        self.rowcount = 1
        self.lastrowid = 1

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        s = (sql or "").strip().upper()
        if s.startswith("SELECT") and "CHUNK_META" in s:
            flat = list(params or [])
            pairs = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
            self._last = [{"doc_id": d, "version_no": v} for (d, v) in pairs
                          if (d, v) in self._existing]
        else:
            self._last = []

    def executemany(self, sql, seq=None):
        self._executed.append((sql, "<many>"))

    def fetchall(self):
        return list(self._last)

    def fetchone(self):
        return self._last[0] if self._last else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, existing, executed):
        self._existing = existing
        self._executed = executed

    def cursor(self, *a, **k):
        return _FakeCursor(self._existing, self._executed)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _conn_factory(existing):
    """(side_effect, executed) — patch _get_db_conn with side_effect, inspect executed SQL."""
    executed = []
    return (lambda *a, **k: _FakeConn(set(existing), executed)), executed


def _good_classification():
    return {"category_l1": "sop", "category_l2": "safety_sop", "faq_eligible": False,
            "confidence": 0.9, "llm_risk_level": "low", "summary": "s"}


# ── 1. unfrozen + prior chunks → blocked, before preempt, 0 LLM ──────────────
@patch(LLM_PATH)
@patch(DB_PATH)
def test_unfrozen_rechunk_with_prior_chunks_blocks(mock_db, mock_llm):
    factory, executed = _conn_factory({("d1", 1)})
    mock_db.side_effect = factory
    ctx = {"canonicals": [_doc("d1")], "simulate_db": False, "simulate_api": False}
    with pytest.raises(RuntimeError, match="unfrozen re-chunk blocked"):
        node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0, "must not reclassify on a blocked unfrozen re-chunk"
    assert not any("PROCESSING" in (q[0] or "") for q in executed), \
        "must raise BEFORE the preempt UPDATE (nothing left in PROCESSING)"


# ── 2. first ingest (no prior chunks) → passes, classifier runs (regression) ──
@patch(LLM_PATH)
@patch(DB_PATH)
def test_first_ingest_passes(mock_db, mock_llm, llm_key_present):
    mock_llm.return_value = _good_classification()
    factory, _ = _conn_factory(set())  # chunk_meta empty for this (doc_id, version_no)
    mock_db.side_effect = factory
    docs = [_doc("d1")]
    ctx = {"canonicals": docs, "simulate_db": False, "simulate_api": False}
    node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 1
    assert docs[0]["category_l1"] == "sop"


# ── 3. version bump → passes (predicate keys on exact (doc_id, version_no)) ───
@patch(LLM_PATH)
@patch(DB_PATH)
def test_version_bump_passes(mock_db, mock_llm, llm_key_present):
    mock_llm.return_value = _good_classification()
    # v1 has chunks, but this run is v2 → the guard's SELECT for (d1, 2) returns nothing.
    factory, _ = _conn_factory({("d1", 1)})
    mock_db.side_effect = factory
    docs = [_doc("d1", version_no=2)]
    ctx = {"canonicals": docs, "simulate_db": False, "simulate_api": False}
    node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 1, "a genuine version bump must re-classify"


# ── 4. frozen_routing present + prior chunks → passes, 0 LLM (unchanged) ──────
@patch(LLM_PATH)
@patch(DB_PATH)
def test_frozen_routing_bypasses_guard(mock_db, mock_llm):
    factory, _ = _conn_factory({("d1", 1)})
    mock_db.side_effect = factory
    docs = [_doc("d1")]
    ctx = {"canonicals": docs, "simulate_db": False, "simulate_api": False,
           "frozen_routing": {"d1": {"category_l1": "sop", "category_l2": "inspection_sop"}}}
    node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0, "frozen maintenance must never call the classifier"
    assert docs[0]["classification_status"] == "FROZEN_MAINTENANCE"


# ── 5a. doc-set-bound ack (correct) → passes, classifier runs ────────────────
@patch(LLM_PATH)
@patch(DB_PATH)
def test_correct_docset_ack_allows_reclassify(mock_db, mock_llm, llm_key_present):
    mock_llm.return_value = _good_classification()
    factory, _ = _conn_factory({("d1", 1)})
    mock_db.side_effect = factory
    token = f"me:{TODAY}:{docset_hash(['d1'])}"
    ctx = {"canonicals": [_doc("d1")], "simulate_db": False, "simulate_api": False,
           "allow_unfrozen_rechunk": token}
    node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 1, "a valid doc-set-bound override authorizes re-classification"


# ── 5b. stale-date ack → blocked ─────────────────────────────────────────────
@patch(LLM_PATH)
@patch(DB_PATH)
def test_stale_date_ack_blocks(mock_db, mock_llm):
    factory, _ = _conn_factory({("d1", 1)})
    mock_db.side_effect = factory
    token = f"me:2020-01-01:{docset_hash(['d1'])}"  # right hash, dead date
    ctx = {"canonicals": [_doc("d1")], "simulate_db": False, "simulate_api": False,
           "allow_unfrozen_rechunk": token}
    with pytest.raises(RuntimeError, match="unfrozen re-chunk blocked"):
        node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0


# ── 5c. ack minted for a DIFFERENT doc-set → blocked (token reuse defense) ────
@patch(LLM_PATH)
@patch(DB_PATH)
def test_wrong_docset_hash_blocks(mock_db, mock_llm):
    factory, _ = _conn_factory({("d1", 1)})
    mock_db.side_effect = factory
    token = f"me:{TODAY}:{docset_hash(['some_other_doc'])}"  # today, wrong doc-set
    ctx = {"canonicals": [_doc("d1")], "simulate_db": False, "simulate_api": False,
           "allow_unfrozen_rechunk": token}
    with pytest.raises(RuntimeError, match="unfrozen re-chunk blocked"):
        node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0


# ── 6. simulate_db=True → guard skipped, no DB connection opened ──────────────
@patch(LLM_PATH)
@patch(DB_PATH)
def test_simulate_db_skips_guard(mock_db, mock_llm, llm_key_present):
    mock_llm.return_value = _good_classification()
    ctx = {"canonicals": [_doc("d1")], "simulate_db": True, "simulate_api": False}
    node_classify_and_risk_assess(ctx)
    assert mock_db.call_count == 0, "simulate_db must not open any DB connection (guard skipped)"
    assert mock_llm.call_count == 1


# ── 7. DB lookup error → fail closed (does NOT fall through to classify) ──────
@patch("time.sleep", lambda *_a, **_k: None)  # skip the 2s retry backoff
@patch(LLM_PATH)
@patch(DB_PATH)
def test_db_error_fails_closed(mock_db, mock_llm):
    mock_db.side_effect = RuntimeError("RDS unreachable")
    ctx = {"canonicals": [_doc("d1")], "simulate_db": False, "simulate_api": False}
    with pytest.raises(RuntimeError, match="fail closed"):
        node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0, "an un-verifiable guard must block, never classify"


# ── 8. mixed batch (fresh + re-chunk) → whole run blocked, before preempt ─────
@patch(LLM_PATH)
@patch(DB_PATH)
def test_mixed_batch_blocks_whole_run(mock_db, mock_llm):
    factory, executed = _conn_factory({("d2", 1)})  # only d2 already chunked
    mock_db.side_effect = factory
    ctx = {"canonicals": [_doc("d1"), _doc("d2")], "simulate_db": False, "simulate_api": False}
    with pytest.raises(RuntimeError, match="unfrozen re-chunk blocked"):
        node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0, "one re-chunk doc must abort the whole batch (fresh d1 included)"
    assert not any("PROCESSING" in (q[0] or "") for q in executed)


# ── crash-resume auto-freeze（ultra P1 2026-07-17）────────────────────────────
# node_write_chunk_meta 在 chunk 行提交后、DONE 收口前崩溃 → 文档卡 PROCESSING → orchestrator
# stale sweep 置 FAILED+retry+1。这类目标有 chunk_meta 行，但与 deliberate reset_for_rechunk
# （NOT_STARTED+retry=0）状态互斥：应 auto-freeze 复用存储分类续跑，绝不整批 raise 楔死 stage-2。

class _CrashFakeCursor:
    """在 _FakeCursor 基础上再 mock `document_version JOIN document_meta` 状态查询。
    dv_status: {(doc_id,version_no): (content_process_status, retry_count, category_l1, category_l2)}。"""

    def __init__(self, existing, dv_status, executed):
        self._existing = existing
        self._dv = dv_status
        self._executed = executed
        self._last = []
        self.rowcount = 1
        self.lastrowid = 1

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        s = (sql or "").strip().upper()
        flat = list(params or [])
        pairs = [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
        if s.startswith("SELECT") and "CHUNK_META" in s:
            self._last = [{"doc_id": d, "version_no": v} for (d, v) in pairs
                          if (d, v) in self._existing]
        elif s.startswith("SELECT") and "DOCUMENT_VERSION" in s and "DOCUMENT_META" in s:
            self._last = []
            for (d, v) in pairs:
                if (d, v) in self._dv:
                    cps, rc, c1, c2 = self._dv[(d, v)]
                    self._last.append({"doc_id": d, "version_no": v,
                                       "content_process_status": cps, "retry_count": rc,
                                       "category_l1": c1, "category_l2": c2})
        else:
            self._last = []

    def executemany(self, sql, seq=None):
        self._executed.append((sql, "<many>"))

    def fetchall(self):
        return list(self._last)

    def fetchone(self):
        return self._last[0] if self._last else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _CrashFakeConn:
    def __init__(self, existing, dv_status, executed):
        self._existing, self._dv, self._executed = existing, dv_status, executed

    def cursor(self, *a, **k):
        return _CrashFakeCursor(self._existing, self._dv, self._executed)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _crash_factory(existing, dv_status):
    executed = []
    return (lambda *a, **k: _CrashFakeConn(set(existing), dict(dv_status), executed)), executed


@patch(LLM_PATH)
@patch(DB_PATH)
def test_crash_resume_autofreezes_not_blocks(mock_db, mock_llm):
    """crash-resume（FAILED+retry>0+有存储分类）→ auto-freeze 复用分类续跑，绝不 raise，0 LLM。"""
    factory, _ = _crash_factory({("d1", 1)}, {("d1", 1): ("FAILED", 1, "sop", "safety_sop")})
    mock_db.side_effect = factory
    docs = [_doc("d1")]
    ctx = {"canonicals": docs, "simulate_db": False, "simulate_api": False}
    node_classify_and_risk_assess(ctx)   # 不抛
    assert mock_llm.call_count == 0, "crash-resume 复用存储分类，绝不重跑 LLM classifier"
    assert docs[0]["category_l1"] == "sop"
    assert docs[0]["classification_status"] == "FROZEN_MAINTENANCE"


@patch(LLM_PATH)
@patch(DB_PATH)
def test_deliberate_rechunk_notstarted_still_blocks(mock_db, mock_llm):
    """deliberate reset_for_rechunk（NOT_STARTED+retry=0）不是 crash-resume → 仍整批 fail-closed。"""
    factory, _ = _crash_factory({("d1", 1)}, {("d1", 1): ("NOT_STARTED", 0, "sop", "safety_sop")})
    mock_db.side_effect = factory
    ctx = {"canonicals": [_doc("d1")], "simulate_db": False, "simulate_api": False}
    with pytest.raises(RuntimeError, match="unfrozen re-chunk blocked"):
        node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0


@patch(LLM_PATH)
@patch(DB_PATH)
def test_crash_resume_missing_category_fails_closed(mock_db, mock_llm):
    """crash-resume 但存储分类丢失（category_l1=NULL）→ 降级 deliberate → fail-closed raise。"""
    factory, _ = _crash_factory({("d1", 1)}, {("d1", 1): ("FAILED", 2, None, None)})
    mock_db.side_effect = factory
    ctx = {"canonicals": [_doc("d1")], "simulate_db": False, "simulate_api": False}
    with pytest.raises(RuntimeError, match="unfrozen re-chunk blocked"):
        node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0


@patch(LLM_PATH)
@patch(DB_PATH)
def test_mixed_fresh_plus_crash_resume_both_proceed(mock_db, mock_llm, llm_key_present):
    """mixed：fresh d1（无 chunk）正常分类 + crash-resume d2 auto-freeze —— 整批不再被 d2 楔死。"""
    mock_llm.return_value = _good_classification()
    factory, _ = _crash_factory({("d2", 1)}, {("d2", 1): ("FAILED", 1, "sop", "inspection_sop")})
    mock_db.side_effect = factory
    docs = [_doc("d1"), _doc("d2")]
    ctx = {"canonicals": docs, "simulate_db": False, "simulate_api": False}
    node_classify_and_risk_assess(ctx)   # 不抛
    assert mock_llm.call_count == 1, "只有 fresh d1 重新分类；crash-resume d2 复用存储分类"
    assert docs[1]["classification_status"] == "FROZEN_MAINTENANCE"
    assert docs[1]["category_l2"] == "inspection_sop"


@patch(LLM_PATH)
@patch(DB_PATH)
def test_crash_resume_killswitch_reverts_to_block(mock_db, mock_llm, monkeypatch):
    """kill switch：RAG_CRASH_RESUME_AUTOFREEZE=false → crash-resume 也回退旧整批 raise 行为。"""
    monkeypatch.setenv("RAG_CRASH_RESUME_AUTOFREEZE", "false")
    factory, _ = _crash_factory({("d1", 1)}, {("d1", 1): ("FAILED", 1, "sop", "safety_sop")})
    mock_db.side_effect = factory
    ctx = {"canonicals": [_doc("d1")], "simulate_db": False, "simulate_api": False}
    with pytest.raises(RuntimeError, match="unfrozen re-chunk blocked"):
        node_classify_and_risk_assess(ctx)
    assert mock_llm.call_count == 0

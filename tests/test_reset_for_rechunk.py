"""Regression: a re-chunk reset must leave document_version.index_status in a state the stage-3 lock
can preempt. The 2026-06-15 canary bug set it to 'NOT_STARTED' -> stage-3 lock skipped every doc."""
import inspect
import re

from opensearch_pipeline.reindex_states import (
    STAGE3_CLAIMABLE_INDEX_STATUS,
    rechunk_reset_state,
)


def test_rechunk_reset_index_status_is_not_indexed():
    st = rechunk_reset_state()
    # the fix: index_status must be NOT_INDEXED, never the content/chunk-lifecycle 'NOT_STARTED'
    assert st["index_status"] == "NOT_INDEXED"
    assert st["index_status"] != "NOT_STARTED", "the exact canary bug value must never return"


def test_rechunk_reset_is_stage3_claimable():
    st = rechunk_reset_state()
    assert st["index_status"] in STAGE3_CLAIMABLE_INDEX_STATUS
    # 'NOT_STARTED' must NOT be claimable (that is precisely why the bug skipped stage 3)
    assert "NOT_STARTED" not in STAGE3_CLAIMABLE_INDEX_STATUS


def test_rechunk_reset_content_and_chunk_are_not_started():
    st = rechunk_reset_state()
    # content/chunk lifecycle initial value IS 'NOT_STARTED' (so stage-2 re-claims it)
    assert st["content_process_status"] == "NOT_STARTED"
    assert st["chunk_status"] == "NOT_STARTED"
    assert st["retry_count"] == 0


def test_constant_matches_live_stage3_lock_predicate():
    """Coupling guard: the lock node's preemption predicates must be RENDERED from
    STAGE3_CLAIMABLE_INDEX_STATUS (single source), so the reset target can't drift away
    from what stage 3 actually claims. (词表重构后不再 regex 抽字面量——源码里就不该有字面量。)"""
    from opensearch_pipeline import pipeline_nodes

    src = inspect.getsource(pipeline_nodes.node_acquire_index_lock)
    # both preemption UPDATEs (batch claim + per-doc retry) interpolate the constant
    assert src.count("index_status IN ({sql_in_list(STAGE3_CLAIMABLE_INDEX_STATUS)})") == 2, (
        "node_acquire_index_lock must render its IN (...) preemption predicates from "
        "STAGE3_CLAIMABLE_INDEX_STATUS via sql_in_list — do not hand-write the status list"
    )
    # and no raw literal IN-list may drift back in
    assert not re.search(r"index_status\s+IN\s*\(\s*'", src), (
        "raw index_status IN ('...') literal found in node_acquire_index_lock — "
        "use STAGE3_CLAIMABLE_INDEX_STATUS instead"
    )
    assert rechunk_reset_state()["index_status"] in STAGE3_CLAIMABLE_INDEX_STATUS

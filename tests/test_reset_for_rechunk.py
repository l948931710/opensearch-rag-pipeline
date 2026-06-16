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
    """Coupling guard: STAGE3_CLAIMABLE_INDEX_STATUS must equal the statuses the lock node preempts on,
    so the reset target can't drift away from what stage 3 actually claims."""
    from opensearch_pipeline import pipeline_nodes

    src = inspect.getsource(pipeline_nodes.node_acquire_index_lock)
    # the primary preemption UPDATE: index_status IN ('NOT_INDEXED', 'FAILED')
    m = re.search(r"index_status\s+IN\s*\(([^)]*)\)", src)
    assert m, "could not find the index_status IN (...) preemption predicate in node_acquire_index_lock"
    claimed = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
    assert claimed == set(STAGE3_CLAIMABLE_INDEX_STATUS), (
        f"lock node claims {claimed} but STAGE3_CLAIMABLE_INDEX_STATUS={set(STAGE3_CLAIMABLE_INDEX_STATUS)} "
        "— keep them in sync"
    )
    assert rechunk_reset_state()["index_status"] in claimed

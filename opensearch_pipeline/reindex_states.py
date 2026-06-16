"""Canonical document_version state transitions for re-chunk / re-index resets.

Single source of truth for the laptop push-then-purge / DataWorks re-chunk flow, so an ad-hoc reset
can never again leave a row in a state the stage-3 lock can't claim.

THE BUG THIS PREVENTS (2026-06-15 canary): a reset set ``document_version.index_status='NOT_STARTED'``.
Stage-2's chunk loader keys on ``chunk_meta.index_status='NOT_INDEXED'`` (so chunking ran), but the
stage-3 lock node (``pipeline_nodes.node_acquire_index_lock``) only preempts rows whose
``document_version.index_status IN ('NOT_INDEXED','FAILED')`` (+ a SUCCESS-relock fallback). With the
column left at 'NOT_STARTED' the lock matched nothing → stage 3 skipped every doc (0 work, 0 writes).

``index_status`` for ``document_version`` is an indexing-lifecycle column whose initial/ready value is
**NOT_INDEXED**, never 'NOT_STARTED' ('NOT_STARTED' is the content/chunk lifecycle's initial value).
"""

import hashlib


def docset_hash(doc_ids) -> str:
    """Stable 12-char hash binding an unfrozen-rechunk override token to a specific doc-set.

    The ``RAG_ALLOW_UNFROZEN_RECHUNK`` token is ``<op>:<YYYY-MM-DD>:<docset_hash>``; the hash pins the
    authorization to exactly the doc_ids it was minted for, so a same-day token cannot be silently
    reused for a *different* re-chunk batch (the failure this guards: ack minted for doc-set A
    accidentally authorizing an unfrozen re-chunk of doc-set B later the same day).

    Order-independent + deduped (sorted set), so a re-ordered/duplicated doc list yields the same hash.
    ``reset_for_rechunk.py`` prints this for the docs it resets; the stage-2 guard
    (``pipeline_nodes._unfrozen_rechunk_acked``) recomputes it for the flagged doc-set and requires an
    exact match. Single source of truth so the script and the node can never drift.
    """
    ids = sorted({str(d) for d in doc_ids})
    return hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()[:12]

# The exact set node_acquire_index_lock (pipeline_nodes.py) preempts on its primary UPDATE.
# Keep in sync with that node — tests/test_reset_for_rechunk.py asserts the coupling.
STAGE3_CLAIMABLE_INDEX_STATUS = ("NOT_INDEXED", "FAILED")


def rechunk_reset_state() -> dict:
    """The document_version field values a re-chunk (stage 2 -> 3) reset MUST write.

    - content_process_status / chunk_status = 'NOT_STARTED'  -> stage-2 claim predicate selects it
    - index_status = 'NOT_INDEXED'                            -> stage-3 lock can preempt it (the fix)
    - retry_count = 0                                          -> fresh attempt budget

    Canonical KEEP-CANONICAL reset: callers must NOT clear canonical_json_key (extraction was fine;
    only chunking changed) and must scope to version_no = current_version_no.
    """
    return {
        "content_process_status": "NOT_STARTED",
        "chunk_status": "NOT_STARTED",
        "index_status": "NOT_INDEXED",
        "retry_count": 0,
    }

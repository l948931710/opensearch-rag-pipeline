# -*- coding: utf-8 -*-
"""pipeline_run.py — per-run provenance header writer (Phase-1 L6prov).

Writes one pipeline_run row per orchestrator stage run: RUNNING at start → SUCCESS/FAILED at end,
recording git_commit + extractor/chunker/detector + embedding/llm model versions. It is the run
HEADER that kb_audit_log (L5) per-doc events join to (trace_id '<git_commit>:<bizdate>'), and the
lineage_audit (dim7) capstone: 'which run, with which code/model revision, did stage N, and how
did it end'.

Fail-open + no-op in simulate (mirrors audit_log / qa_logger): a provenance-write failure must NEVER
abort the run, and it uses its own short-lived connection so it can't poison any other transaction.
"""
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def make_run_id(provenance: dict) -> str:
    """Unique run id: s<stage>_<bizdate>_<git_commit>_<uuid6>."""
    commit = provenance.get("git_commit") or "nocommit"
    return f"s{provenance.get('stage')}_{provenance.get('bizdate')}_{commit}_{uuid.uuid4().hex[:6]}"


def run_start(provenance: dict, *, simulate: bool = False) -> Optional[str]:
    """INSERT a pipeline_run row (status RUNNING). Returns run_id, or None in simulate / on failure."""
    if simulate:
        return None
    run_id = make_run_id(provenance)
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_run (
                        run_id, stage, bizdate, git_commit, extractor_version, chunker_version,
                        detector_version, embedding_model, embedding_model_version, llm_model, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'RUNNING')
                    """,
                    (run_id, provenance.get("stage"), provenance.get("bizdate"),
                     provenance.get("git_commit"), provenance.get("extractor_version"),
                     provenance.get("chunker_version"), provenance.get("detector_version"),
                     provenance.get("embedding_model"), provenance.get("embedding_model_version"),
                     provenance.get("llm_model")),
                )
            conn.commit()
        finally:
            conn.close()
        return run_id
    except Exception as e:
        logger.warning("pipeline_run start failed (non-fatal): %s", e)
        return None


# OBS-3: ctx counter key → pipeline_run column. The DAG nodes already write these into ctx;
# extract_run_metrics pulls the subset, accumulate_metrics sums them across drain batches.
_METRIC_CTX_MAP = {
    "embedded_chunks": "embedded_chunks",
    "embedding_failed_chunks": "embedding_failed_chunks",
    "chunk_meta_written": "chunks_written",
    "deactivated_chunks": "chunks_deactivated",
    "published_count": "docs_processed",
}


def _as_count(v) -> Optional[int]:
    """Coerce a ctx counter to an int count. Lists/sets → len; ints → value; else None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, (list, tuple, set, dict)):
        return len(v)
    return None


def extract_run_metrics(ctx: dict) -> dict:
    """OBS-3: pull the pipeline_run metric columns out of a DAG result ctx. Fail-open → {} on junk.
    `docs_failed` derives from ctx['failed_doc_versions'] (a set/list of (doc_id, ver))."""
    if not isinstance(ctx, dict):
        return {}
    out = {}
    for ctx_key, col in _METRIC_CTX_MAP.items():
        n = _as_count(ctx.get(ctx_key))
        if n is not None:
            out[col] = n
    failed = _as_count(ctx.get("failed_doc_versions"))
    if failed is not None:
        out["docs_failed"] = failed
    return out


def accumulate_metrics(acc: dict, new: dict) -> dict:
    """Sum metric dicts across drain-loop batches (None-safe)."""
    if not new:
        return acc
    for k, v in new.items():
        if v is None:
            continue
        acc[k] = (acc.get(k) or 0) + v
    return acc


def run_finish(run_id: Optional[str], status: str, *, docs_processed: Optional[int] = None,
               chunks_written: Optional[int] = None, error_message: Optional[str] = None,
               metrics: Optional[dict] = None, simulate: bool = False) -> None:
    """UPDATE the pipeline_run row to a terminal status + finished_at. No-op if no run_id; fail-open.

    OBS-3: `metrics` (from extract_run_metrics/accumulate_metrics) backfills the per-run counter
    columns. Explicit docs_processed/chunks_written args win over the same keys in metrics."""
    if simulate or not run_id:
        return
    m = dict(metrics or {})
    if docs_processed is not None:
        m["docs_processed"] = docs_processed
    if chunks_written is not None:
        m["chunks_written"] = chunks_written
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pipeline_run
                       SET status=%s, docs_processed=%s, chunks_written=%s,
                           embedded_chunks=%s, embedding_failed_chunks=%s,
                           chunks_deactivated=%s, docs_failed=%s,
                           error_message=%s, finished_at=NOW()
                     WHERE run_id=%s
                    """,
                    (status, m.get("docs_processed"), m.get("chunks_written"),
                     m.get("embedded_chunks"), m.get("embedding_failed_chunks"),
                     m.get("chunks_deactivated"), m.get("docs_failed"),
                     (error_message[:2000] if error_message else None), run_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("pipeline_run finish failed (non-fatal): %s", e)

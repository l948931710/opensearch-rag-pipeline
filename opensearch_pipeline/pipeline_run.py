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


def run_finish(run_id: Optional[str], status: str, *, docs_processed: Optional[int] = None,
               chunks_written: Optional[int] = None, error_message: Optional[str] = None,
               simulate: bool = False) -> None:
    """UPDATE the pipeline_run row to a terminal status + finished_at. No-op if no run_id; fail-open."""
    if simulate or not run_id:
        return
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pipeline_run
                       SET status=%s, docs_processed=%s, chunks_written=%s,
                           error_message=%s, finished_at=NOW()
                     WHERE run_id=%s
                    """,
                    (status, docs_processed, chunks_written,
                     (error_message[:2000] if error_message else None), run_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("pipeline_run finish failed (non-fatal): %s", e)

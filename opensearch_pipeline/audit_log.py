# -*- coding: utf-8 -*-
"""audit_log.py — append-only kb_audit_log writer (Phase-1 L5).

Revives kb_audit_log (defined in schema/001 but previously ZERO writers): an append-only record of
doc/version lifecycle transitions (REGISTER / CHUNK / INDEX / DEACTIVATE / …) for forensic lineage —
who/what/when, and which run produced or retired a version. trace_id is the run fingerprint from
ctx['run_provenance'] (L1), so an audit row joins back to the producing code/model revision.

Mirrors the qa_logger pattern deliberately: opens its OWN short-lived connection, commits, and
swallows ALL exceptions (fail-open). An audit failure must NEVER abort ingestion (CLAUDE.md
graceful-degradation invariant) and must not poison the caller's transaction — hence a separate
connection rather than the caller's cursor. No-op in simulate. Honors RAG_READONLY (the
GuardedDBConnection blocks the INSERT under PROD-RO, which is then swallowed fail-open).
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def audit_trace_id(ctx: Optional[dict]) -> Optional[str]:
    """Run-scoped trace_id from ctx['run_provenance'] (L1): '<git_commit>:<bizdate>'.
    Falls back to bizdate, then None. Never raises."""
    try:
        prov = (ctx or {}).get("run_provenance") or {}
        commit = prov.get("git_commit")
        bizdate = prov.get("bizdate") or (ctx or {}).get("bizdate")
        if commit:
            return f"{commit}:{bizdate}" if bizdate else str(commit)
        return str(bizdate) if bizdate else None
    except Exception:
        return None


def write_audit(*, doc_id: Optional[str], version_no: Optional[int],
                action_type: str, action_result: str = "SUCCESS",
                trace_id: Optional[str] = None, message: Optional[str] = None,
                operator_type: str = "pipeline", operator_id: Optional[str] = None,
                oss_key: Optional[str] = None, simulate: bool = False) -> None:
    """Append one kb_audit_log row. Fail-open, no-op in simulate, never raises."""
    if simulate:
        return
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO kb_audit_log (
                        trace_id, doc_id, version_no, action_type, action_result,
                        operator_type, operator_id, oss_key, message
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (trace_id, doc_id, version_no, action_type, action_result,
                     operator_type, operator_id, oss_key, message),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # append-only audit is auxiliary — never break ingest (mirrors qa_logger swallow-on-error)
        logger.warning("kb_audit_log write failed (non-fatal): action=%s doc=%s v=%s err=%s",
                       action_type, doc_id, version_no, e)

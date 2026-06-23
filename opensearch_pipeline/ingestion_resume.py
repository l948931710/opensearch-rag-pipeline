# -*- coding: utf-8 -*-
"""ingestion_resume.py — READ-ONLY resume / orphan-crash recovery report.

Reports, per stage, what a plain re-run (run_stage_drained) would pick up from the CURRENT RDS
state: pending rows (the authoritative _count_pending_rows predicate — kept in lockstep with the
claim SQL), in-flight locks, and stale (>2h) locks the drain reclaims; for stage 3 also a
read-only estimate of physical HA3 orphan PKs.

STRICTLY READ-ONLY: pure SELECTs + reconcile_ha3_orphan_pks(dry_run=True) (documented read-only,
never-raises). It deliberately does NOT call any reconcile WRITE path (e.g.
reconcile_stranded_versions, which heals). It recovers from current RDS state — it does NOT
resume a specific historical pipeline_run (there is no reliable run_id↔chunk link).

The actual resume is just re-running the stage: `dataworks_orchestrator --stage N --resume` prints
this report then calls the unchanged run_stage_drained (idempotent re-entry through the unchanged
DAG-3 — no new version, no reset of completed state, never bypasses the 04b parity node).
"""
import argparse
import json

_STALE = "NOW() - INTERVAL 2 HOUR"


def _scalar(cur, sql: str) -> int:
    cur.execute(sql)
    return int(cur.fetchone()[0])


def build_resume_report(stage: int) -> dict:
    """Read-only per-stage recovery snapshot. No writes."""
    from opensearch_pipeline.config import get_config
    if get_config().simulate_db:
        return {"stage": stage, "simulate": True, "note": "simulate mode — no DB; report is a no-op"}

    from opensearch_pipeline.pipeline_nodes import _get_db_conn
    from opensearch_pipeline.dataworks_orchestrator import _count_pending_rows

    rep = {"stage": stage, "simulate": False,
           "recovers_from": "current RDS state (NOT a specific pipeline_run)",
           "pending": _count_pending_rows(stage)}
    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        with conn.cursor() as cur:
            if stage == 1:
                rep["in_flight_loading"] = _scalar(
                    cur, "SELECT COUNT(*) FROM document_version "
                         "WHERE content_process_status='LOADING' AND status='active'")
                rep["stale_note"] = "stage-1 LOADING has no age guard — inspect manually if stuck"
            elif stage == 2:
                rep["in_flight"] = _scalar(
                    cur, "SELECT COUNT(*) FROM document_version "
                         "WHERE content_process_status IN ('LOADING','PROCESSING') AND status='active'")
                rep["stale_locks_2h"] = _scalar(
                    cur, "SELECT COUNT(*) FROM document_version "
                         "WHERE content_process_status IN ('LOADING','PROCESSING') AND status='active' "
                         f"AND updated_at < {_STALE}")
            elif stage == 3:
                rep["in_flight"] = _scalar(
                    cur, "SELECT COUNT(*) FROM document_version WHERE index_status='PROCESSING'")
                rep["stale_locks_2h"] = _scalar(
                    cur, "SELECT COUNT(*) FROM document_version "
                         f"WHERE index_status='PROCESSING' AND updated_at < {_STALE}")
    finally:
        if conn:
            conn.close()

    if stage == 3:
        # read-only HA3 physical-orphan estimate (dry_run never writes / never raises); fail-open
        try:
            from opensearch_pipeline.ha3_reconcile import reconcile_ha3_orphan_pks
            rep["ha3_orphan_pks_estimate"] = reconcile_ha3_orphan_pks(dry_run=True).get("stale", 0)
        except Exception as e:
            rep["ha3_orphan_pks_estimate"] = f"unavailable: {e}"
    return rep


def format_report(rep: dict) -> str:
    lines = [f"[RESUME] stage {rep.get('stage')} — read-only recovery report"]
    if rep.get("simulate"):
        lines.append("  simulate mode — no DB; nothing to report")
        return "\n".join(lines)
    lines.append(f"  recovers_from: {rep.get('recovers_from')}")
    for k in ("pending", "in_flight", "in_flight_loading", "stale_locks_2h",
              "ha3_orphan_pks_estimate", "stale_note"):
        if k in rep:
            lines.append(f"  {k}: {rep[k]}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Read-only ingestion resume/recovery report")
    ap.add_argument("--stage", type=int, required=True, choices=[1, 2, 3])
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()
    rep = build_resume_report(args.stage)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if args.json else format_report(rep))


if __name__ == "__main__":
    main()

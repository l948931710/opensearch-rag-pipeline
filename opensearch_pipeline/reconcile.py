# -*- coding: utf-8 -*-
"""reconcile.py — Phase-3 CS3: read-only RDS↔HA3 parity reconciler.

The ingestion pipeline is laptop/DataWorks-driven and the cross-store reconcilers run only when
invoked — there is no daily self-heal. This module is the standing parity probe that covers the
**silent-recall-loss direction no other check covers**: an RDS chunk that is active+INDEXED but
absent from HA3 (its vector vanished, yet the doc is "served"). It also surfaces the inverse
(HA3 rows with no RDS-active backing — purge lag / zombies) and the worst case (a doc with RDS-active
chunks but ZERO HA3 rows = fully vanished from search).

Design contract (mirrors qa_logger / audit_log / alerting):
  - **Read-only.** RDS access goes through prod_access.get_prod_readonly_conn (fuling_ro). HA3 is
    queried with include_vector=False, no writes. This module NEVER deletes or deactivates.
  - **Deterministic enumeration.** HA3 is scanned by PK range (`id>=lo AND id<hi`, ≤bucket per call)
    — a zero-vector ANN top_k under-enumerates HNSW (the scratch v1 incident); range filter is
    complete per bucket. A bucket that returns ≥ its cap is flagged `truncated` → report.complete=False.
  - **Fail-open.** run_parity_check never raises to its caller; on any error it returns a report with
    ok=False + error set, and (if alert=True) fires one OBS-4 ops alert. Simulate → skipped no-op.

`compute_parity` is a pure function (no DB/HA3) and is the unit-tested core.

CLI:  python -m opensearch_pipeline.reconcile [--alert] [--json] [--hi N]
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_BUCKET = 500
_HI_HEADROOM = 1000  # scan past max(rds.id) so freshly-pushed-but-unrecorded rows still surface


def compute_parity(rds_rows: List[Dict[str, Any]],
                   ha3_rows: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """Pure parity diff. No I/O.

    Args:
        rds_rows: chunk_meta rows; each needs id, chunk_id, doc_id, version_no, is_active,
                  index_status, chunk_type.
        ha3_rows: pk(int) -> {chunk_id, doc_id, chunk_type, version_no} from the HA3 scan.

    Returns a report dict. `ok` is True iff there is NO recall-loss drift — i.e. no
    `rds_active_missing` AND no `vanished_docs`. HA3 stale rows alone do NOT fail `ok` (purge lag is
    expected and harmless to recall); they are reported for cleanup tracking.
    """
    rds_by_id = {int(r["id"]): r for r in rds_rows}
    active = [r for r in rds_rows if r.get("is_active") == 1]
    active_ids = {int(r["id"]) for r in active}
    active_indexed = {int(r["id"]): r for r in active if r.get("index_status") == "INDEXED"}
    active_chunkids = {r["chunk_id"] for r in active}
    active_by_doc: Dict[str, set] = defaultdict(set)
    for r in active:
        active_by_doc[r["doc_id"]].add(int(r["id"]))

    seen_pks = set(ha3_rows)

    # ── DIRECTION 1 (recall loss): RDS active+INDEXED not present in HA3 ──
    rds_active_missing = [
        {"id": pk, "chunk_id": r["chunk_id"], "doc_id": r["doc_id"],
         "version_no": r.get("version_no"), "chunk_type": r.get("chunk_type")}
        for pk, r in active_indexed.items() if pk not in seen_pks
    ]

    # ── DIRECTION 2 (stale / zombie): HA3 PK with no RDS-active backing ──
    ha3_stale = []
    ha3_kept_by_doc: Counter = Counter()
    for pk, h in ha3_rows.items():
        if pk in active_ids:
            ha3_kept_by_doc[h.get("doc_id")] += 1
            continue
        cid = h.get("chunk_id", "")
        subtype = ("dup" if cid in active_chunkids
                   else "rds_inactive" if pk in rds_by_id
                   else "orphan_chunkid")
        ha3_stale.append({"id": pk, "chunk_id": cid, "doc_id": h.get("doc_id"),
                          "chunk_type": h.get("chunk_type"), "subtype": subtype})

    # ── WORST CASE: doc has RDS-active chunks but ZERO HA3 kept rows (fully vanished) ──
    vanished_docs = [
        {"doc_id": d, "rds_active": len(ids), "ha3_kept": ha3_kept_by_doc.get(d, 0)}
        for d, ids in active_by_doc.items()
        if ids and ha3_kept_by_doc.get(d, 0) == 0
    ]

    # ── INFORMATIONAL: HA3 doc_ids with no RDS-active backing at all ──
    ha3_docs = {h.get("doc_id") for h in ha3_rows.values()}
    orphan_docs = sorted(ha3_docs - set(active_by_doc))

    ok = not rds_active_missing and not vanished_docs
    return {
        "ok": ok,
        "counts": {
            "rds_rows": len(rds_rows),
            "rds_active": len(active_ids),
            "rds_active_indexed": len(active_indexed),
            "ha3_pks": len(ha3_rows),
            "rds_active_missing": len(rds_active_missing),
            "ha3_stale": len(ha3_stale),
            "vanished_docs": len(vanished_docs),
            "orphan_docs": len(orphan_docs),
        },
        "stale_subtypes": dict(Counter(s["subtype"] for s in ha3_stale)),
        "rds_active_missing": rds_active_missing,
        "vanished_docs": vanished_docs,
        "ha3_stale_sample": ha3_stale[:50],
        "orphan_docs_sample": orphan_docs[:50],
    }


def _scan_ha3_pks(cli, table_name: str, hi: int, *,
                  lo: int = 0, bucket: int = _DEFAULT_BUCKET) -> Dict[str, Any]:
    """Deterministic HA3 PK-range enumeration. Returns {"rows": {pk: {...}}, "truncated": [lo,...]}.

    A bucket whose result count reaches its cap is flagged truncated (some ids may be unseen) so the
    caller can mark the report incomplete rather than reporting false 'missing' rows.
    """
    from alibabacloud_ha3engine_vector.models import QueryRequest
    from opensearch_pipeline.retriever import _DEFAULT_OUTPUT_FIELDS, _parse_ha3_response

    rows: Dict[int, Dict[str, Any]] = {}
    truncated: List[int] = []
    cap = bucket + 100
    for start in range(lo, hi, bucket):
        req = QueryRequest(table_name=table_name, vector=[0.0] * 1024, top_k=cap,
                           include_vector=False, output_fields=_DEFAULT_OUTPUT_FIELDS,
                           filter=f"id>={start} AND id<{start + bucket}")
        parsed = _parse_ha3_response(cli.query(req))
        if len(parsed) >= cap:
            truncated.append(start)
        for r in parsed:
            try:
                pk = int(r.get("id"))
            except (TypeError, ValueError):
                continue
            rows[pk] = {"chunk_id": r.get("chunk_id", ""), "doc_id": r.get("doc_id", ""),
                        "chunk_type": r.get("chunk_type", ""), "version_no": r.get("version_no")}
    return {"rows": rows, "truncated": truncated}


def run_parity_check(*, alert: bool = False, hi: Optional[int] = None,
                     bucket: int = _DEFAULT_BUCKET) -> Dict[str, Any]:
    """Top-level CS3 reconcile: read RDS (read-only) + scan HA3 + diff. Fail-open, simulate-safe.

    Returns the compute_parity report enriched with `complete` (False if any HA3 bucket truncated)
    and, on failure, `error`. Never raises. When alert=True and drift (recall-loss) is detected — or
    the run errors — fires a single OBS-4 ops alert (itself fail-open / config-gated).
    """
    from opensearch_pipeline.config import get_config
    cfg = get_config()

    if cfg.simulate or cfg.simulate_db or cfg.simulate_opensearch:
        logger.info("reconcile: simulate mode → skipped no-op")
        return {"ok": True, "skipped": "simulate", "complete": True, "counts": {}}

    try:
        from opensearch_pipeline.prod_access import get_prod_readonly_conn
        from opensearch_pipeline.retriever import _get_ha3_client

        conn = get_prod_readonly_conn()
        try:
            with conn.cursor() as c:
                c.execute("""SELECT id, chunk_id, doc_id, version_no, is_active,
                                    index_status, chunk_type
                             FROM fuling_knowledge.chunk_meta""")
                rds_rows = list(c.fetchall())
        finally:
            conn.close()

        scan_hi = hi if hi is not None else (
            (max((int(r["id"]) for r in rds_rows), default=0)) + _HI_HEADROOM)
        cli = _get_ha3_client()
        scan = _scan_ha3_pks(cli, cfg.alibaba_vector.table_name, scan_hi, bucket=bucket)

        report = compute_parity(rds_rows, scan["rows"])
        report["complete"] = not scan["truncated"]
        report["truncated_buckets"] = scan["truncated"]
        report["scan_hi"] = scan_hi
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.exception("reconcile: parity check failed")
        report = {"ok": False, "complete": False, "error": f"{type(e).__name__}: {e}", "counts": {}}

    if alert and (not report.get("ok") or report.get("error")):
        _alert_on_drift(report)
    return report


def _alert_on_drift(report: Dict[str, Any]) -> None:
    """Fire one OBS-4 ops alert summarizing recall-loss drift (fail-open)."""
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        c = report.get("counts", {})
        if report.get("error"):
            text = f"parity check errored: {report['error']}"
        else:
            text = (f"RDS-active missing from HA3: **{c.get('rds_active_missing', 0)}** chunks; "
                    f"fully-vanished docs: **{c.get('vanished_docs', 0)}**; "
                    f"HA3 stale: {c.get('ha3_stale', 0)}; "
                    f"complete={report.get('complete')}")
        send_ops_alert("RDS↔HA3 parity drift", text, severity="critical",
                       dedup_key="reconcile:rds-ha3-parity")
    except Exception:  # noqa: BLE001
        logger.warning("reconcile: ops-alert dispatch failed (non-fatal)", exc_info=True)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI. Exit 0 = parity OK (or simulate-skipped); 2 = drift; 3 = error/incomplete."""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="CS3 read-only RDS↔HA3 parity reconciler")
    ap.add_argument("--alert", action="store_true", help="fire an OBS-4 ops alert on drift/error")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--hi", type=int, default=None, help="override HA3 PK scan upper bound")
    args = ap.parse_args(argv)

    report = run_parity_check(alert=args.alert, hi=args.hi)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        if report.get("skipped"):
            print(f"[reconcile] skipped ({report['skipped']})")
            return 0
        c = report.get("counts", {})
        print(f"[reconcile] ok={report.get('ok')} complete={report.get('complete')}")
        print(f"  RDS rows={c.get('rds_rows')} active={c.get('rds_active')} "
              f"active_indexed={c.get('rds_active_indexed')} | HA3 pks={c.get('ha3_pks')}")
        print(f"  ⚠️ RDS-active MISSING from HA3 = {c.get('rds_active_missing')} (recall loss)")
        print(f"  ⚠️ fully-VANISHED docs = {c.get('vanished_docs')}")
        print(f"  stale HA3 rows = {c.get('ha3_stale')} {report.get('stale_subtypes', {})}")
        print(f"  orphan HA3 docs = {c.get('orphan_docs')}")
        if report.get("error"):
            print(f"  ERROR: {report['error']}")
        for m in report.get("rds_active_missing", [])[:10]:
            print(f"    MISSING id={m['id']} {m['chunk_id']} type={m['chunk_type']}")
        for v in report.get("vanished_docs", [])[:10]:
            print(f"    VANISHED {v}")

    if report.get("error") or report.get("complete") is False:
        return 3
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    import sys
    sys.exit(main())

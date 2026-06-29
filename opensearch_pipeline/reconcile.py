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


def _kb_db() -> str:
    """知识库库名（document_meta/version/chunk_meta 所在库）；经 RAG_RDS_DATABASE 配置（STAGING=_stg）。
    惰性读 config（不在 import 期）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.database

_DEFAULT_BUCKET = 500
_HI_HEADROOM = 1000  # scan past max(rds.id) so freshly-pushed-but-unrecorded rows still surface
_OSS_IMAGE_PREFIX = "processing/assets/"  # where active-chunk image_refs[].oss_key live
_RDS_COLS = ("id", "chunk_id", "doc_id", "version_no", "is_active", "index_status", "chunk_type")


# ── cred portability: run from the laptop (prod_access .env files → dedicated read-only fuling_ro)
# OR inside a DataWorks pod / any host with injected RAG_ env vars (no .env files). Prefer the
# read-only path; fall back to the config/env pool when prod_access finds no .env file. On
# RAG_ENV=prod_ro the config pool is itself SESSION READ ONLY, so read-only safety holds on both
# paths; the reconcilers only ever SELECT / HA3-query / OSS-list. ────────────────────────────────

def _rds_conn():
    from opensearch_pipeline.prod_access import ProdAccessError, get_prod_readonly_conn
    try:
        return get_prod_readonly_conn()
    except ProdAccessError:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        return _get_db_conn(select_db=False)


def _oss_bucket():
    from opensearch_pipeline.prod_access import ProdAccessError, get_prod_oss_bucket
    try:
        return get_prod_oss_bucket()
    except ProdAccessError:
        import oss2
        from opensearch_pipeline.config import get_config
        from opensearch_pipeline.oss_url import _ensure_public_endpoint
        from opensearch_pipeline.prod_access import _ReadOnlyBucket
        oc = get_config().oss
        auth = oss2.Auth(oc.access_key_id, oc.access_key_secret)
        return _ReadOnlyBucket(oss2.Bucket(auth, _ensure_public_endpoint(oc.endpoint), oc.bucket_name))


def _as_dict_rows(raw, cols):
    """Normalize cursor rows to dicts regardless of cursor class (prod_access=DictCursor, pool=tuple)."""
    return [r if isinstance(r, dict) else dict(zip(cols, r)) for r in raw]


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
        from opensearch_pipeline.retriever import _get_ha3_client

        conn = _rds_conn()
        try:
            with conn.cursor() as c:
                c.execute(f"""SELECT id, chunk_id, doc_id, version_no, is_active,
                                    index_status, chunk_type
                             FROM {_kb_db()}.chunk_meta""")
                rds_rows = _as_dict_rows(c.fetchall(), _RDS_COLS)
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


# ──────────────────────────────────────────────────────────────────────────────
# CS4 — OSS↔RDS image-object parity (the third store)
# ──────────────────────────────────────────────────────────────────────────────

def collect_referenced_image_keys(rds_rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """Parse active chunks' extra_json image_refs[].oss_key → {oss_key: sample_chunk_id}.

    Only is_active=1 rows count — an inactive chunk's image being absent is not a serving defect.
    Fail-open per row: a malformed extra_json is skipped, not fatal.
    """
    import json
    out: Dict[str, str] = {}
    for r in rds_rows:
        if r.get("is_active") != 1:
            continue
        raw = r.get("extra_json")
        if not raw or "oss_key" not in raw:
            continue
        try:
            ej = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            continue
        refs = ej.get("image_refs") if isinstance(ej, dict) else None
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if isinstance(ref, dict):
                k = ref.get("oss_key")
                if k and k not in out:
                    out[k] = r.get("chunk_id", "")
    return out


def compute_oss_parity(referenced: Dict[str, str], present: set, *,
                       verify_fn=None) -> Dict[str, Any]:
    """OSS parity diff. Pure when verify_fn is None.

    Args:
        referenced: {oss_key: sample_chunk_id} that active chunks point at (ANY prefix).
        present: set of oss_keys actually listed in OSS under the image prefix.
        verify_fn: optional callable(key)->bool returning True iff the object EXISTS. CRITICAL:
            `present` only covers the listed prefix, so a referenced key under a DIFFERENT prefix
            (e.g. raw/marketing/*.jpg vs processing/assets/*) would be a FALSE 'missing' on the raw
            set-diff. When verify_fn is given, each set-diff candidate is HEAD-checked and kept only
            if it truly does NOT exist. Without verify_fn this returns the raw candidates (unit tests).

    `ok` is True iff no referenced key is truly missing from OSS (broken-image / serving defect).
    Orphan OSS objects (present but unreferenced) are storage bloat — reported, do NOT fail ok.
    """
    ref_keys = set(referenced)
    candidates = sorted(ref_keys - present)
    if verify_fn is not None:
        missing = [k for k in candidates if not verify_fn(k)]
    else:
        missing = candidates
    orphan = sorted(present - ref_keys)
    return {
        "ok": not missing,
        "counts": {
            "referenced": len(ref_keys),
            "present": len(present),
            "candidates_offprefix": len(candidates) - len(missing) if verify_fn else 0,
            "missing": len(missing),
            "orphan": len(orphan),
        },
        "missing": [{"oss_key": k, "chunk_id": referenced.get(k, "")} for k in missing[:50]],
        "orphan_sample": orphan[:50],
    }


def _list_oss_keys(bucket, prefix: str = _OSS_IMAGE_PREFIX) -> set:
    """Paginated read-only ListObjects under prefix → set of object keys."""
    import oss2
    keys = set()
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        keys.add(obj.key)
    return keys


def run_oss_parity_check(*, alert: bool = False,
                         prefix: str = _OSS_IMAGE_PREFIX) -> Dict[str, Any]:
    """CS4: read active-chunk image keys (RDS, read-only) + list OSS image objects + diff.
    Read-only (prod_access read-only bucket blocks all writes), simulate-safe, fail-open.
    """
    from opensearch_pipeline.config import get_config
    cfg = get_config()

    if cfg.simulate or cfg.simulate_db:
        logger.info("reconcile(oss): simulate mode → skipped no-op")
        return {"ok": True, "skipped": "simulate", "complete": True, "counts": {}}

    try:
        conn = _rds_conn()
        try:
            with conn.cursor() as c:
                c.execute(f"""SELECT chunk_id, is_active, extra_json
                             FROM {_kb_db()}.chunk_meta
                             WHERE is_active=1 AND extra_json LIKE %s""", ("%oss_key%",))
                rds_rows = _as_dict_rows(c.fetchall(), ("chunk_id", "is_active", "extra_json"))
        finally:
            conn.close()

        referenced = collect_referenced_image_keys(rds_rows)
        bucket = _oss_bucket()
        present = _list_oss_keys(bucket, prefix)

        # HEAD-verify candidates so a referenced key under a different prefix (e.g. raw/*) is not a
        # false 'missing'; only objects that truly don't exist count. object_exists is read-only.
        def _exists(k):
            try:
                return bool(bucket.object_exists(k))
            except Exception:  # noqa: BLE001 — treat HEAD error conservatively as "exists" (no false alarm)
                return True

        report = compute_oss_parity(referenced, present, verify_fn=_exists)
        report["complete"] = True
        report["prefix"] = prefix
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        logger.exception("reconcile(oss): parity check failed")
        report = {"ok": False, "complete": False, "error": f"{type(e).__name__}: {e}", "counts": {}}

    if alert and (not report.get("ok") or report.get("error")):
        _alert_on_oss_drift(report)
    return report


def _alert_on_oss_drift(report: Dict[str, Any]) -> None:
    """Fire one OBS-4 ops alert summarizing OSS image-object drift (fail-open)."""
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        c = report.get("counts", {})
        if report.get("error"):
            text = f"OSS parity check errored: {report['error']}"
        else:
            text = (f"active-chunk image oss_keys missing from OSS: **{c.get('missing', 0)}** "
                    f"(broken images); orphan OSS objects: {c.get('orphan', 0)}")
        send_ops_alert("OSS↔RDS image parity drift", text, severity="critical",
                       dedup_key="reconcile:oss-rds-parity")
    except Exception:  # noqa: BLE001
        logger.warning("reconcile(oss): ops-alert dispatch failed (non-fatal)", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────────
# CS4b — raw_key↔OSS parity (the source-file gap CS4 doesn't cover)
# ──────────────────────────────────────────────────────────────────────────────

def compute_raw_parity(rows: List[Dict[str, Any]], exists_fn) -> Dict[str, Any]:
    """Pure (+ injected exists_fn): of current-version active docs, which raw_key OSS objects are
    MISSING. rows: [{doc_id, version_no, raw_key}]; exists_fn(key)->bool (True iff object exists).
    A NULL raw_key is 'unregistered' (reported separately, not a missing-file). CS4 checks image keys
    only; this closes the raw source-file gap (the DC-3-survey 404). Lower severity: a missing raw does
    NOT break serving (canonical/chunks already exist) — it's a re-ingest/audit concern."""
    null_raw = [r for r in rows if not r.get("raw_key")]
    have = [r for r in rows if r.get("raw_key")]
    missing = [r for r in have if not exists_fn(r["raw_key"])]
    return {
        "ok": not missing,
        "counts": {"total": len(rows), "have_raw_key": len(have),
                   "null_raw_key": len(null_raw), "missing": len(missing)},
        "missing": [{"doc_id": r.get("doc_id"), "version_no": r.get("version_no"),
                     "raw_key": r.get("raw_key")} for r in missing[:50]],
        "null_raw_key_sample": [r.get("doc_id") for r in null_raw[:20]],
    }


def run_raw_parity_check(*, alert: bool = False) -> Dict[str, Any]:
    """CS4b: current-version active docs whose raw_key OSS object is missing. Read-only, simulate-safe,
    fail-open. HEAD-checks each raw_key (bounded to active docs)."""
    from opensearch_pipeline.config import get_config
    cfg = get_config()
    if cfg.simulate or cfg.simulate_db:
        logger.info("reconcile(raw): simulate mode → skipped no-op")
        return {"ok": True, "skipped": "simulate", "complete": True, "counts": {}}
    try:
        conn = _rds_conn()
        try:
            with conn.cursor() as c:
                c.execute(f"""SELECT v.doc_id, v.version_no, v.raw_key
                             FROM {_kb_db()}.document_version v
                             JOIN {_kb_db()}.document_meta m
                               ON m.doc_id=v.doc_id AND m.current_version_no=v.version_no
                             WHERE m.status='active'""")
                rows = _as_dict_rows(c.fetchall(), ("doc_id", "version_no", "raw_key"))
        finally:
            conn.close()
        bucket = _oss_bucket()

        def _exists(k):
            try:
                return bool(bucket.object_exists(k))
            except Exception:  # noqa: BLE001 — conservative: HEAD error → treat as exists (no false alarm)
                return True

        report = compute_raw_parity(rows, _exists)
        report["complete"] = True
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.exception("reconcile(raw): parity check failed")
        report = {"ok": False, "complete": False, "error": f"{type(e).__name__}: {e}", "counts": {}}
    if alert and (not report.get("ok") or report.get("error")):
        _alert_on_raw_drift(report)
    return report


def _alert_on_raw_drift(report: Dict[str, Any]) -> None:
    """Fire one OBS-4 ops alert summarizing raw_key→OSS drift (fail-open)."""
    try:
        from opensearch_pipeline.alerting import send_ops_alert
        c = report.get("counts", {})
        if report.get("error"):
            text = f"raw_key parity check errored: {report['error']}"
        else:
            text = (f"active docs whose raw source file is MISSING from OSS: **{c.get('missing', 0)}** "
                    f"(of {c.get('have_raw_key', 0)} with raw_key; {c.get('null_raw_key', 0)} have none)")
        send_ops_alert("raw_key↔OSS parity drift", text, severity="warning",
                       dedup_key="reconcile:raw-oss-parity")
    except Exception:  # noqa: BLE001
        logger.warning("reconcile(raw): ops-alert dispatch failed (non-fatal)", exc_info=True)


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


def _exit_code(report: Dict[str, Any]) -> int:
    """0 = ok (or simulate-skipped); 2 = drift; 3 = error/incomplete."""
    if report.get("skipped"):
        return 0
    if report.get("error") or report.get("complete") is False:
        return 3
    return 0 if report.get("ok") else 2


def _print_ha3(report: Dict[str, Any]) -> None:
    if report.get("skipped"):
        print(f"[reconcile:ha3] skipped ({report['skipped']})")
        return
    c = report.get("counts", {})
    print(f"[reconcile:ha3] ok={report.get('ok')} complete={report.get('complete')}")
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


def _print_oss(report: Dict[str, Any]) -> None:
    if report.get("skipped"):
        print(f"[reconcile:oss] skipped ({report['skipped']})")
        return
    c = report.get("counts", {})
    print(f"[reconcile:oss] ok={report.get('ok')} complete={report.get('complete')}")
    print(f"  referenced image keys={c.get('referenced')} | OSS objects={c.get('present')}")
    print(f"  ⚠️ referenced MISSING from OSS = {c.get('missing')} (broken images)")
    print(f"  orphan OSS objects = {c.get('orphan')}")
    if report.get("error"):
        print(f"  ERROR: {report['error']}")
    for m in report.get("missing", [])[:10]:
        print(f"    MISSING {m['oss_key']} (chunk={m['chunk_id']})")


def _print_raw(report: Dict[str, Any]) -> None:
    if report.get("skipped"):
        print(f"[reconcile:raw] skipped ({report['skipped']})")
        return
    c = report.get("counts", {})
    print(f"[reconcile:raw] ok={report.get('ok')} complete={report.get('complete')}")
    print(f"  current-version active docs={c.get('total')} "
          f"(with raw_key={c.get('have_raw_key')}, null={c.get('null_raw_key')})")
    print(f"  ⚠️ raw source MISSING from OSS = {c.get('missing')} (CS4 covers image keys only)")
    if report.get("error"):
        print(f"  ERROR: {report['error']}")
    for m in report.get("missing", [])[:10]:
        print(f"    MISSING {m['doc_id']} v{m['version_no']} raw_key={m['raw_key']}")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI. Runs the selected cross-store parity check(s). Exit = worst of the run codes
    (0 = ok / simulate-skipped; 2 = drift; 3 = error/incomplete)."""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="read-only cross-store parity reconciler (CS3 + CS4 + CS4b)")
    ap.add_argument("--check", choices=["ha3", "oss", "raw", "all"], default="all",
                    help="which parity check to run (default: all)")
    ap.add_argument("--alert", action="store_true", help="fire an OBS-4 ops alert on drift/error")
    ap.add_argument("--json", action="store_true", help="emit the full report(s) as JSON")
    ap.add_argument("--hi", type=int, default=None, help="override HA3 PK scan upper bound")
    args = ap.parse_args(argv)

    reports: Dict[str, Any] = {}
    if args.check in ("ha3", "all"):
        reports["ha3"] = run_parity_check(alert=args.alert, hi=args.hi)
    if args.check in ("oss", "all"):
        reports["oss"] = run_oss_parity_check(alert=args.alert)
    if args.check in ("raw", "all"):
        reports["raw"] = run_raw_parity_check(alert=args.alert)

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    else:
        if "ha3" in reports:
            _print_ha3(reports["ha3"])
        if "oss" in reports:
            _print_oss(reports["oss"])
        if "raw" in reports:
            _print_raw(reports["raw"])

    return max((_exit_code(r) for r in reports.values()), default=0)


if __name__ == "__main__":
    import sys
    sys.exit(main())

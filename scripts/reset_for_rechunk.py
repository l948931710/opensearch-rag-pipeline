#!/usr/bin/env python
"""Reset a scoped set of docs' CURRENT version for a re-chunk (stage 2 -> 3), KEEPING canonical.

Sets the canonical re-chunk reset state (opensearch_pipeline.reindex_states.rechunk_reset_state):
content/chunk = NOT_STARTED, **index_status = NOT_INDEXED** (so the stage-3 lock can preempt — the
2026-06-15 canary bug was setting it to 'NOT_STARTED', which silently skipped stage 3).

Read-only preview by default; pass --commit to write. Scope is strictly version_no = current_version_no
(never touches other versions). Writes go through prod_access.get_prod_rw_conn (same-day PROD-RW token).

Usage:
  python scripts/reset_for_rechunk.py --docs scratch/l6_ab/affected_docs.json            # preview
  PROD_RW_ACK=PROD-RW:$(date +%F) python scripts/reset_for_rechunk.py --docs <file> --commit
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opensearch_pipeline.reindex_states import rechunk_reset_state  # noqa: E402


def _load_doc_ids(path: str) -> list:
    data = json.load(open(path))
    if isinstance(data, dict):
        data = data.get("doc_ids") or data.get("docs") or list(data.keys())
    docs = [str(d) for d in data]
    if not docs:
        raise SystemExit(f"no doc_ids in {path}")
    return docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", required=True, help="JSON file: list of doc_ids or {doc_ids:[...]}")
    ap.add_argument("--commit", action="store_true", help="write (default: preview only)")
    args = ap.parse_args()

    docs = _load_doc_ids(args.docs)
    state = rechunk_reset_state()
    print(f"[reset_for_rechunk] {len(docs)} doc(s); target state = {state}")

    from opensearch_pipeline.prod_access import get_prod_readonly_conn, get_prod_rw_conn
    ph = ",".join(["%s"] * len(docs))

    # ── preview (read-only): exactly the current-version rows we will touch ──
    ro = get_prod_readonly_conn()
    with ro.cursor() as cur:
        cur.execute(f"""SELECT dv.doc_id, dv.version_no, dv.content_process_status, dv.chunk_status,
              dv.index_status
            FROM document_version dv
            JOIN document_meta dm ON dm.doc_id = dv.doc_id AND dv.version_no = dm.current_version_no
            WHERE dv.doc_id IN ({ph})""", docs)
        rows = cur.fetchall()
    ro.close()
    found = {r["doc_id"] for r in rows}
    missing = set(docs) - found
    print(f"[preview] current-version rows found: {len(rows)} / {len(docs)} requested")
    if missing:
        print(f"[preview] WARNING: {len(missing)} doc(s) have no current-version row: {sorted(missing)[:5]}")
    for r in rows[:8]:
        print(f"   {r['doc_id'][:40]:40} v{r['version_no']} "
              f"content={r['content_process_status']} chunk={r['chunk_status']} index={r['index_status']}")
    if len(rows) > 8:
        print(f"   ... and {len(rows) - 8} more")

    if not args.commit:
        print("\n[preview] DRY RUN — re-run with --commit (and PROD_RW_ACK=PROD-RW:<today>) to apply.")
        return

    ack = os.environ.get("PROD_RW_ACK") or os.environ.get("RAG_PROD_RW_ACK")
    if not ack:
        raise SystemExit("--commit requires PROD_RW_ACK=PROD-RW:<today> in the environment")

    rw = get_prod_rw_conn(ack=ack)
    with rw.cursor() as cur:
        cur.execute(f"""UPDATE document_version dv
            JOIN document_meta dm ON dm.doc_id = dv.doc_id AND dv.version_no = dm.current_version_no
            SET dv.content_process_status = %s,
                dv.chunk_status = %s,
                dv.index_status = %s,
                dv.retry_count = %s,
                dv.updated_at = NOW()
            WHERE dv.doc_id IN ({ph})""",
            (state["content_process_status"], state["chunk_status"],
             state["index_status"], state["retry_count"], *docs))
        n = cur.rowcount
    rw.commit()
    rw.close()
    print(f"[commit] updated {n} current-version row(s) -> {state} (ack={ack})")


if __name__ == "__main__":
    main()

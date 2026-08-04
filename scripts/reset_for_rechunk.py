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
    ap.add_argument("--include-quarantined", action="store_true",
                    help="允许对隔离件（publish=QUARANTINED / gate=quarantined）执行 reset。"
                         "默认硬拒：隔离件 reset 后走非 orchestrator 裸跑可铸出 gate-only 态"
                         "（列表侧口径分叉的唯一残余链，2026-08-04 独立核验 B2）。")
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
              dv.index_status, dv.publish_status, dv.gate_status
            FROM document_version dv
            JOIN document_meta dm ON dm.doc_id = dv.doc_id AND dv.version_no = dm.current_version_no
            WHERE dv.doc_id IN ({ph})""", docs)
        rows = cur.fetchall()
    ro.close()
    found = {r["doc_id"] for r in rows}
    missing = set(docs) - found
    # 隔离守卫（2026-08-04 独立核验 B2）：与 _kb_version_quarantined 同 OR 语义（authority
    # 在 api.py；此处内联避免拉起整个 FastAPI 依赖面）。隔离件 reset + 非 orchestrator 裸跑
    # 是铸出 gate-only 态（列表徽章口径分叉）的唯一残余链——在链条第一步显式拒绝。
    quarantined = [r for r in rows
                   if str(r.get("publish_status") or "").upper() == "QUARANTINED"
                   or str(r.get("gate_status") or "").lower() == "quarantined"]
    print(f"[preview] current-version rows found: {len(rows)} / {len(docs)} requested")
    if missing:
        print(f"[preview] WARNING: {len(missing)} doc(s) have no current-version row: {sorted(missing)[:5]}")
    if quarantined:
        print(f"[preview] 🔴 {len(quarantined)} QUARANTINED doc(s) in target set: "
              f"{sorted(r['doc_id'] for r in quarantined)[:5]}"
              + ("" if args.include_quarantined else "  (--commit will REFUSE; "
                 "re-run with --include-quarantined only if you understand the gate-only risk)"))
    for r in rows[:8]:
        print(f"   {r['doc_id'][:40]:40} v{r['version_no']} "
              f"content={r['content_process_status']} chunk={r['chunk_status']} index={r['index_status']}")
    if len(rows) > 8:
        print(f"   ... and {len(rows) - 8} more")

    # Doc-set hash for the UNFROZEN re-chunk override token. After this reset the docs keep their
    # chunk_meta, so a stage-2 run on them WITHOUT a freeze will fail-close at node_classify (it would
    # re-roll classification and could flip the chunk family). For a normal maintenance re-chunk set
    # RAG_MAINTENANCE_ROUTING (freeze). Only to DELIBERATELY re-classify (route-v2 family migration)
    # mint the doc-set-bound token below. This is the EXPECTED hash if the re-chunk run covers exactly
    # these found docs; the stage-2 guard prints the AUTHORITATIVE hash for whatever it actually loads.
    from datetime import date
    from opensearch_pipeline.reindex_states import docset_hash
    _h = docset_hash(found)
    print(f"[preview] docset_hash(found)={_h}  "
          f"(unfrozen-rechunk override, if ever needed: "
          f"RAG_ALLOW_UNFROZEN_RECHUNK=<op>:{date.today().isoformat()}:{_h})")

    if not args.commit:
        print("\n[preview] DRY RUN — re-run with --commit (and PROD_RW_ACK=PROD-RW:<today>) to apply.")
        return

    ack = os.environ.get("PROD_RW_ACK") or os.environ.get("RAG_PROD_RW_ACK")
    if not ack:
        raise SystemExit("--commit requires PROD_RW_ACK=PROD-RW:<today> in the environment")
    if quarantined and not args.include_quarantined:
        raise SystemExit(
            f"REFUSED: {len(quarantined)} target doc(s) are quarantined "
            f"({sorted(r['doc_id'] for r in quarantined)[:5]}...). Reset on quarantined docs "
            "can mint a gate-only state via a bare (non-orchestrator) stage-2 run. "
            "Remove them from the doc set, or pass --include-quarantined explicitly.")

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

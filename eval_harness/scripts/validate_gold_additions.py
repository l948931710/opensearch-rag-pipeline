#!/usr/bin/env python3
"""Validate authored gold additions before merging into golden_50.json (see goldset/AUTHORING_GUIDE.md).

Offline (default): schema + taxonomy + qid-uniqueness + coverage-vs-targets.
--verify-live (read-only prod): resolve each positive's doc_id + confirm its keyword_gt appears in a
  retrieved chunk; flag off_topic negatives whose top-1 still scores high (informational).

Usage:
  PYTHONPATH=. python -m eval_harness.scripts.validate_gold_additions \
      --additions eval_harness/goldset/additions.template.json [--verify-live]
Exit code 0 = schema OK (coverage shortfalls are warnings); non-zero = schema/taxonomy errors.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

NEG_TYPES = {"off_topic", "near_miss_answer_absent", "metadata", "modality_gap", "live_data"}
DEPTS = {"it", "production", "quality", "sales", "marketing", "hr", "admin", "finance"}
TARGETS = {"off_topic": 5, "metadata": 3, "image_cases": 5, "xlsx_cases": 1}
DEPT_TARGET = 3  # positives per uncovered dept
UNCOVERED = {"it", "production", "quality", "sales", "marketing"}


def _schema_errors(e: dict, qid: str) -> list:
    errs = []
    for f in ("qid", "source", "module", "query", "kind"):
        if not e.get(f):
            errs.append(f"{qid}: missing required field '{f}'")
    kind = e.get("kind")
    if kind not in ("positive", "negative"):
        errs.append(f"{qid}: kind must be positive|negative (got {kind!r})")
    if kind == "positive":
        if not e.get("expected_docs"):
            errs.append(f"{qid}: positive needs non-empty expected_docs")
        if not e.get("keyword_gt"):
            errs.append(f"{qid}: positive needs >=1 keyword_gt (VERBATIM substring of the chunk)")
        if e.get("expect_images") and not e.get("expected_images"):
            errs.append(f"{qid}: expect_images=true but expected_images is empty")
    elif kind == "negative":
        nt = e.get("neg_type")
        if nt not in NEG_TYPES:
            errs.append(f"{qid}: negative neg_type must be one of {sorted(NEG_TYPES)} (got {nt!r})")
        if e.get("expected_docs"):
            errs.append(f"{qid}: negative should have empty expected_docs (it's unanswerable)")
        if e.get("keyword_gt"):
            errs.append(f"{qid}: negative should have empty keyword_gt")
    dept = e.get("dept")
    if dept is not None and dept not in DEPTS:
        errs.append(f"{qid}: dept {dept!r} not in {sorted(DEPTS)}")
    return errs


def _coverage(adds: list) -> dict:
    negs = [e for e in adds if e.get("kind") == "negative"]
    pos = [e for e in adds if e.get("kind") == "positive"]
    return {
        "off_topic": sum(1 for e in negs if e.get("neg_type") == "off_topic"),
        "metadata": sum(1 for e in negs if e.get("neg_type") == "metadata"),
        "image_cases": sum(1 for e in pos if e.get("expect_images")),
        "xlsx_cases": sum(1 for e in pos if e.get("source") == "xlsx"),
        "dept_pos": Counter(e.get("dept") for e in pos),
        "neg_type_dist": dict(Counter(e.get("neg_type") for e in negs)),
        "n_pos": len(pos), "n_neg": len(negs),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--additions", required=True)
    ap.add_argument("--goldset", default="eval_harness/goldset/golden_50.json")
    ap.add_argument("--verify-live", action="store_true",
                    help="read-only prod: resolve doc_ids + check keyword_gt in retrieved chunk")
    args = ap.parse_args(argv)

    adds = json.load(open(args.additions, encoding="utf-8"))
    if not isinstance(adds, list):
        print("FATAL: additions file must be a JSON array of case objects")
        return 3
    existing = {c.get("qid") for c in json.load(open(args.goldset, encoding="utf-8"))}

    errors, seen = [], set()
    for e in adds:
        qid = e.get("qid", "<no-qid>")
        if qid in seen or qid in existing:
            errors.append(f"{qid}: duplicate qid (collides with existing gold or another addition)")
        seen.add(qid)
        errors.extend(_schema_errors(e, qid))

    print(f"=== {len(adds)} additions: schema check ===")
    for er in errors:
        print(f"  ERROR {er}")
    if not errors:
        print("  schema OK")

    cov = _coverage(adds)
    print("\n=== coverage vs targets (shortfalls = WARN, not error) ===")
    for key, tgt in TARGETS.items():
        got = cov[key]
        print(f"  {key}: {got}/{tgt} {'OK' if got >= tgt else 'WARN (short)'}")
    print(f"  uncovered-dept positives (target {DEPT_TARGET} each): "
          + ", ".join(f"{d}={cov['dept_pos'].get(d,0)}" for d in sorted(UNCOVERED)))
    print(f"  neg_type distribution: {cov['neg_type_dist']}")

    if args.verify_live:
        print("\n=== --verify-live (read-only prod) ===")
        try:
            from eval_harness import envboot  # noqa: F401
            from opensearch_pipeline.retriever import retrieve_and_enrich
        except Exception as ex:  # noqa: BLE001
            print(f"  SKIP: could not init retriever ({ex})")
        else:
            for e in adds:
                q = e.get("query")
                if not q:
                    continue
                try:
                    res = retrieve_and_enrich(q, top_k=5)
                    chunks = res if isinstance(res, list) else (res.get("chunks") or res.get("results") or [])
                except Exception as ex:  # noqa: BLE001
                    print(f"  {e.get('qid')}: retrieve error {ex}")
                    continue
                texts = " ".join((c.get("chunk_text") or c.get("text") or "") for c in chunks if isinstance(c, dict))
                top = chunks[0] if chunks else {}
                top_doc = top.get("doc_id") if isinstance(top, dict) else None
                top_sc = top.get("score") if isinstance(top, dict) else None
                if e.get("kind") == "positive":
                    miss = [k for k in (e.get("keyword_gt") or []) if k not in texts]
                    ok = not miss and (not e.get("expected_doc_ids") or top_doc in (e.get("expected_doc_ids") or []))
                    print(f"  [{'OK' if ok else 'CHECK'}] {e.get('qid')} top_doc={top_doc} "
                          + (f"keyword_gt MISSING from retrieved: {miss}" if miss else "keyword_gt present"))
                elif e.get("neg_type") == "off_topic":
                    hi = isinstance(top_sc, (int, float)) and top_sc >= 0.9
                    print(f"  [{'LEAK?' if hi else 'ok'}] {e.get('qid')} off_topic top1_score={top_sc} "
                          f"(high score on off_topic = the leak the L2 AUC gate measures)")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Authoritative HA3 presence verification — immune to the G30 enumeration defect.

Problem (cost a false-FAIL on 2026-06-20): the zero-vector range-scan
`client.query(vector=[0]*1024, filter="id>=lo AND id<hi")` is **non-deterministic
and incomplete** (G30). Right after a realtime push it can return 0 rows even though
every chunk is indexed and serving — so using it to confirm "is this doc in HA3?"
produces false negatives (and could wrongly trigger a rollback).

Authoritative method = **per-chunk self-query**: take each chunk's own text, run it
through the real serving path (`retrieve_and_enrich`, which uses a query vector +
hybrid + the ACL filter), and confirm the chunk returns *itself*. If a chunk can be
retrieved by its own content under its owner's ACL, it is provably indexed + served.
This is the same dense self-query idea as the rebuild G29 gate, generalized.

Pure/injectable: `retrieve_fn` and the RDS cursor source are passed in, so this is
unit-testable without network and reusable by spot_checker / rebuild / 上线 verifies.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


def _active_chunks(conn, doc_id: str, kn: str = "fuling_knowledge") -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, chunk_id, chunk_text, chunk_type, owner_dept "
            f"FROM {kn}.chunk_meta WHERE doc_id=%s AND is_active=1 ORDER BY id",
            (doc_id,),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append(r if isinstance(r, dict) else
                   {"id": r[0], "chunk_id": r[1], "chunk_text": r[2], "chunk_type": r[3], "owner_dept": r[4]})
    return out


def verify_chunks_present(
    doc_id: str,
    *,
    conn,
    retrieve_fn: Callable[..., List[Dict[str, Any]]],
    kn: str = "fuling_knowledge",
    snippet: int = 160,
    top_k: int = 5,
    owner_dept_override: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Confirm every active chunk of `doc_id` is searchable in HA3 via self-query.

    Args:
      conn        : a DB connection (read-only is fine) yielding chunk_meta rows.
      retrieve_fn : callable(query:str, *, top_k:int, user_dept) -> list[result dict].
                    In prod pass `retriever.retrieve_and_enrich`. Each result should
                    carry "id" (str of chunk_meta.id) and/or "doc_id"+"chunk_id".
      owner_dept_override : ACL groups to query as; default = each chunk's owner_dept.

    Returns: {expected_ids, present_ids, missing_ids, present, total, ok,
              served_ids (all this-doc ids observed), foreign_ids (served ids of
              OTHER docs surfaced by these queries — should be empty), method}.
    """
    chunks = _active_chunks(conn, doc_id, kn)
    expected = sorted(int(c["id"]) for c in chunks)
    present, served, foreign = set(), set(), set()
    for c in chunks:
        ud = owner_dept_override if owner_dept_override is not None else (
            [c["owner_dept"]] if c.get("owner_dept") else None)
        q = (c.get("chunk_text") or "")[:snippet]
        if not q.strip():
            continue
        try:
            results = retrieve_fn(q, top_k=top_k, user_dept=ud) or []
        except Exception:
            results = []
        for r in results:
            rdoc = r.get("doc_id")
            rid = r.get("id")
            if rdoc == doc_id:
                try:
                    served.add(int(rid))
                except (TypeError, ValueError):
                    pass
                if str(rid) == str(c["id"]) or r.get("chunk_id") == c.get("chunk_id"):
                    present.add(int(c["id"]))
            elif rid is not None:
                try:
                    foreign.add(int(rid))
                except (TypeError, ValueError):
                    pass
    missing = sorted(set(expected) - present)
    return {
        "doc_id": doc_id,
        "expected_ids": expected,
        "present_ids": sorted(present),
        "missing_ids": missing,
        "present": len(present),
        "total": len(expected),
        "served_ids": sorted(served),
        # 契约键（docstring 承诺）：自查中浮现的【他文档】id —— 跨文档/ACL 泄漏信号。
        # 此前漏返回，导致上线 verify 读 result['foreign_ids'] KeyError、泄漏检查形同虚设。
        "foreign_ids": sorted(foreign),
        "ok": bool(expected) and not missing,
        "method": "per-chunk self-query (G30-immune)",
    }

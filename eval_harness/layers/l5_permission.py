"""Layer 5 — Permission filtering regression + filter-injection safety.

Verifies the rebuilt index + serving filter still enforce department access control:
  - A permission-gated (dept_internal/restricted) doc must NOT surface on the public path.
  - It SHOULD surface for an authorized user_dept (== its owner_dept).
  - A malicious user_dept (filter-injection payload) must not leak other-dept gated docs.

If the live corpus has no gated docs, the layer reports `applicable=False` (filtering is
simply not exercised by this corpus) rather than a misleading pass.
"""
from __future__ import annotations

from typing import Dict, List

from .. import envboot  # noqa: F401
from ..ha3live import doc_inventory

INJECTION_PAYLOADS = [
    'x" OR permission_level="dept_internal',
    'x" OR "1"="1',
    '*',
    'admin" OR owner_dept="',
]


def _doc_in(results: List[Dict], doc_id: str) -> bool:
    return any(str(r.get("doc_id")) == doc_id for r in results)


def run(max_docs: int = 5) -> Dict:
    from opensearch_pipeline.retriever import search_chunks

    inv = doc_inventory()
    gated = [d for d in inv if (d.get("permission_level") or "public") in ("dept_internal", "restricted")
             and d.get("title") and d.get("owner_dept")]

    if not gated:
        return {"applicable": False,
                "note": "No permission-gated (dept_internal/restricted) docs with owner_dept "
                        "in the live corpus; dept filtering is not exercised by this index.",
                "n_public_docs": sum(1 for d in inv if (d.get("permission_level") or "public") == "public"),
                "n_total_docs": len(inv)}

    probes = []
    excl_ok = auth_ok = 0
    for d in gated[:max_docs]:
        q = d["title"]; did = d["doc_id"]; dept = d["owner_dept"]
        pub = search_chunks(q, top_k=10, user_dept=None)
        auth = search_chunks(q, top_k=10, user_dept=dept)
        leaked_public = _doc_in(pub, did)
        visible_auth = _doc_in(auth, did)
        excl_ok += (0 if leaked_public else 1)
        auth_ok += (1 if visible_auth else 0)
        probes.append({
            "doc_id": did, "title": q, "owner_dept": dept,
            "permission_level": d.get("permission_level"),
            "leaked_on_public_path": leaked_public,     # must be False
            "visible_to_authorized_dept": visible_auth,  # should be True
        })

    # injection: use the first gated doc; query with malicious user_dept values
    inj = []
    d0 = gated[0]
    for payload in INJECTION_PAYLOADS:
        try:
            res = search_chunks(d0["title"], top_k=10, user_dept=payload)
            leaked = _doc_in(res, d0["doc_id"])
            inj.append({"payload": payload, "leaked_gated_doc": leaked, "error": None})
        except Exception as e:
            inj.append({"payload": payload, "leaked_gated_doc": None, "error": f"{type(e).__name__}: {e}"[:120]})

    injection_safe = all((p["leaked_gated_doc"] in (False, None)) for p in inj)
    return {
        "applicable": True,
        "n_gated_docs_tested": len(probes),
        "public_exclusion_ok": excl_ok,
        "authorized_visibility_ok": auth_ok,
        "no_public_leak": all(not p["leaked_on_public_path"] for p in probes),
        "injection_safe": injection_safe,
        "probes": probes,
        "injection": inj,
        "PASS": all(not p["leaked_on_public_path"] for p in probes) and injection_safe,
    }

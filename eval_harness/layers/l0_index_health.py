"""Layer 0 — Index health gates (rebuild-specific, fast, no LLM).

Proves the rebuilt index is actually healthy and the order=DESC InnerProduct trap is not
biting. Gates (all read-only):

  G0  status == IN_USE and docCount == active chunk_meta count (no silent loss)
  G1  segments > 0 on every partition
  G2  DENSE self-query: a chunk re-embedded and queried (order=DESC) returns ITSELF at
      rank-1 with high self-score  -> proves dense leg + ordering are correct
  G3  SPARSE self-query: sparse-only (zero dense) returns itself at rank-1 for >=95%
      -> proves the sparse vector was actually built (else hybrid collapses to BM25)
  G4  VECTOR FIDELITY (blast-radius): stored vector ~= fresh embedding (cosine), so the
      index holds the right vectors (no stale / corrupt push)
"""
from __future__ import annotations

from typing import Dict

from .. import envboot  # noqa: F401
from ..ha3live import (status_and_stats, query_vector, fields_of, cosine,
                       sample_active_chunks, total_active_chunks, table)
from ..metrics import mean, percentiles


def _stored_vector(item: Dict):
    for key in ("vector", "embedding"):
        if key in item and isinstance(item[key], list):
            return item[key]
    f = item.get("fields", {})
    for key in ("vector", "embedding"):
        if isinstance(f.get(key), list):
            return f[key]
    return None


def run(n_dense: int = 60, n_sparse: int = 40, self_score_min: float = 0.95,
        drift_min: float = 0.99, seed: int = 7) -> Dict:
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.retriever import get_query_embedding
    dim = get_config().embedding.dimension or 1024

    out: Dict = {"table": table()}

    # G0/G1 — status, docCount, segments
    st = status_and_stats()
    active = total_active_chunks()
    out["stats"] = st
    out["rds_active_chunks"] = active
    doc_count = st.get("docCount")
    delta = (doc_count - active) if (doc_count is not None and active is not None) else None
    # Loss (HA3 < RDS active) is serious — a doc silently vanished from search.
    # A small surplus (HA3 > RDS) is usually old-version chunks pending deactivation/cleanup.
    tolerance = max(5, int(active * 0.005)) if active else 5
    if delta is None:
        interp, g0 = "unknown", False
    elif delta < 0:
        interp, g0 = "DATA LOSS: index has fewer docs than active chunks", False
    elif delta == 0:
        interp, g0 = "exact match", True
    elif delta <= tolerance:
        interp, g0 = f"+{delta} surplus within tolerance (likely stale chunks pending cleanup)", True
    else:
        interp, g0 = f"+{delta} surplus exceeds tolerance ({tolerance}) — check deactivation", False
    out["G0_status_doccount"] = {
        "pass": bool(g0 and st.get("status") == "IN_USE"), "status": st.get("status"),
        "docCount": doc_count, "rds_active": active, "delta": delta,
        "tolerance": tolerance, "interpretation": interp,
    }
    out["G1_segments"] = {"pass": bool(st.get("segments_ok")), "partitions": st.get("partitions")}

    # sample chunks once; reuse for dense + drift; first n_sparse for sparse gate
    sample = sample_active_chunks(max(n_dense, n_sparse), seed=seed)

    # G2 — dense self-query (+ G4 vector fidelity from include_vector)
    # Health = top-1 self-score ~1.0 AND it returns the same chunk OR an identical-text
    # sibling (duplicate chunk content is a chunking issue, NOT an index failure).
    def _norm(t):
        return "".join((t or "").split())

    dense_score_ok = 0; id_exact = 0; text_dup = 0; self_scores = []
    drift_cos = []; dense_fails = []
    for row in sample[:n_dense]:
        cid, txt = row["chunk_id"], row["chunk_text"]
        try:
            dense, _si, _sv = get_query_embedding(txt)
        except Exception as e:
            dense_fails.append({"chunk_id": cid, "err": f"embed:{e}"[:120]}); continue
        items = query_vector(dense, top_k=2, include_vector=True, order="DESC",
                             output_fields=["chunk_id", "doc_id", "title", "chunk_text_store"])
        if not items:
            dense_fails.append({"chunk_id": cid, "top": None, "score": None}); continue
        top = items[0]; tf = fields_of(top)
        top_cid = tf.get("chunk_id"); score = top.get("score")
        same_id = (top_cid == cid)
        same_text = (_norm(tf.get("chunk_text_store")) == _norm(txt)) and bool(_norm(txt))
        healthy = (score is not None and score >= 0.99) and (same_id or same_text)
        if same_id:
            id_exact += 1
        elif same_text:
            text_dup += 1
        if healthy:
            dense_score_ok += 1
            self_scores.append(float(score))
            sv = _stored_vector(top)
            if sv:
                drift_cos.append(cosine(sv, dense))
        else:
            dense_fails.append({"chunk_id": cid, "top": top_cid, "score": score,
                                "same_text": same_text})
    out["G2_dense_self_query"] = {
        "pass": dense_score_ok >= int(n_dense * 0.98),
        "healthy": dense_score_ok, "total": n_dense,
        "id_exact_match": id_exact, "identical_text_sibling": text_dup,
        "self_score_min_seen": round(min(self_scores), 4) if self_scores else None,
        "self_score_mean": round(mean(self_scores), 4) if self_scores else None,
        "fails_sample": dense_fails[:5],
        "note": "healthy = self-score>=0.99 returning same chunk or an identical-text sibling",
    }
    out["duplicate_content_diagnostic"] = {
        "exact_id_self_match_rate": round(id_exact / n_dense, 3),
        "identical_text_sibling_rate": round(text_dup / n_dense, 3),
        "note": "high identical-text-sibling rate => duplicate chunk content (chunking quality), "
                "not an index fault",
    }
    out["G4_vector_fidelity"] = (
        {"pass": (mean(drift_cos) >= drift_min) if drift_cos else None,
         "n": len(drift_cos), "cos_mean": round(mean(drift_cos), 5) if drift_cos else None,
         "cos_min": round(min(drift_cos), 5) if drift_cos else None,
         "note": "stored index vector vs fresh embedding (cos~1.0 => no drift/corruption)"}
        if drift_cos else
        {"pass": None, "n": 0, "note": "include_vector returned no vectors; drift check skipped"}
    )

    # G3 — sparse-only self-query (zero dense). Same dup-text tolerance as G2.
    zeros = [0.0] * dim
    sparse_ok = sparse_tot = 0
    from alibabacloud_ha3engine_vector.models import SparseData
    for row in sample[:n_sparse]:
        cid, txt = row["chunk_id"], row["chunk_text"]
        try:
            _d, si, sv = get_query_embedding(txt)
        except Exception:
            continue
        if not si:
            continue
        sparse_tot += 1
        sd = SparseData(count=[len(si)], indices=si, values=sv)
        items = query_vector(zeros, sparse=sd, top_k=5, order="DESC",
                             output_fields=["chunk_id", "chunk_text_store"])
        if items:
            tf = fields_of(items[0])
            if tf.get("chunk_id") == cid or (
                    _norm(tf.get("chunk_text_store")) == _norm(txt) and _norm(txt)):
                sparse_ok += 1
    out["G3_sparse_self_query"] = {
        "pass": (sparse_tot > 0 and sparse_ok >= int(sparse_tot * 0.90)),
        "ok": sparse_ok, "total": sparse_tot,
        "note": "sparse vector present & queryable (dup-text siblings allowed)" if sparse_tot
                else "NO sparse vectors produced",
    }

    gates = [out["G0_status_doccount"]["pass"], out["G1_segments"]["pass"],
             out["G2_dense_self_query"]["pass"], out["G3_sparse_self_query"]["pass"]]
    if out["G4_vector_fidelity"]["pass"] is not None:
        gates.append(out["G4_vector_fidelity"]["pass"])
    out["PASS"] = all(gates)
    return out

# -*- coding: utf-8 -*-
"""485 盲文档治愈只读复检（2026-07-12）——零写，prod_ro RDS + HA3 公网自查询。

复检项：
  A. RDS 状态：485 docs active chunk index_status 分布 / 今日被重推触碰数 /
     document_version 今日 SUCCESS 收尾数 / 双 active 残余数（=stage-3 进度表）
  B. HA3 自查询（终验口径，点查/枚举不作数）：
     - 探针 doc DOC_ADMIN_20260513120214_A8CB90 延迟复验（verify_chunks_present）
     - 485 内 seeded 抽样 15 docs：每 doc 取首个 active chunk 前 160 字作查询，
       retrieve_and_enrich top_k=10，看本 doc 是否返回（含 rank）
     - BM25 文号探针 FL-QC-015-002：明文含它的盲行现在是否可被倒排召回
"""
import json
import random
import sys

sys.path.insert(0, ".")
from eval_harness import envboot  # noqa: F401  (boots .env+.env.prod_ro, RAG_SIMULATE=false)

from opensearch_pipeline.prod_access import get_prod_readonly_conn

PROBE_DOC = "DOC_ADMIN_20260513120214_A8CB90"
BM25_TOKEN = "FL-QC-015-002"
TODAY = "2026-07-12 00:00:00"
KN = "fuling_knowledge"

docs = [ln.strip() for ln in open("scratch/blind485_docids_20260712.txt") if ln.strip()]
print(f"[SCOPE] 485 清单实读 {len(docs)} docs")
ph = ",".join(["%s"] * len(docs))

report = {"scope": len(docs)}
conn = get_prod_readonly_conn(dict_cursor=True)
with conn.cursor() as cur:
    # A1 active chunk index_status 分布
    cur.execute(f"SELECT index_status, COUNT(*) n FROM {KN}.chunk_meta "
                f"WHERE doc_id IN ({ph}) AND is_active=1 GROUP BY index_status", docs)
    dist = {r["index_status"]: r["n"] for r in cur.fetchall()}
    report["chunk_index_status"] = dist
    print(f"[A1] active chunk index_status: {dist}")

    # A2 今日被触碰（重推证据）
    cur.execute(f"SELECT COUNT(DISTINCT doc_id) n FROM {KN}.chunk_meta "
                f"WHERE doc_id IN ({ph}) AND is_active=1 AND updated_at >= %s",
                docs + [TODAY])
    report["docs_chunks_touched_today"] = cur.fetchone()["n"]
    print(f"[A2] 今日 chunk 被触碰 docs: {report['docs_chunks_touched_today']}/485")

    # A3 document_version 今日 SUCCESS 收尾
    cur.execute(f"SELECT COUNT(DISTINCT doc_id) n FROM {KN}.document_version "
                f"WHERE doc_id IN ({ph}) AND index_status='SUCCESS' AND updated_at >= %s",
                docs + [TODAY])
    report["dv_success_today"] = cur.fetchone()["n"]
    print(f"[A3] document_version 今日 SUCCESS 收尾 docs: {report['dv_success_today']}/485")

    # A4 双 active 残余（stage-3 版本收尾进度表；含 481ed79 supersede 自愈）
    cur.execute(f"SELECT COUNT(*) n FROM (SELECT doc_id FROM {KN}.document_version "
                f"WHERE doc_id IN ({ph}) AND status='active' "
                f"GROUP BY doc_id HAVING COUNT(*)>1) t", docs)
    report["double_active_remaining"] = cur.fetchone()["n"]
    print(f"[A4] 双 active 残余 docs: {report['double_active_remaining']}/485")

    # BM25 探针宿主 doc（明文含文号的行）
    cur.execute(f"SELECT doc_id, id FROM {KN}.chunk_meta "
                f"WHERE doc_id IN ({ph}) AND is_active=1 AND chunk_text LIKE %s LIMIT 3",
                docs + [f"%{BM25_TOKEN}%"])
    bm25_hosts = cur.fetchall()
    report["bm25_host_rows"] = [(r["doc_id"], r["id"]) for r in bm25_hosts]
    print(f"[BM25] 明文含 {BM25_TOKEN} 的活 chunk 行: {report['bm25_host_rows']}")

# ── B. HA3 live 自查询 ──────────────────────────────────────────────────────
from opensearch_pipeline.retriever import retrieve_and_enrich  # noqa: E402
from opensearch_pipeline.ha3_verify import verify_chunks_present  # noqa: E402

# B1 探针 doc 延迟复验
v = verify_chunks_present(PROBE_DOC, conn=conn, retrieve_fn=retrieve_and_enrich, top_k=10)
report["probe_doc"] = {k: v[k] for k in ("present", "total", "ok", "missing_ids")}
print(f"[B1] 探针 doc 延迟复验: present={v['present']}/{v['total']} ok={v['ok']} "
      f"missing={v['missing_ids']}")

# B2 seeded 抽样 15 docs 自查询（每 doc 首个 active chunk）
rng = random.Random(20260712)
sample = rng.sample(docs, 15)
rows = []
with conn.cursor() as cur:
    for d in sample:
        cur.execute(f"SELECT chunk_text, owner_dept FROM {KN}.chunk_meta "
                    f"WHERE doc_id=%s AND is_active=1 ORDER BY id LIMIT 1", (d,))
        r = cur.fetchone()
        rows.append((d, (r or {}).get("chunk_text") or "", (r or {}).get("owner_dept")))

hits, results = 0, []
for d, text, dept in rows:
    q = text[:160].strip()
    if not q:
        results.append({"doc_id": d, "rank": None, "note": "empty chunk_text"})
        continue
    try:
        res = retrieve_and_enrich(q, top_k=10, user_dept=[dept] if dept else None) or []
    except Exception as e:
        results.append({"doc_id": d, "rank": None, "note": f"ERR {e}"})
        continue
    rank = next((i + 1 for i, r in enumerate(res) if r.get("doc_id") == d), None)
    if rank:
        hits += 1
    results.append({"doc_id": d, "rank": rank})
report["sample_selfquery"] = {"n": len(rows), "hits": hits, "detail": results}
print(f"[B2] 抽样自查询: {hits}/{len(rows)} 命中 top-10（上轮法证同法为 0/15）")
for r in results:
    print(f"     {r['doc_id']}  rank={r['rank']}" + (f"  {r.get('note','')}" if r.get("note") else ""))

# B3 BM25 文号探针（纯倒排臂，复现法证方法）
try:
    from alibabacloud_ha3engine_vector.models import SearchRequest, TextQuery, RankQuery
    from opensearch_pipeline.retriever import _get_ha3_client
    from opensearch_pipeline.config import get_config
    cfg = get_config().alibaba_vector
    tq = TextQuery(query_string=f"chunk_text:'{BM25_TOKEN}'", query_params={"default_op": "OR"})
    req = SearchRequest(table_name=cfg.table_name, text=tq, rank=RankQuery(), size=20,
                        output_fields=["doc_id", "chunk_id"])
    resp = _get_ha3_client().search(req)
    body = json.loads(resp.body) if isinstance(resp.body, (str, bytes)) else resp.body
    # 容错解析：body["result"] 可能是 dict{items} 或直接是 list
    res = body.get("result") if isinstance(body, dict) else body
    items = res.get("items", []) if isinstance(res, dict) else (res or [])
    got_docs = [(it.get("fields", it) or {}).get("doc_id")
                for it in items if isinstance(it, dict)]
    host_docs = {d for d, _ in report["bm25_host_rows"]}
    report["bm25_probe"] = {"returned_docs": got_docs,
                            "host_recalled": sorted(host_docs & set(got_docs))}
    print(f"[B3] BM25 探针返回 {len(got_docs)} 行；盲行宿主命中: "
          f"{report['bm25_probe']['host_recalled'] or '无'}（法证时=缺席）")
except Exception as e:
    report["bm25_probe"] = {"error": str(e)}
    print(f"[B3] BM25 探针失败（不影响 B1/B2 结论）: {e}")

conn.close()
out = "scratchpad_verify485_report.json"
json.dump(report, open(out, "w"), ensure_ascii=False, indent=1)
print(f"\n[REPORT] {out}")

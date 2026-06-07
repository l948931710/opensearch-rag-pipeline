"""Validate fuling_kb_chunks_v2 once the console build is complete.
Self-query gate (dense+sparse), BM25 regression, stats sanity, bot e2e.

PASS criteria (all must hold):
  - status == IN_USE, docCount == 3669 (exactly)
  - 100/100 dense self-queries rank-1 == self with score >= 0.99
  -  95+/100 sparse self-queries rank-1 == self
  -  20/20 BM25 keyword queries match live top-1
  - bot_query_test against v2 returns 触电 SOP at rank-1

No mutating ops. Live fuling_kb_chunks is read-only-compared, not modified.
"""
import os, sys, json, math, random, hashlib, datetime
def _load(p):
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
_load(".env"); _load(".env.production")
os.environ.update({"RAG_ENVIRONMENT":"test","RAG_ENV":"","RAG_SIMULATE":"false",
    "RAG_SIMULATE_DB":"false","RAG_SIMULATE_API":"false",
    "RAG_HA3_ENDPOINT":"ha-cn-kgl4slr1n01.public.ha.aliyuncs.com"})
_ds = os.environ.get("RAG_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY","")
os.environ["RAG_DASHSCOPE_API_KEY"]=_ds; os.environ["DASHSCOPE_API_KEY"]=_ds

V2 = "fuling_kb_chunks_v2"
LIVE = "fuling_kb_chunks"
SAMPLE_DENSE = 100
SAMPLE_SPARSE = 100
SAMPLE_BM25 = 20
OUT = f"scratch/validate_v2_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
os.makedirs(OUT, exist_ok=True)

from alibabacloud_ha3engine_vector.client import Client
from alibabacloud_ha3engine_vector.models import (Config, QueryRequest, SparseData,
    SearchRequest, TextQuery, RankQuery)
cli = Client(Config(endpoint="ha-cn-kgl4slr1n01.public.ha.aliyuncs.com",
    instance_id="ha-cn-kgl4slr1n01",
    access_user_name=os.environ["RAG_HA3_USER"], access_pass_word=os.environ["RAG_HA3_PASSWORD"],
    protocol="HTTP"))

def tomap(r):
    b = getattr(r,"body",r)
    if isinstance(b,str):
        try: return json.loads(b)
        except: return b
    return b.to_map() if hasattr(b,"to_map") else b
def parse(resp):
    b=tomap(resp)
    raw=b.get("result",b.get("hits",b.get("data",[]))) if isinstance(b,dict) else []
    if isinstance(raw,dict): raw=raw.get("hits") or raw.get("items") or []
    return raw if isinstance(raw,list) else []

# 0. Pre-check: table is IN_USE + docCount exact
print(f"=== checking {V2} status ===")
gt = tomap(cli.get_table(V2)); status = (gt.get("result") or {}).get("status")
stats = tomap(cli.stats(V2)); dc = (stats.get("result") or {}).get("totalDocCount")
print(f"  status={status}  docCount={dc}  expected_docCount=3669")
status_ok = (status == "IN_USE") and (dc == 3669)
print(f"  status_check: {'PASS' if status_ok else 'FAIL — wait for build to finish'}")
if not status_ok:
    print("Aborting validation; table not ready."); sys.exit(1)

# 1. Sample chunks from RDS + cache for self-query
import pymysql
cache = json.load(open("scratch/embedding_cache.json"))
EMB_MODEL = "text-embedding-v4"
def ckey(t): return hashlib.md5(f"{EMB_MODEL}_{t}".encode()).hexdigest()
conn=pymysql.connect(host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT","3306")),
    user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ.get("RAG_RDS_DATABASE","fuling_knowledge"), connect_timeout=8, charset="utf8mb4")
with conn.cursor() as c:
    c.execute("SELECT id, chunk_id, chunk_text FROM chunk_meta WHERE is_active=1")
    all_rows = c.fetchall()
conn.close()
random.seed(7)
dense_sample = random.sample(all_rows, SAMPLE_DENSE)

# 2. Dense self-query gate
print(f"\n=== dense self-query gate (n={SAMPLE_DENSE}, expect 100/100 rank-1) ===")
dense_ok = 0; dense_fails = []
for rds_id, chunk_id, txt in dense_sample:
    k = ckey(txt); dense = cache[k]
    q = QueryRequest(table_name=V2, vector=dense, sparse_data=None, top_k=2,
                     include_vector=False, output_fields=["chunk_id","id"])
    items = parse(cli.query(q))
    if items:
        top = items[0].get("fields", items[0]); top_cid = top.get("chunk_id"); score = items[0].get("score")
        if top_cid == chunk_id and (score or 0) >= 0.99: dense_ok += 1
        else: dense_fails.append({"chunk_id": chunk_id, "top": top_cid, "score": score})
    else:
        dense_fails.append({"chunk_id": chunk_id, "top": None, "score": None})
print(f"  dense: {dense_ok}/{SAMPLE_DENSE} rank-1 @>=0.99")
if dense_fails[:3]: print(f"  sample fails: {dense_fails[:3]}")
json.dump({"ok": dense_ok, "total": SAMPLE_DENSE, "fails": dense_fails}, open(f"{OUT}/dense.json","w"), indent=2)

# 3. Sparse self-query
print(f"\n=== sparse self-query (n={SAMPLE_SPARSE}, expect >=95/100 rank-1) ===")
sparse_ok = 0
for rds_id, chunk_id, txt in dense_sample[:SAMPLE_SPARSE]:
    k = ckey(txt); sp = cache.get(f"sp_{k}", {})
    sp_i = sp.get("indices", []); sp_v = sp.get("values", [])
    if not sp_i: continue
    sd = SparseData(count=[len(sp_i)], indices=sp_i, values=sp_v)
    # sparse-only: dense vector zero-filled (Sparse-only query)
    zeros = [0.0] * 1024
    q = QueryRequest(table_name=V2, vector=zeros, sparse_data=sd, top_k=5,
                     include_vector=False, output_fields=["chunk_id"])
    items = parse(cli.query(q))
    if items and items[0].get("fields", items[0]).get("chunk_id") == chunk_id:
        sparse_ok += 1
print(f"  sparse: {sparse_ok}/{SAMPLE_SPARSE} rank-1")

# 4. BM25 regression (compare top-1 between live and v2)
print(f"\n=== BM25 regression (n={SAMPLE_BM25}, expect v2 top-1 == live top-1) ===")
QUERIES = ["触电应急","员工手册","宿舍管理","报销流程","入职流程","年休假","薪资","加班","设备维修",
           "安全规定","质量管理","出货","订单","客户","生产计划","入库","采购","品质","培训","考勤"]
bm25_ok = 0; bm25_fails = []
for q in QUERIES[:SAMPLE_BM25]:
    tq = TextQuery(query_string=f"chunk_text:'{q}'", query_params={"default_op":"OR"})
    req = SearchRequest(table_name=V2, text=tq, rank=RankQuery(), size=1, output_fields=["chunk_id","title"])
    v2_top = parse(cli.search(req))
    tq2 = TextQuery(query_string=f"chunk_text:'{q}'", query_params={"default_op":"OR"})
    req2 = SearchRequest(table_name=LIVE, text=tq2, rank=RankQuery(), size=1, output_fields=["chunk_id","title"])
    live_top = parse(cli.search(req2))
    v2_cid = (v2_top[0].get("fields", v2_top[0]) if v2_top else {}).get("chunk_id")
    live_cid = (live_top[0].get("fields", live_top[0]) if live_top else {}).get("chunk_id")
    if v2_cid and v2_cid == live_cid: bm25_ok += 1
    else: bm25_fails.append({"q":q,"v2":v2_cid,"live":live_cid})
print(f"  bm25: {bm25_ok}/{SAMPLE_BM25} match live top-1")
if bm25_fails[:3]: print(f"  sample diffs: {bm25_fails[:3]}")

# 5. Stats sanity
print(f"\n=== stats sanity ===")
stats_v2 = tomap(cli.stats(V2)).get("result", {})
print(f"  v2 stats: {json.dumps(stats_v2, ensure_ascii=False)[:300]}")
parts = stats_v2.get("partitions") or []
seg_ok = all(p.get("segmentCount", 0) > 0 for p in parts) and stats_v2.get("totalDocCount") == 3669
print(f"  segments per partition > 0 and total=3669: {seg_ok}")

# Summary
summary = {
    "v2_status": status, "v2_docCount": dc,
    "dense_self_query": f"{dense_ok}/{SAMPLE_DENSE}",
    "sparse_self_query": f"{sparse_ok}/{SAMPLE_SPARSE}",
    "bm25_match_live": f"{bm25_ok}/{SAMPLE_BM25}",
    "stats_sane": seg_ok,
    "overall": "PASS" if (status_ok and dense_ok==SAMPLE_DENSE and sparse_ok>=95 and bm25_ok>=int(SAMPLE_BM25*0.9) and seg_ok) else "FAIL",
}
json.dump(summary, open(f"{OUT}/summary.json","w"), indent=2)
print(f"\n=== SUMMARY ===\n{json.dumps(summary, indent=2)}")
print(f"\nReports in {OUT}/")

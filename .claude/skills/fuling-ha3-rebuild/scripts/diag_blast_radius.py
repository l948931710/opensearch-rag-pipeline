"""Blast-radius: for a sample of active v2 chunks, fetch the STORED dense vector
from HA3 (include_vector=True) and compare to a FRESH embedding of the same
chunk_text via cosine. cosine~1.0 => stored vector matches text (healthy);
low => the index holds a mismatched vector (corruption). Read-only.
"""
import os, math, json, random
def _load(p):
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
_load(".env"); _load(".env.production")
os.environ.update({"RAG_ENVIRONMENT": "test", "RAG_ENV": "", "RAG_SIMULATE": "false",
    "RAG_SIMULATE_DB": "false", "RAG_SIMULATE_API": "false",
    "RAG_HA3_ENDPOINT": "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com"})
_ds = os.environ.get("RAG_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
os.environ["RAG_DASHSCOPE_API_KEY"] = _ds; os.environ["DASHSCOPE_API_KEY"] = _ds

from opensearch_pipeline import retriever
from opensearch_pipeline.config import get_config
from alibabacloud_ha3engine_vector.client import Client
from alibabacloud_ha3engine_vector.models import Config, QueryRequest

c = get_config().alibaba_vector
cli = Client(Config(endpoint=c.endpoint, instance_id=c.instance_id,
    access_user_name=c.access_user_name, access_pass_word=c.access_pass_word, protocol="HTTP"))

def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9) if na and nb else 0.0

# Sample doc_ids: the known-bad 触电 doc + random active v2 docs
import pymysql
conn = pymysql.connect(host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT","3306")),
    user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ.get("RAG_RDS_DATABASE","fuling_knowledge"), connect_timeout=8, charset="utf8mb4")
with conn.cursor() as cur:
    cur.execute("SELECT DISTINCT doc_id FROM chunk_meta WHERE is_active=1 AND version_no>=2 "
                "AND chunk_type='text_chunk'")
    all_docs = [r[0] for r in cur.fetchall()]
random.seed(7)
sample_docs = ["DOC_HR_20260514123022_FA2AD3"] + random.sample(
    [d for d in all_docs if d != "DOC_HR_20260514123022_FA2AD3"], min(9, len(all_docs)-1))
print(f"active v2 text docs: {len(all_docs)}; sampling {len(sample_docs)}")

def raw_items(resp):
    body = resp.body
    if isinstance(body, str): body = json.loads(body)
    elif hasattr(body, "to_map"): body = body.to_map()
    raw = body.get("result", body.get("hits", body.get("data", []))) if isinstance(body, dict) else []
    if isinstance(raw, dict): raw = raw.get("hits", raw.get("items", []))
    return raw if isinstance(raw, list) else []

def get_vec(item):
    f = item.get("fields", item)
    for k in ("vector", "embedding", "dense", "__vector__"):
        v = f.get(k) or item.get(k)
        if isinstance(v, str):
            try: v = json.loads(v)
            except Exception: continue
        if isinstance(v, list) and v and isinstance(v[0], (int, float)): return v
    return None

dummy = [0.0] * c.dimension if hasattr(c, "dimension") else None
healthy, corrupt, skipped = 0, 0, 0
rows = []
with conn.cursor() as cur:
    for d in sample_docs:
        cur.execute("SELECT chunk_id, chunk_text FROM chunk_meta WHERE doc_id=%s AND is_active=1 "
                    "AND version_no>=2 ORDER BY chunk_index LIMIT 3", (d,))
        meta = {cid: txt for cid, txt in cur.fetchall()}
        if not meta:
            continue
        # fetch this doc's chunks WITH stored vectors (filter pins doc; vector arg is just required)
        q = retriever.get_query_embedding(list(meta.values())[0])[0]
        req = QueryRequest(table_name=c.table_name, vector=q, top_k=20, include_vector=True,
                           filter=f'doc_id="{d}"',
                           output_fields=["id", "chunk_id", "doc_id", "chunk_text_store"])
        items = raw_items(cli.query(req))
        for it in items:
            f = it.get("fields", it)
            cid = f.get("chunk_id", "")
            txt = meta.get(cid) or f.get("chunk_text_store", "")
            stored = get_vec(it)
            if not stored or not txt:
                skipped += 1; continue
            fresh = retriever.get_query_embedding(txt)[0]
            sim = cos(stored, fresh)
            tag = "OK " if sim > 0.95 else ("LOW" if sim > 0.6 else "BAD")
            if sim > 0.95: healthy += 1
            else: corrupt += 1
            rows.append((tag, round(sim, 3), d, cid))
conn.close()

rows.sort()
print("\n  cos(stored,fresh)  doc / chunk")
for tag, sim, d, cid in rows:
    print(f"  [{tag}] {sim:>6}  {d}  {cid}")
tot = healthy + corrupt
print("\n" + "=" * 60)
print(f"sampled chunks: {tot}  | healthy(>0.95): {healthy}  | mismatched: {corrupt}  | skipped: {skipped}")
if tot:
    print(f"MISMATCH RATE: {corrupt}/{tot} = {100*corrupt/tot:.0f}%")

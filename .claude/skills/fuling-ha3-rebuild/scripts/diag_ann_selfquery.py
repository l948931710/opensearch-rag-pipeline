"""Discriminate global-ANN-failure vs per-doc-missing: self-query chunks by their
OWN text via unfiltered dense kNN. A chunk searched by its own text must be ~rank1
@~1.0 if the ANN index is healthy. Read-only.

⚠️⚠️ MUST set order="DESC" on the QueryRequest. The index is InnerProduct (higher score =
more similar), so results MUST be sorted DESCending. WITHOUT order=DESC the engine returns
ASCending (worst-first) and the score-1.0 self-match is buried at the BOTTOM (position ~N),
so top_k=500 shows top1≈0.4 and "NOT IN TOP500" — a FALSE 'HNSW is empty' signal. This exact
omission caused a multi-hour false-alarm investigation (2026-06-07). Production retriever.py
correctly uses order="DESC"; dense was never broken — the diagnostic was. ALWAYS pass order=DESC."""
import os, json
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
def items(resp):
    b = resp.body
    if isinstance(b, str): b = json.loads(b)
    elif hasattr(b, "to_map"): b = b.to_map()
    raw = b.get("result", b.get("hits", b.get("data", []))) if isinstance(b, dict) else []
    if isinstance(raw, dict): raw = raw.get("hits", raw.get("items", []))
    return raw if isinstance(raw, list) else []

import pymysql
conn = pymysql.connect(host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT","3306")),
    user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ.get("RAG_RDS_DATABASE","fuling_knowledge"), connect_timeout=8, charset="utf8mb4")
# one chunk each from: the target + a few control docs proven healthy earlier
docs = ["DOC_HR_20260514123022_FA2AD3", "DOC_IT_20260514123026_951420",
        "DOC_PRODUCTION_20260513120639_6735E8", "DOC_HR_20260514123024_8E5773"]
samples = []
with conn.cursor() as cur:
    for d in docs:
        cur.execute("SELECT chunk_id, chunk_text FROM chunk_meta WHERE doc_id=%s AND is_active=1 "
                    "AND version_no>=2 ORDER BY chunk_index LIMIT 1", (d,))
        r = cur.fetchone()
        if r: samples.append((d, r[0], r[1]))
conn.close()

print("self-query each chunk by its OWN text (unfiltered dense kNN, top500):")
for d, cid, txt in samples:
    qv = retriever.get_query_embedding(txt)[0]
    req = QueryRequest(table_name=c.table_name, vector=qv, sparse_data=None, top_k=500,
                       include_vector=False, output_fields=["doc_id", "chunk_id"],
                       order="DESC")  # ⚠️ REQUIRED for InnerProduct — see module docstring
    res = items(cli.query(req))
    rank, score = None, None
    for i, it in enumerate(res):
        f = it.get("fields", it)
        if f.get("chunk_id") == cid or f.get("doc_id") == d:
            rank, score = i + 1, it.get("score"); break
    top1 = (res[0].get("score") if res else None)
    print(f"  {d[:34]:34} self-rank={'#'+str(rank) if rank else 'NOT IN TOP500':14} "
          f"score={score}  | global_top1_score={top1}")

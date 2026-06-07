"""Read HA3 table/vector-index definition + build status, and test whether raising
the HNSW search param (ef) fixes recall. Read-only except no writes. No secrets printed."""
import os, json
def _load(p):
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
_load(".env"); _load(".env.production")
os.environ.update({"RAG_ENVIRONMENT": "test", "RAG_ENV": "", "RAG_SIMULATE": "false",
    "RAG_SIMULATE_API": "false", "RAG_HA3_ENDPOINT": "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com"})
_ds = os.environ.get("RAG_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
os.environ["RAG_DASHSCOPE_API_KEY"] = _ds; os.environ["DASHSCOPE_API_KEY"] = _ds

from opensearch_pipeline import retriever
from opensearch_pipeline.config import get_config
from alibabacloud_ha3engine_vector.client import Client
from alibabacloud_ha3engine_vector.models import Config, QueryRequest
c = get_config().alibaba_vector
cli = Client(Config(endpoint=c.endpoint, instance_id=c.instance_id,
    access_user_name=c.access_user_name, access_pass_word=c.access_pass_word, protocol="HTTP"))
TABLE = c.table_name
TARGET = "DOC_HR_20260514123022_FA2AD3"

def dump(label, fn):
    print("\n" + "=" * 72 + f"\n{label}")
    try:
        r = fn()
        b = getattr(r, "body", r)
        m = b.to_map() if hasattr(b, "to_map") else b
        print(json.dumps(m, ensure_ascii=False, indent=2, default=str)[:2500])
    except Exception as e:
        print(f"  (failed: {type(e).__name__}: {str(e)[:200]})")

dump("[get_table] vector index definition (HNSW params + distance)", lambda: cli.get_table(TABLE))
dump("[list_table_generations] full-build history / status", lambda: cli.list_table_generations(TABLE))
dump("[stats] doc / index stats", lambda: cli.stats(TABLE))
dump("[list_tasks] pending index/build tasks", lambda: cli.list_tasks())

# ── ef_search hypothesis: does a higher HNSW scan param recover recall? ──
def items(resp):
    b = resp.body
    if isinstance(b, str): b = json.loads(b)
    elif hasattr(b, "to_map"): b = b.to_map()
    raw = b.get("result", b.get("hits", b.get("data", []))) if isinstance(b, dict) else []
    if isinstance(raw, dict): raw = raw.get("hits", raw.get("items", []))
    return raw if isinstance(raw, list) else []
def target_rank(res):
    for i, it in enumerate(res):
        if (it.get("fields", it)).get("doc_id") == TARGET: return i + 1, it.get("score")
    return None, None

Q = retriever.get_query_embedding("触电应急")[0]
print("\n" + "=" * 72 + "\n[ef test] dense-only query for '触电应急', target rank under different search_params")
# try several param encodings used by HA3/Havenask HNSW (proxima)
candidates = [
    None,
    '{"ef_search":2000}',
    '{"proxima.hnsw.searcher.ef":2000}',
    '{"proxima.general.searcher.scan_ratio":0.5}',
    '{"proxima.hnsw.searcher.ef":2000,"proxima.general.searcher.scan_ratio":0.5}',
]
for sp in candidates:
    try:
        kw = dict(table_name=TABLE, vector=Q, sparse_data=None, top_k=500,
                  include_vector=False, output_fields=["doc_id", "chunk_id"])
        if sp is not None: kw["search_params"] = sp
        res = items(cli.query(QueryRequest(**kw)))
        rk, sc = target_rank(res)
        top1 = res[0].get("score") if res else None
        print(f"  search_params={str(sp):60} -> target={('#'+str(rk)+' @'+str(round(sc,3))) if rk else 'NOT IN TOP500':18} top1={top1} n={len(res)}")
    except Exception as e:
        print(f"  search_params={str(sp):60} -> ERROR {type(e).__name__}: {str(e)[:120]}")

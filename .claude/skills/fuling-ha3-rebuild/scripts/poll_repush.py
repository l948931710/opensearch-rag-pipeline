"""Poll Stage-3 re-push progress. RDS: active chunks INDEXED vs pending vs failed.
HA3: docCount + live BM25 hit count. DONE when pending==0 and docCount>0."""
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
import pymysql
from opensearch_pipeline import retriever
from opensearch_pipeline.config import get_config
from alibabacloud_ha3engine_vector.client import Client
from alibabacloud_ha3engine_vector.models import Config, SearchRequest, TextQuery, RankQuery
c = get_config().alibaba_vector
cli = Client(Config(endpoint=c.endpoint, instance_id=c.instance_id,
    access_user_name=c.access_user_name, access_pass_word=c.access_pass_word, protocol="HTTP"))
def tomap(r):
    b = getattr(r, "body", r)
    if isinstance(b, str):
        try: return json.loads(b)
        except Exception: return b
    return b.to_map() if hasattr(b, "to_map") else b
def docc():
    try:
        m = tomap(cli.stats(c.table_name)); return (m.get("result", {}) or {}).get("totalDocCount")
    except Exception as e:
        return f"ERR:{str(e)[:25]}"
def bm25():
    try:
        tq = TextQuery(query_string=f"{c.text_search_field}:'管理'", query_params={"default_op": "OR"})
        return len(retriever._parse_ha3_response(cli.search(SearchRequest(
            table_name=c.table_name, text=tq, rank=RankQuery(), size=5, output_fields=["doc_id"]))))
    except Exception as e:
        return f"ERR:{str(e)[:25]}"
conn = pymysql.connect(host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT", "3306")),
    user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge"), connect_timeout=8, charset="utf8mb4")
with conn.cursor() as cur:
    cur.execute("SELECT index_status, COUNT(*) FROM chunk_meta WHERE is_active=1 GROUP BY index_status")
    d = dict(cur.fetchall())
conn.close()
indexed = d.get("INDEXED", 0); pending = d.get("NOT_INDEXED", 0) + d.get("PROCESSING", 0); failed = d.get("FAILED", 0)
dc = docc(); hits = bm25()
print(f"INDEXED={indexed}/3669 pending={pending} failed={failed} | HA3_docCount={dc} bm25_hits={hits}")
print("DONE" if (pending == 0 and isinstance(dc, int) and dc > 0) else "WORKING")

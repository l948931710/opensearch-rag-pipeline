"""Replicate the PRODUCTION 3-way weighted hybrid query (retriever.py): Dense+Sparse kNN (w=0.7)
fused with BM25 on chunk_text (w=0.3) via SearchRequest/client.search(). Public HTTP endpoint.
Creds from .env files. No permission filter (pure relevance test)."""
import os, sys, json
def le(p):
    d={}
    if os.path.exists(p):
        for ln in open(p,encoding="utf-8"):
            ln=ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k,v=ln.split("=",1); d[k.strip()]=v.strip().strip('"').strip("'")
    return d
e={}; e.update(le(".env")); e.update(le(".env.production"))
import requests
QUERY = sys.argv[1] if len(sys.argv)>1 else "触电了怎么应急处理"
DS=e.get("DASHSCOPE_API_KEY") or e.get("RAG_DASHSCOPE_API_KEY")
USER=e.get("RAG_HA3_USER"); PWD=e.get("RAG_HA3_PASSWORD"); TABLE=e.get("RAG_HA3_TABLE_NAME","fuling_kb_chunks")
KNN_W, TEXT_W, KNN_TOPK, TEXT_FIELD = 0.7, 0.3, 100, "chunk_text"

def esc(t): return t.replace("'"," ").replace("\\","\\\\").replace('"','\\"').strip()

# native dense+sparse query embedding
u="https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
r=requests.post(u,json={"model":"text-embedding-v4","input":{"texts":[QUERY]},"parameters":{"dimension":1024,"output_type":"dense&sparse"}},headers={"Authorization":f"Bearer {DS}"},timeout=30); r.raise_for_status()
emb=r.json()["output"]["embeddings"][0]; dense=emb["embedding"]
sp=sorted(emb.get("sparse_embedding",[]),key=lambda x:x["index"]); sidx=[s["index"] for s in sp]; sval=[float(s["value"]) for s in sp]

from alibabacloud_ha3engine_vector.client import Client
from alibabacloud_ha3engine_vector.models import Config, QueryRequest, SparseData, SearchRequest, TextQuery, RankQuery
cli=Client(Config(endpoint="ha-cn-kgl4slr1n01.public.ha.aliyuncs.com",instance_id="ha-cn-kgl4slr1n01",access_user_name=USER,access_pass_word=PWD,protocol="HTTP"))
sd=SparseData(count=[len(sidx)],indices=sidx,values=sval) if sidx else None
knn=QueryRequest(table_name=TABLE, vector=dense, sparse_data=sd, top_k=KNN_TOPK, include_vector=False); knn.weight=KNN_W
txt=TextQuery(query_string=f"{TEXT_FIELD}:'{esc(QUERY)}'", query_params={"default_op":"OR"}); txt.weight=TEXT_W
req=SearchRequest(table_name=TABLE, knn=knn, text=txt, rank=RankQuery(), size=5, order="DESC",
    output_fields=["id","doc_id","chunk_text_store","title","section_title","chunk_type","owner_dept","permission_level"])
resp=cli.search(req)
body=resp.body
if isinstance(body,str): body=json.loads(body)
elif hasattr(body,"to_map"): body=body.to_map()
res=body.get("result") or body.get("hits") or body.get("data") or []
if isinstance(res,dict): res=res.get("hits") or res.get("items") or []
print(f"===== PROD weighted hybrid (knn {KNN_W}/text {TEXT_W}, BM25 on {TEXT_FIELD})  query='{QUERY}' =====")
if not res: print("NO RESULTS. raw:",json.dumps(body,ensure_ascii=False)[:1500]); sys.exit(0)
for i,it in enumerate(res):
    f=it.get("fields",it)
    print(f"[{i+1}] score={it.get('score',it.get('_score'))} | {f.get('title')} / {f.get('section_title')} | type={f.get('chunk_type')} dept={f.get('owner_dept')}")
    print(f"     {str(f.get('chunk_text_store',f.get('chunk_text','')))[:200]}")
print("\nDONE")

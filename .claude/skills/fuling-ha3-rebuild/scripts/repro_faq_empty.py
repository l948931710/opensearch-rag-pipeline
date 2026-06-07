"""Reproduce the faq=0-chunks bug: download the actual canonical of EMPTY docs from OSS,
inspect block structure, and run each chunk mode to see which yields chunks. Read-only."""
import os, json
def le(p):
    d={}
    if os.path.exists(p):
        for ln in open(p,encoding="utf-8"):
            ln=ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k,v=ln.split("=",1); d[k.strip()]=v.strip().strip('"').strip("'")
    return d
e={}; e.update(le(".env")); e.update(le(".env.production"))
import pymysql, oss2, sys
sys.path.insert(0, ".")
from opensearch_pipeline.chunker import DocumentChunker

DOCS = ["DOC_HR_20260513120631_CFF6E8", "DOC_HR_20260514123026_F54673", "DOC_HR_20260514123022_FA2AD3"]
c=pymysql.connect(host=e["RAG_RDS_HOST"],port=int(e.get("RAG_RDS_PORT","3306")),user=e["RAG_RDS_USER"],
 password=e["RAG_RDS_PASSWORD"],database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),charset="utf8mb4",connect_timeout=8,read_timeout=30)
keys={}
with c.cursor() as cur:
    fmt=",".join(["%s"]*len(DOCS))
    cur.execute(f"SELECT doc_id, version_no, canonical_json_key FROM document_version WHERE doc_id IN ({fmt}) AND version_no>=2", tuple(DOCS))
    for d_,v_,k_ in cur.fetchall(): keys[d_]=(v_,k_)
c.close()

auth=oss2.Auth(e["RAG_OSS_ACCESS_KEY_ID"], e["RAG_OSS_ACCESS_KEY_SECRET"])
bucket=oss2.Bucket(auth, "https://oss-cn-hangzhou.aliyuncs.com", e.get("RAG_OSS_BUCKET_NAME","fuling-knowledge-base"))

def chunk_count(blocks, doc_id, ver, mode, xlsx="normal_spreadsheet"):
    try:
        ck=DocumentChunker(split_mode=mode, min_chunk_chars=50, xlsx_layout_type=xlsx)
        return len(ck.chunk_from_blocks(blocks, doc_id, ver, {"title":"t"}))
    except Exception as ex:
        return f"ERR:{type(ex).__name__}:{ex}"

for d_ in DOCS:
    ver,key=keys.get(d_,(None,None))
    if not key: print(f"\n{d_}: no key"); continue
    data=json.loads(bucket.get_object(key).read().decode("utf-8"))
    blocks=data.get("blocks",[])
    bt={}
    for b in blocks:
        t=b.get("block_type","?") if isinstance(b,dict) else getattr(b,"block_type","?")
        bt[t]=bt.get(t,0)+1
    print(f"\n=== {d_} v{ver} | text_len={len(data.get('text','') or '')} | blocks={len(blocks)} | types={bt}")
    # sample first 3 block texts
    for b in blocks[:3]:
        tt=(b.get("text","") if isinstance(b,dict) else getattr(b,"text","")) or ""
        bt_=b.get("block_type","?") if isinstance(b,dict) else getattr(b,"block_type","?")
        print(f"    [{bt_}] {tt[:70]!r}")
    print(f"    chunks: faq={chunk_count(blocks,d_,ver,'faq')} | clause={chunk_count(blocks,d_,ver,'clause')} | "
          f"text={chunk_count(blocks,d_,ver,'text')} | step={chunk_count(blocks,d_,ver,'step')}")
print("\nDONE")

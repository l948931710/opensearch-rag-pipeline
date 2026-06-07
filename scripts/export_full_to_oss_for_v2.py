"""Export all 3,669 active chunks to a single JSONL file on OSS, ready for the v2
table's offline build. Per-record shape mirrors Chunk.to_ha3_doc() exactly (the same
mapping production push uses today).

Output: oss://fuling-knowledge-base/opensearch/fuling-kb-chunks-v2-<ts>/data.json

Run AFTER tier1_preflight.py has confirmed cache complete + drift OK.
"""
import os, sys, json, hashlib, datetime
def _load(p):
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
_load(".env"); _load(".env.production")

import pymysql, oss2
TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
OSS_DIR = f"opensearch/fuling-kb-chunks-v2-{TS}"
OSS_KEY = f"{OSS_DIR}/data.json"
LOCAL = f"scratch/export_v2_{TS}.json"
print(f"local={LOCAL}\noss=oss://{os.environ['RAG_OSS_BUCKET_NAME']}/{OSS_KEY}")

cache = json.load(open("scratch/embedding_cache.json"))
EMB_MODEL = "text-embedding-v4"
def ckey(t): return hashlib.md5(f"{EMB_MODEL}_{t}".encode()).hexdigest()

conn = pymysql.connect(host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT","3306")),
    user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ.get("RAG_RDS_DATABASE","fuling_knowledge"), connect_timeout=8, charset="utf8mb4")
with conn.cursor() as c:
    c.execute("""SELECT cm.id, cm.chunk_id, cm.doc_id, cm.version_no, cm.chunk_index, cm.page_num,
                        cm.section_title, cm.chunk_type, cm.chunk_text, cm.permission_level, cm.owner_dept,
                        cm.category_l1, cm.category_l2, cm.source_url, cm.extra_json,
                        COALESCE(dm.title, '') AS title
                 FROM chunk_meta cm LEFT JOIN document_meta dm ON cm.doc_id = dm.doc_id
                 WHERE cm.is_active=1""")
    rows = c.fetchall()
conn.close()
print(f"rows: {len(rows)}")

# Write JSONL
written = 0; missing = 0
with open(LOCAL, "w", encoding="utf-8") as f:
    for r in rows:
        (rds_id, chunk_id, doc_id, version_no, chunk_index, page_num, section_title, chunk_type,
         chunk_text, perm, dept, cat1, cat2, src_url, extra_json, title) = r
        if not chunk_text:
            missing += 1; continue
        k = ckey(chunk_text); dense = cache.get(k); sp = cache.get(f"sp_{k}", {}) or {}
        if not dense or len(dense) != 1024:
            missing += 1; continue
        extra = {}
        if extra_json:
            try: extra = json.loads(extra_json) if isinstance(extra_json,str) else (extra_json or {})
            except Exception: extra = {}
        # Mirror Chunk.to_ha3_doc()
        fields = {
            "id": int(rds_id),
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "version_no": int(version_no),
            "chunk_index": int(chunk_index or 0),
            "page_num": int(page_num or 0),
            "section_title": section_title or "",
            "chunk_type": [chunk_type] if chunk_type else ["text_chunk"],
            "chunk_text": chunk_text,
            "chunk_text_store": chunk_text,
            "dense_vector": dense,
            "sparse_vector_indices": sp.get("indices", []),
            "sparse_vector_values":  sp.get("values", []),
            "permission_level": perm or "public",
            "owner_dept": dept or "",
            "category_l1": cat1 or "",
            "category_l2": cat2 or "",
            "is_active": 1,
            "kb_type": "public" if (perm or "public")=="public" else "private",
            "title": title or "",
            "source_url": src_url or "",
            "source_image": extra.get("source_image") or "",
            "visual_summary": extra.get("visual_summary") or "",
        }
        f.write(json.dumps({"cmd":"add","fields": fields}, ensure_ascii=False) + "\n")
        written += 1
print(f"wrote {written} records ({missing} skipped) to {LOCAL} ({os.path.getsize(LOCAL)//(1024*1024)} MB)")

# Upload to OSS
ak=os.environ["RAG_OSS_ACCESS_KEY_ID"]; sk=os.environ["RAG_OSS_ACCESS_KEY_SECRET"]
bucket=oss2.Bucket(oss2.Auth(ak,sk), "oss-cn-hangzhou.aliyuncs.com", os.environ["RAG_OSS_BUCKET_NAME"])
print(f"uploading -> oss://{os.environ['RAG_OSS_BUCKET_NAME']}/{OSS_KEY}")
bucket.put_object_from_file(OSS_KEY, LOCAL)
print(f"DONE.\n\nWhen creating fuling_kb_chunks_v2 in the HA3 console, set the OSS data source path to:")
print(f"  bucket:  {os.environ['RAG_OSS_BUCKET_NAME']}")
print(f"  ossPath: /{OSS_KEY}")

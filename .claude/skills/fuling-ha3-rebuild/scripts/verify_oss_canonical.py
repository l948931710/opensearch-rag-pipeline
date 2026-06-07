"""Verify DAG1 output: every rebuilt doc's canonical file exists & is non-empty in OSS,
cross-referenced against RDS canonical_json_key. Reads creds from .env files.
OSS read via PUBLIC endpoint (laptop can't reach the -internal one). Read-only."""
import os, json
def load_env(p):
    d = {}
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); d[k.strip()] = v.strip().strip('"').strip("'")
    return d
r = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
e = {}; e.update(load_env(os.path.join(r, ".env"))); e.update(load_env(os.path.join(r, ".env.production")))

import pymysql, oss2
# 1) rebuilt rows that finished extraction (the 398): NOT_STARTED + canonical set + ext!=doc
conn = pymysql.connect(host=e["RAG_RDS_HOST"], port=int(e.get("RAG_RDS_PORT","3306")), user=e["RAG_RDS_USER"],
    password=e["RAG_RDS_PASSWORD"], database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),
    charset="utf8mb4", connect_timeout=8, read_timeout=40)
with conn.cursor() as c:
    c.execute("SELECT doc_id, version_no, canonical_json_key, file_ext FROM document_version "
              "WHERE status='active' AND content_process_status='NOT_STARTED' "
              "AND canonical_json_key IS NOT NULL AND file_ext NOT IN ('doc')")
    rows = c.fetchall()
conn.close()
print(f"[oss-verify] rebuilt rows with canonical_json_key: {len(rows)} (expect 398)")

# 2) OSS via PUBLIC endpoint
pub_ep = "https://oss-cn-hangzhou.aliyuncs.com"
auth = oss2.Auth(e["RAG_OSS_ACCESS_KEY_ID"], e["RAG_OSS_ACCESS_KEY_SECRET"])
bucket = oss2.Bucket(auth, pub_ep, e.get("RAG_OSS_BUCKET_NAME", "fuling-knowledge-base"))

present = empty = missing = 0
missing_keys = []; empty_keys = []
for doc_id, ver, key, ext in rows:
    try:
        meta = bucket.head_object(key)
        if meta.content_length and meta.content_length > 0:
            present += 1
        else:
            empty += 1; empty_keys.append(key)
    except oss2.exceptions.NoSuchKey:
        missing += 1; missing_keys.append((doc_id, ver, key))
    except Exception as ex:
        missing += 1; missing_keys.append((doc_id, ver, f"{key} ERR:{ex}"))

print(f"[oss-verify] canonical present & non-empty: {present}/{len(rows)} | empty: {empty} | missing: {missing}")
if missing_keys:
    print("  MISSING (first 10):")
    for x in missing_keys[:10]: print("   ", x)
if empty_keys:
    print("  EMPTY (first 10):", empty_keys[:10])

# 3) spot-check 5: parse canonical JSON, report structure stats (no content dumped)
print("\n[oss-verify] structure spot-check (5 samples):")
for doc_id, ver, key, ext in rows[:5]:
    try:
        data = json.loads(bucket.get_object(key).read().decode("utf-8"))
        blocks = data.get("blocks", [])
        txt = data.get("text", "") or ""
        assets = data.get("assets", [])
        btypes = {}
        for b in blocks:
            t = b.get("type", "?"); btypes[t] = btypes.get(t, 0) + 1
        print(f"   {doc_id} v{ver} [{ext}]: blocks={len(blocks)} {btypes} | text_len={len(txt)} | assets={len(assets)} | method={data.get('extract_method')}")
    except Exception as ex:
        print(f"   {doc_id} v{ver}: PARSE ERROR {ex}")
print("\n[oss-verify] DONE")

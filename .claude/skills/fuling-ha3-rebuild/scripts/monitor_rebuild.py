"""Read-only live monitor of the rebuild. Tracks queue + liveness (recent updated_at).
Stage-1 canonical commits per ~100-doc batch, so S1_pending drops in steps of ~100.
Creds from .env files."""
import os, sys, time
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
import pymysql
CFG = dict(host=e["RAG_RDS_HOST"], port=int(e.get("RAG_RDS_PORT","3306")), user=e["RAG_RDS_USER"],
           password=e["RAG_RDS_PASSWORD"], database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),
           charset="utf8mb4", connect_timeout=8, read_timeout=25)
Q = ("SELECT "
     "(SELECT COUNT(*) FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NULL AND file_ext NOT IN ('doc')),"
     "(SELECT COUNT(*) FROM document_version WHERE status='active' AND content_process_status='FAILED'),"
     "(SELECT COUNT(*) FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NOT NULL AND file_ext NOT IN ('doc')),"
     "(SELECT COUNT(*) FROM chunk_meta WHERE is_active=1 AND index_status IN ('NOT_INDEXED','FAILED')),"
     "(SELECT COUNT(*) FROM document_version WHERE status='active' AND version_no>=2 AND updated_at > NOW() - INTERVAL 10 MINUTE)")
prev = None; dead = 0; i = 0
print("[monitor] watching rebuild. S1 starts 398, drops in ~100-step batches. polling 90s.", flush=True)
while True:
    i += 1
    try:
        conn = pymysql.connect(**CFG)
        with conn.cursor() as c:
            c.execute(Q); s1, failed, s2, s3, live = c.fetchone()
        conn.close()
    except Exception as ex:
        print(f"[poll {i}] DB error: {ex}", flush=True); time.sleep(90); continue
    line = (f"[poll {i}] S1_pending={s1} (extracted {398-s1}/398) | awaiting_chunk={s2} | "
            f"S3_not_indexed={s3} | FAILED={failed} | live(updated<10min)={live}")
    if s1 != prev or i % 6 == 1:
        print(line, flush=True)
    if s1 == 0:
        print(f"✅ STAGE1_DRAINED — all 398 extracted. FAILED={failed}. "
              f"Next: run opensearch_stage2_safe_chunk. (I'll then verify OSS canonical outputs.)", flush=True)
        break
    # NOTE: extraction writes nothing to RDS for ~25-40min/batch, so flat S1 + live=0 is NORMAL.
    # Only flag if flat for ~60min with zero DB writes the whole time (genuinely abnormal).
    dead = dead + 1 if (s1 == prev and live == 0) else 0
    prev = s1
    if dead == 40:
        print(f"⚠️ S1_pending={s1} flat with no DB writes for ~60min, FAILED={failed}. "
              f"A VLM-heavy batch can be slow, but worth checking the DataWorks IDE run log for an error.", flush=True)
    time.sleep(90)

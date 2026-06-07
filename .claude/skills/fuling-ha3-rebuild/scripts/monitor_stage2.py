"""Read-only Stage-2 monitor: classify + chunk over the 398. Creds from .env files.
Tracks awaiting_chunk (NOT_STARTED+canonical) draining 398->0, in-flight batch,
new-version chunks accumulating, and FAILED. Terminal when awaiting==0 and inflight==0."""
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
     "(SELECT COUNT(*) FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NOT NULL AND file_ext NOT IN ('doc')),"
     "(SELECT COUNT(*) FROM document_version WHERE status='active' AND version_no>=2 AND content_process_status IN ('LOADING','PROCESSING')),"
     "(SELECT COUNT(*) FROM document_version WHERE status='active' AND version_no>=2 AND content_process_status='DONE'),"
     "(SELECT COUNT(*) FROM document_version WHERE status='active' AND version_no>=2 AND content_process_status='FAILED'),"
     "(SELECT COUNT(*) FROM chunk_meta WHERE is_active=1 AND version_no>=2)")
prev = None; i = 0
print("[stage2] watching classify+chunk. awaiting starts 398. polling 90s.", flush=True)
while True:
    i += 1
    try:
        conn = pymysql.connect(**CFG)
        with conn.cursor() as c:
            c.execute(Q); awaiting, inflight, done, failed, chunks = c.fetchone()
        conn.close()
    except Exception as ex:
        print(f"[poll {i}] DB error: {ex}", flush=True); time.sleep(90); continue
    line = (f"[poll {i}] awaiting_chunk={awaiting} | in_flight={inflight} | done(v>=2)={done} | "
            f"FAILED={failed} | new_chunks={chunks}")
    if (awaiting, inflight, chunks) != prev or i % 6 == 1:
        print(line, flush=True)
    if awaiting == 0 and inflight == 0:
        print(f"✅ STAGE2_DRAINED — chunking complete. new_chunks={chunks}, FAILED={failed}. "
              f"Next: run 清理stage3 (embed + push to HA3 + swap).", flush=True)
        break
    prev = (awaiting, inflight, chunks)
    time.sleep(90)

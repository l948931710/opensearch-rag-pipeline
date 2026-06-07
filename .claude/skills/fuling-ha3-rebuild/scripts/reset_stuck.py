"""Reset stuck rebuild rows (v>=2 in LOADING/PROCESSING from the failed Stage-2 run) back to
NOT_STARTED so the re-run re-claims them. Scoped to v>=2 only (never touches live v1 docs).
Creds from .env files. Default preview; --commit to apply."""
import os, sys
def le(p):
    d = {}
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); d[k.strip()] = v.strip().strip('"').strip("'")
    return d
e = {}; e.update(le(".env")); e.update(le(".env.production"))
COMMIT = "--commit" in sys.argv
import pymysql
c = pymysql.connect(host=e["RAG_RDS_HOST"], port=int(e.get("RAG_RDS_PORT","3306")), user=e["RAG_RDS_USER"],
    password=e["RAG_RDS_PASSWORD"], database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),
    charset="utf8mb4", connect_timeout=8, read_timeout=60, autocommit=False)
WHERE = ("status='active' AND version_no>=2 AND content_process_status IN ('LOADING','PROCESSING')")
try:
    with c.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM document_version WHERE {WHERE}")
        n = cur.fetchone()[0]
        print(f"[reset] stuck rebuild rows to reset -> NOT_STARTED: {n}")
        if not (0 <= n <= 398):
            print(f"[reset] ABORT: unexpected count {n}"); sys.exit(2)
        if not COMMIT:
            print("[reset] PREVIEW ONLY — re-run with --commit."); sys.exit(0)
        cur.execute(f"UPDATE document_version SET content_process_status='NOT_STARTED', content_process_error=NULL WHERE {WHERE}")
        print(f"[reset] updated: {cur.rowcount}")
        c.commit()
        cur.execute("SELECT COUNT(*) FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NOT NULL AND file_ext NOT IN ('doc')")
        print(f"[reset] Stage-2 queue now (expect 398): {cur.fetchone()[0]}")
finally:
    c.close()
print("[reset] DONE")

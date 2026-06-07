"""Reset the v>=2 rebuild docs that produced 0 chunks (chunk_status='EMPTY') back to
NOT_STARTED so Stage 2 reprocesses them under the new PII policy. Scoped to v>=2 EMPTY
only — never touches the 384 already-chunked docs or live v1. Creds from .env. --commit to apply."""
import os, sys
def le(p):
    d={}
    if os.path.exists(p):
        for ln in open(p,encoding="utf-8"):
            ln=ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k,v=ln.split("=",1); d[k.strip()]=v.strip().strip('"').strip("'")
    return d
e={}; e.update(le(".env")); e.update(le(".env.production"))
COMMIT="--commit" in sys.argv
import pymysql
c=pymysql.connect(host=e["RAG_RDS_HOST"],port=int(e.get("RAG_RDS_PORT","3306")),user=e["RAG_RDS_USER"],
 password=e["RAG_RDS_PASSWORD"],database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),charset="utf8mb4",
 connect_timeout=8,read_timeout=60,autocommit=False)
WHERE="status='active' AND version_no>=2 AND chunk_status='EMPTY'"
try:
    with c.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM document_version WHERE {WHERE}")
        n=cur.fetchone()[0]
        print(f"[reset-empty] EMPTY rebuild docs to requeue -> NOT_STARTED: {n}")
        if not (0<=n<=40):
            print(f"[reset-empty] ABORT unexpected count {n}"); sys.exit(2)
        if not COMMIT:
            print("[reset-empty] PREVIEW ONLY — re-run with --commit."); sys.exit(0)
        cur.execute(f"UPDATE document_version SET content_process_status='NOT_STARTED', chunk_status='NOT_STARTED', content_process_error=NULL WHERE {WHERE}")
        print(f"[reset-empty] updated: {cur.rowcount}")
        c.commit()
        cur.execute("SELECT COUNT(*) FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NOT NULL AND file_ext NOT IN ('doc')")
        print(f"[reset-empty] Stage-2 re-run queue now: {cur.fetchone()[0]} (these reprocess; the 384 DONE are untouched)")
finally:
    c.close()
print("[reset-empty] DONE")

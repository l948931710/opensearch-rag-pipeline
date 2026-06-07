"""Read-only RDS pre-flight for the HA3 rebuild.
Reads RDS credentials from .env / .env.production (never hardcoded here).
Runs SELECT-only counts; mutates nothing.
"""
import os
import sys


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
e = {}
e.update(load_env(os.path.join(root, ".env")))
e.update(load_env(os.path.join(root, ".env.production")))

host = e.get("RAG_RDS_HOST")
port = int(e.get("RAG_RDS_PORT", "3306"))
user = e.get("RAG_RDS_USER")
pw = e.get("RAG_RDS_PASSWORD")
db = e.get("RAG_RDS_DATABASE", "fuling_knowledge")

if not (host and user and pw):
    print("MISSING RDS creds in .env/.env.production:",
          {k: bool(e.get(k)) for k in ("RAG_RDS_HOST", "RAG_RDS_USER", "RAG_RDS_PASSWORD")})
    sys.exit(2)

try:
    import pymysql
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "PyMySQL", "-q"])
    import pymysql

print(f"[preflight] connecting {host}:{port}/{db} as {user} (READ-ONLY) ...")
conn = pymysql.connect(host=host, port=port, user=user, password=pw, database=db,
                       charset="utf8mb4", connect_timeout=8, read_timeout=30)


def q(label, sql):
    with conn.cursor() as c:
        c.execute(sql)
        rows = c.fetchall()
    print(f"\n## {label}")
    for r in rows:
        print("   ", r)


q("ACTIVE_DOCS", "SELECT COUNT(DISTINCT doc_id) FROM document_version WHERE status='active'")
q("STATE_DIST (content_process_status, index_status, n)",
  "SELECT content_process_status,index_status,COUNT(*) FROM document_version WHERE status='active' "
  "GROUP BY content_process_status,index_status ORDER BY 3 DESC")
q("STALE_LOCKS (expect none)",
  "SELECT doc_id,version_no,content_process_status,index_status,updated_at FROM document_version "
  "WHERE status='active' AND (content_process_status IN ('PROCESSING','LOADING') OR index_status='PROCESSING')")
q("ACTIVE_CHUNKS", "SELECT COUNT(*) FROM chunk_meta WHERE is_active=1")
q("INACTIVE_CHUNKS", "SELECT COUNT(*) FROM chunk_meta WHERE is_active=0")
q("VERSION_DIST (current max version -> #docs)",
  "SELECT version_no,COUNT(*) FROM (SELECT doc_id,MAX(version_no) version_no FROM document_version "
  "WHERE status='active' GROUP BY doc_id) t GROUP BY version_no ORDER BY version_no")
q("QUARANTINE_VERSIONS",
  "SELECT COUNT(*) FROM document_version WHERE status='active' AND LOCATE('/_quarantine/',raw_key)>0")
q("PENDING_BULK_JOBS", "SELECT COUNT(*) FROM opensearch_bulk_job WHERE status='PENDING'")
q("TOP_VERSION_NOT_SUCCESS",
  "SELECT COUNT(*) FROM document_version dv JOIN (SELECT doc_id,MAX(version_no) mv FROM document_version "
  "WHERE status='active' GROUP BY doc_id) m ON dv.doc_id=m.doc_id AND dv.version_no=m.mv "
  "WHERE dv.status='active' AND dv.index_status<>'SUCCESS'")
conn.close()
print("\n[preflight] DONE")

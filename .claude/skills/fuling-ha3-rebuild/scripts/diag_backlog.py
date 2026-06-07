"""Read-only: characterize the 243 pre-existing NOT_STARTED docs (no DONE version).
Creds from .env/.env.production (never hardcoded)."""
import os
def load_env(p):
    d={}
    if os.path.exists(p):
        for ln in open(p,encoding="utf-8"):
            ln=ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k,v=ln.split("=",1); d[k.strip()]=v.strip().strip('"').strip("'")
    return d
r=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
e={}; e.update(load_env(os.path.join(r,".env"))); e.update(load_env(os.path.join(r,".env.production")))
import pymysql
conn=pymysql.connect(host=e["RAG_RDS_HOST"],port=int(e.get("RAG_RDS_PORT","3306")),user=e["RAG_RDS_USER"],
    password=e["RAG_RDS_PASSWORD"],database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),
    charset="utf8mb4",connect_timeout=8,read_timeout=30)
# "backlog" = active NOT_STARTED canonical-null rows whose doc_id has NO DONE version
BACKLOG="""FROM document_version dv WHERE dv.status='active'
 AND dv.content_process_status='NOT_STARTED' AND dv.canonical_json_key IS NULL
 AND dv.doc_id NOT IN (SELECT doc_id FROM document_version WHERE content_process_status='DONE')"""
def q(label,sql):
    with conn.cursor() as c:
        c.execute(sql); rows=c.fetchall()
    print(f"\n## {label}")
    for x in rows: print("   ",x)
q("BACKLOG count (expect 243)", f"SELECT COUNT(*) {BACKLOG}")
q("BACKLOG by file_ext", f"SELECT dv.file_ext,COUNT(*) {BACKLOG} GROUP BY dv.file_ext ORDER BY 2 DESC")
q("BACKLOG by version_no", f"SELECT dv.version_no,COUNT(*) {BACKLOG} GROUP BY dv.version_no")
q("BACKLOG raw_key samples (top-level folder)", f"SELECT SUBSTRING_INDEX(dv.raw_key,'/',2) folder,COUNT(*) {BACKLOG} GROUP BY folder ORDER BY 2 DESC")
q("BACKLOG raw_key first 15", f"SELECT dv.doc_id,dv.raw_key {BACKLOG} ORDER BY dv.created_at LIMIT 15")
conn.close(); print("\n[diag] DONE")

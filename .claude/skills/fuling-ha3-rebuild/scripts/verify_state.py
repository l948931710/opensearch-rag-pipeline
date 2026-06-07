"""Read-only: confirm current RDS state of the rebuild seed. Creds from .env files."""
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
c=pymysql.connect(host=e["RAG_RDS_HOST"],port=int(e.get("RAG_RDS_PORT","3306")),user=e["RAG_RDS_USER"],
  password=e["RAG_RDS_PASSWORD"],database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),charset="utf8mb4",
  connect_timeout=8,read_timeout=30)
def q(label,sql):
    with c.cursor() as cur: cur.execute(sql); rows=cur.fetchall()
    print(f"\n## {label}")
    for x in rows: print("   ",x)
q("Rebuild targets: NEW rows NOT_STARTED & canonical NULL & ext!=doc (expect 398)",
  "SELECT COUNT(*) FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NULL AND file_ext NOT IN ('doc')")
q("Sample of NEW rows (doc_id, ver, cps, canonical_json_key)",
  "SELECT doc_id,version_no,content_process_status,canonical_json_key FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NULL AND version_no>=2 ORDER BY doc_id LIMIT 4")
q("OLD versions still serving (DONE/SUCCESS count, should be ~380, untouched)",
  "SELECT content_process_status,index_status,COUNT(*) FROM document_version WHERE status='active' GROUP BY content_process_status,index_status ORDER BY 3 DESC")
c.close(); print("\nDONE")

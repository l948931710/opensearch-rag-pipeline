"""Read-only Stage-2 output verification. Creds from .env files."""
import os
def le(p):
    d={}
    if os.path.exists(p):
        for ln in open(p,encoding="utf-8"):
            ln=ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k,v=ln.split("=",1); d[k.strip()]=v.strip().strip('"').strip("'")
    return d
e={}; e.update(le(".env")); e.update(le(".env.production"))
import pymysql
c=pymysql.connect(host=e["RAG_RDS_HOST"],port=int(e.get("RAG_RDS_PORT","3306")),user=e["RAG_RDS_USER"],
 password=e["RAG_RDS_PASSWORD"],database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),charset="utf8mb4",connect_timeout=8,read_timeout=40)
def q(l,s):
    with c.cursor() as cur: cur.execute(s); print(f"\n## {l}"); [print("  ",r) for r in cur.fetchall()]
q("Stage-3 queue: new-version chunks awaiting index (NOT_INDEXED/FAILED, is_active=1, v>=2)",
  "SELECT COUNT(*) FROM chunk_meta WHERE is_active=1 AND version_no>=2 AND index_status IN ('NOT_INDEXED','FAILED')")
q("chunk_type distribution (new-version chunks)",
  "SELECT chunk_type,COUNT(*) FROM chunk_meta WHERE is_active=1 AND version_no>=2 GROUP BY chunk_type ORDER BY 2 DESC")
q("document_version chunk_status across rebuild docs (v>=2)",
  "SELECT chunk_status,COUNT(*) FROM document_version WHERE status='active' AND version_no>=2 GROUP BY chunk_status")
q("rebuild docs that produced 0 chunks (EMPTY) — doc_id, ext, chunk_status",
  "SELECT dv.doc_id, dv.file_ext, dv.chunk_status FROM document_version dv "
  "WHERE dv.status='active' AND dv.version_no>=2 AND dv.chunk_status='EMPTY'")
q("edge docs chunk counts (jpeg + empty docx)",
  "SELECT doc_id, version_no, COUNT(*) chunks FROM chunk_meta WHERE doc_id IN "
  "('DOC_ADMIN_20260513120214_A8CB90','DOC_ADMIN_20260513120213_14D1C1') AND version_no>=2 GROUP BY doc_id, version_no")
q("step_card chunks present? (the new step-card logic)",
  "SELECT COUNT(*) step_cards FROM chunk_meta WHERE is_active=1 AND version_no>=2 AND chunk_type='step_card'")
c.close(); print("\nDONE")

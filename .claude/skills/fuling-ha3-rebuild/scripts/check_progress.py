"""Read-only finer-grained progress check. Creds from .env files."""
import os
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
c = pymysql.connect(host=e["RAG_RDS_HOST"], port=int(e.get("RAG_RDS_PORT","3306")), user=e["RAG_RDS_USER"],
    password=e["RAG_RDS_PASSWORD"], database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),
    charset="utf8mb4", connect_timeout=8, read_timeout=30)
def q(label, sql):
    with c.cursor() as cur: cur.execute(sql); rows = cur.fetchall()
    print(f"\n## {label}")
    for x in rows: print("   ", x)
# Among the rebuild rows (version>=2), how far has extraction persisted?
q("rebuild rows (v>=2) by content_process_status / extraction_status / canonical set?",
  "SELECT content_process_status, extraction_status, (canonical_json_key IS NOT NULL) AS has_canonical, COUNT(*) "
  "FROM document_version WHERE status='active' AND version_no>=2 "
  "GROUP BY content_process_status, extraction_status, has_canonical")
# the specific doc from the IDE log
q("DOC_PRODUCTION_20260513120635_328126 — all versions",
  "SELECT version_no, content_process_status, extraction_status, index_status, (canonical_json_key IS NOT NULL) has_canon, updated_at "
  "FROM document_version WHERE doc_id='DOC_PRODUCTION_20260513120635_328126' ORDER BY version_no")
# recently-updated rebuild rows (did anything get touched in last few min?)
q("rebuild rows updated in last 15 min",
  "SELECT COUNT(*) FROM document_version WHERE status='active' AND version_no>=2 AND updated_at > NOW() - INTERVAL 15 MINUTE")
c.close(); print("\nDONE")

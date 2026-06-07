"""Read-only: extraction-content quality across the 398 rebuilt docs (RDS text_length). Creds from .env."""
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
    charset="utf8mb4", connect_timeout=8, read_timeout=40)
BASE = ("FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' "
        "AND canonical_json_key IS NOT NULL AND file_ext NOT IN ('doc')")
def q(label, sql):
    with c.cursor() as cur: cur.execute(sql); rows = cur.fetchall()
    print(f"\n## {label}")
    for x in rows: print("   ", x)
q("text_length buckets across the 398",
  f"SELECT CASE WHEN text_length IS NULL OR text_length=0 THEN '0 (empty)' "
  f"WHEN text_length<50 THEN '1-49' WHEN text_length<200 THEN '50-199' "
  f"WHEN text_length<1000 THEN '200-999' ELSE '1000+' END AS bucket, COUNT(*), file_ext "
  f"{BASE} GROUP BY bucket, file_ext ORDER BY bucket, COUNT(*) DESC")
q("EMPTY extractions (text_length=0 or NULL) — doc_id, ext, page_count",
  f"SELECT doc_id, file_ext, page_count, ocr_status {BASE} AND (text_length IS NULL OR text_length=0) ORDER BY file_ext")
q("totals: docs, empty, nonempty",
  f"SELECT COUNT(*) total, SUM(text_length IS NULL OR text_length=0) empty, SUM(text_length>0) nonempty {BASE}")
c.close(); print("\nDONE")

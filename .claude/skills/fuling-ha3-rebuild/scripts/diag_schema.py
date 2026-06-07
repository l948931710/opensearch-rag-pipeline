"""Read-only diagnostics: live document_version constraints + how v2 was created.
Creds read from .env/.env.production (never hardcoded)."""
import os, sys

def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
e = {}; e.update(load_env(os.path.join(root, ".env"))); e.update(load_env(os.path.join(root, ".env.production")))
import pymysql
conn = pymysql.connect(host=e["RAG_RDS_HOST"], port=int(e.get("RAG_RDS_PORT","3306")), user=e["RAG_RDS_USER"],
                       password=e["RAG_RDS_PASSWORD"], database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),
                       charset="utf8mb4", connect_timeout=8, read_timeout=30)
def q(label, sql):
    with conn.cursor() as c:
        c.execute(sql); rows = c.fetchall()
    print(f"\n## {label}")
    for r in rows: print("   ", r)

# 1. all UNIQUE keys on document_version
q("UNIQUE keys on document_version",
  "SELECT INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) cols FROM information_schema.STATISTICS "
  "WHERE TABLE_SCHEMA='fuling_knowledge' AND TABLE_NAME='document_version' AND NON_UNIQUE=0 GROUP BY INDEX_NAME")
# 2. verify rollback: stage-1 queue should still be 243
q("Stage-1 queue now (NOT_STARTED & canonical NULL) — expect 243",
  "SELECT COUNT(*) FROM document_version WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NULL")
q("Total active version rows now",
  "SELECT COUNT(*) FROM document_version WHERE status='active'")
# 3. the existing v2 doc: do v1 and v2 differ in raw_key?
q("Versions of DOC_ADMIN_20260509102839_76AFFC (raw_key per version)",
  "SELECT version_no, content_process_status, index_status, raw_key, raw_key_hash FROM document_version "
  "WHERE doc_id='DOC_ADMIN_20260509102839_76AFFC' ORDER BY version_no")
conn.close()
print("\n[diag] DONE")

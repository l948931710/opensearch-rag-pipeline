"""Version-bump seed for the HA3 rebuild (in-place versioned swap).

Bumps every doc whose CURRENT MAX version is content_process_status='DONE'
to a new version_no+1 row (NOT_STARTED, canonical NULL, copy raw_key) so Stage 1
re-extracts it with the new logic; Stage 3 then swaps old->new. Excludes _quarantine.
Docs whose top version is NOT_STARTED are left alone (they process fresh).

Creds are read from .env / .env.production (never hardcoded here).

Usage:
  python3 scratch/seed_versions.py            # PREVIEW only (read-only)
  python3 scratch/seed_versions.py --commit    # execute INSERT + meta bump (transaction)
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

host = e.get("RAG_RDS_HOST"); port = int(e.get("RAG_RDS_PORT", "3306"))
user = e.get("RAG_RDS_USER"); pw = e.get("RAG_RDS_PASSWORD")
db = e.get("RAG_RDS_DATABASE", "fuling_knowledge")
COMMIT = "--commit" in sys.argv

try:
    import pymysql
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "PyMySQL", "-q"])
    import pymysql

# Candidate selector: top (max) version per active doc, where that top version is DONE,
# excluding quarantine paths.
TOP = ("SELECT doc_id, MAX(version_no) mv FROM document_version WHERE status='active' GROUP BY doc_id")
WHERE = ("dv.status='active' AND dv.content_process_status='DONE' "
         "AND LOCATE('/_quarantine/', dv.raw_key)=0")

PREVIEW = (
    f"SELECT dv.doc_id, dv.version_no cur_ver, dv.version_no+1 new_ver, dv.index_status, dv.file_ext "
    f"FROM document_version dv JOIN ({TOP}) m ON dv.doc_id=m.doc_id AND dv.version_no=m.mv "
    f"WHERE {WHERE} ORDER BY dv.doc_id"
)
INSERT = (
    "INSERT INTO document_version "
    "(doc_id, version_no, bucket_name, raw_key, raw_key_hash, file_ext, "
    " gate_status, content_process_status, chunk_status, index_status, status) "
    "SELECT dv.doc_id, dv.version_no+1, dv.bucket_name, dv.raw_key, "
    "       SHA2(CONCAT(dv.raw_key, '#v', dv.version_no+1), 256), dv.file_ext, "
    "       'pending_clean','NOT_STARTED','NOT_STARTED','NOT_INDEXED','active' "
    f"FROM document_version dv JOIN ({TOP}) m ON dv.doc_id=m.doc_id AND dv.version_no=m.mv "
    f"WHERE {WHERE}"
)
META = (
    "UPDATE document_meta dm "
    f"JOIN ({TOP}) m ON dm.doc_id=m.doc_id "
    "SET dm.current_version_no = m.mv"
)
READY = ("SELECT COUNT(*) FROM document_version "
         "WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NULL")

conn = pymysql.connect(host=host, port=port, user=user, password=pw, database=db,
                       charset="utf8mb4", connect_timeout=8, read_timeout=60, autocommit=False)
try:
    with conn.cursor() as c:
        c.execute(PREVIEW)
        rows = c.fetchall()
        print(f"[seed] candidates to version-bump: {len(rows)}")
        for r in rows[:10]:
            print("   sample:", r)
        if len(rows) > 10:
            print(f"   ... and {len(rows)-10} more")

        if not COMMIT:
            print("\n[seed] PREVIEW ONLY — no changes written. Re-run with --commit to apply.")
        else:
            c.execute(INSERT)
            inserted = c.rowcount
            c.execute(META)
            meta_upd = c.rowcount
            conn.commit()
            print(f"\n[seed] COMMITTED: inserted {inserted} new version rows; document_meta bumped {meta_upd} rows.")
            c.execute(READY)
            print("[seed] rows now NOT_STARTED & canonical NULL (Stage-1 queue):", c.fetchone()[0])
finally:
    conn.close()

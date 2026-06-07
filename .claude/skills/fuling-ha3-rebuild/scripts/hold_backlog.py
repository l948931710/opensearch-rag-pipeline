"""Set the 243 legacy/backlog docs (NOT_STARTED, no DONE version) to content_process_status='HOLD'
so the rebuild processes exactly the 398 bumped versions. Reversible.
Creds from .env/.env.production (never hardcoded). Default = preview; --commit to apply."""
import os, sys
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
COMMIT = "--commit" in sys.argv
import pymysql
conn = pymysql.connect(host=e["RAG_RDS_HOST"], port=int(e.get("RAG_RDS_PORT","3306")), user=e["RAG_RDS_USER"],
    password=e["RAG_RDS_PASSWORD"], database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),
    charset="utf8mb4", connect_timeout=8, read_timeout=60, autocommit=False)
SEL = ("SELECT dv.id FROM document_version dv WHERE dv.status='active' "
       "AND dv.content_process_status='NOT_STARTED' AND dv.canonical_json_key IS NULL "
       "AND dv.doc_id NOT IN (SELECT DISTINCT doc_id FROM document_version WHERE content_process_status='DONE')")
try:
    with conn.cursor() as c:
        c.execute(SEL)
        ids = [row[0] for row in c.fetchall()]
        print(f"[hold] backlog rows to HOLD: {len(ids)}")
        if not (0 < len(ids) <= 300):
            print(f"[hold] ABORT: unexpected count {len(ids)} (safety bound 1..300)"); sys.exit(2)
        if not COMMIT:
            print("[hold] PREVIEW ONLY — re-run with --commit to apply.")
        else:
            fmt = ",".join(["%s"] * len(ids))
            c.execute(f"UPDATE document_version SET content_process_status='HOLD' WHERE id IN ({fmt})", tuple(ids))
            print(f"[hold] updated rows: {c.rowcount}")
            conn.commit()
            c.execute("SELECT COUNT(*) FROM document_version WHERE status='active' "
                      "AND content_process_status='NOT_STARTED' AND canonical_json_key IS NULL "
                      "AND file_ext NOT IN ('doc')")
            print("[hold] effective Stage-1 queue now (expect 398):", c.fetchone()[0])
finally:
    conn.close()
print("[hold] DONE")

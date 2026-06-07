"""Recovery step 1: reset ACTIVE chunks' index_status INDEXED->NOT_INDEXED so Stage 3
(清理stage3) re-pushes them to the emptied HA3 index. RDS currently says INDEXED but HA3
is empty (split-brain after the failed reindex) — this corrects RDS to reality.
Non-destructive: only flips a status field on is_active=1 chunks. Preview by default;
pass --commit to write. No secrets printed.
"""
import os, sys
def _load(p):
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
_load(".env"); _load(".env.production")
import pymysql
COMMIT = "--commit" in sys.argv
conn = pymysql.connect(host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT", "3306")),
    user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge"), connect_timeout=8, charset="utf8mb4")
with conn.cursor() as c:
    print("== chunk_meta WHERE is_active=1, by index_status ==")
    c.execute("SELECT index_status, COUNT(*) FROM chunk_meta WHERE is_active=1 GROUP BY index_status")
    for s, n in c.fetchall(): print(f"   {s}: {n}")

    c.execute("SELECT COUNT(*) FROM chunk_meta WHERE is_active=1 AND index_status='INDEXED'")
    target = c.fetchone()[0]
    print(f"\n-> would reset {target} active chunks  INDEXED -> NOT_INDEXED")

    print("\n== parent document_version (status='active') by index_status (must NOT be PROCESSING for Stage 3 to pick up) ==")
    c.execute("""SELECT dv.index_status, COUNT(*) FROM document_version dv
                 WHERE dv.status='active' GROUP BY dv.index_status""")
    for s, n in c.fetchall(): print(f"   {s}: {n}")

    if COMMIT:
        c.execute("UPDATE chunk_meta SET index_status='NOT_INDEXED' "
                  "WHERE is_active=1 AND index_status='INDEXED'")
        n1 = c.rowcount
        # any active-doc version stuck in PROCESSING would block selection -> clear to NOT_INDEXED
        c.execute("""UPDATE document_version SET index_status='NOT_INDEXED'
                     WHERE status='active' AND index_status='PROCESSING'""")
        n2 = c.rowcount
        conn.commit()
        print(f"\nCOMMITTED: {n1} chunks -> NOT_INDEXED; {n2} stuck-PROCESSING versions cleared.")
        c.execute("SELECT index_status, COUNT(*) FROM chunk_meta WHERE is_active=1 GROUP BY index_status")
        print("   verify chunk_meta is_active=1:", dict(c.fetchall()))
    else:
        print("\n(PREVIEW only — no changes written. Re-run with --commit to apply.)")
conn.close()

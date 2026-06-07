"""Read-only Stage-3 monitor: embed + push to HA3 + versioned swap. Creds from .env.
Tracks new-version chunks indexing (3666->0), old v1 chunks deactivating (the swap), FAILED.
Terminal when s3_pending==0 (all new chunks indexed)."""
import os, sys, time
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
CFG=dict(host=e["RAG_RDS_HOST"],port=int(e.get("RAG_RDS_PORT","3306")),user=e["RAG_RDS_USER"],
 password=e["RAG_RDS_PASSWORD"],database=e.get("RAG_RDS_DATABASE","fuling_knowledge"),charset="utf8mb4",connect_timeout=8,read_timeout=25)
Q=("SELECT "
   "(SELECT COUNT(*) FROM chunk_meta WHERE is_active=1 AND version_no>=2 AND index_status IN ('NOT_INDEXED','FAILED')),"
   "(SELECT COUNT(*) FROM chunk_meta WHERE is_active=1 AND version_no>=2 AND index_status='INDEXED'),"
   "(SELECT COUNT(*) FROM document_version WHERE status='active' AND version_no>=2 AND index_status='SUCCESS'),"
   "(SELECT COUNT(*) FROM chunk_meta WHERE is_active=1 AND version_no=1),"          # old v1 chunks still active (swap pending)
   "(SELECT COUNT(*) FROM chunk_meta WHERE version_no>=2 AND index_status='FAILED')")
START=3666
prev=None; i=0
print(f"[stage3] watching embed+push+swap. queue~{START} chunks. polling 90s.", flush=True)
while True:
    i+=1
    try:
        conn=pymysql.connect(**CFG)
        with conn.cursor() as c:
            c.execute(Q); pend,indexed,dvok,oldv1,failed=c.fetchone()
        conn.close()
    except Exception as ex:
        print(f"[poll {i}] DB error: {ex}", flush=True); time.sleep(90); continue
    line=(f"[poll {i}] s3_pending={pend} | indexed={indexed} | dv_SUCCESS={dvok}/395 | "
          f"old_v1_active={oldv1} (was 3901→swap) | FAILED={failed}")
    if (pend,indexed,oldv1)!=prev or i%6==1:
        print(line, flush=True)
    if pend==0 and indexed>0:
        print(f"✅ STAGE3 INDEXED — new chunks indexed={indexed}, dv_SUCCESS={dvok}, old_v1_active={oldv1}, FAILED={failed}. Verifying swap next.", flush=True)
        break
    prev=(pend,indexed,oldv1)
    time.sleep(90)

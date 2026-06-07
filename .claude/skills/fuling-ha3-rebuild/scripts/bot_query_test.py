"""
Settle the relevance question by running the REAL production /api/ask serving core
(retrieve_and_enrich -> generate_answer) over the laptop-reachable PUBLIC endpoints.

This is the faithful test G16 calls for: same retrieval code (3-way weighted hybrid
+ neighbor-stitch + step-card expansion + cover-page demotion) and same LLM answer-gen
as the DingTalk bot. The ONLY deltas vs prod are network routing, forced by the laptop
not being in the VPC:
  - HA3 endpoint -> public domain, protocol=HTTP   (G2)
  - DashScope    -> public domain (RAG_ENVIRONMENT=test, not the VPC domain)
  - RDS          -> public endpoint from .env.production
Models still resolve to Qwen (DashScope key present) — no Gemini, no behavior change.

No secrets are printed. Creds read from .env / .env.production.
Usage:  python scratch/bot_query_test.py ["query"]
"""
import os, sys

# ── 1. Load .env then .env.production into os.environ (production wins) ──
def _load(p):
    if not os.path.exists(p):
        return
    for ln in open(p, encoding="utf-8"):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")

_load(".env")
_load(".env.production")

# ── 2. Force the laptop-reachable, non-simulate, public-routing setup ──
PUBLIC_HA3 = "ha-cn-kgl4slr1n01.public.ha.aliyuncs.com"
os.environ["RAG_ENVIRONMENT"] = "test"        # public DashScope domain + skip prod guard (key->Qwen anyway)
os.environ["RAG_ENV"] = ""                     # don't re-trigger .env.{env} import-time load
os.environ["RAG_SIMULATE"] = "false"
for k in ("RAG_SIMULATE_DB", "RAG_SIMULATE_OPENSEARCH", "RAG_SIMULATE_OSS", "RAG_SIMULATE_API"):
    os.environ[k] = "false"
os.environ["RAG_HA3_ENDPOINT"] = PUBLIC_HA3
# mirror dashscope key under both names config looks for
_ds = os.environ.get("RAG_DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
if _ds:
    os.environ["RAG_DASHSCOPE_API_KEY"] = _ds
    os.environ["DASHSCOPE_API_KEY"] = _ds

QUERY = sys.argv[1] if len(sys.argv) > 1 else "触电了怎么应急处理"

# ── 3. Import serving core AFTER env is set, then inject protocol=HTTP on the HA3 client ──
from opensearch_pipeline import retriever
from opensearch_pipeline.config import get_config

def _http_ha3_client():
    if retriever._ha3_client is not None:
        return retriever._ha3_client
    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config
    cfg = get_config().alibaba_vector
    ep = cfg.endpoint.replace("http://", "").replace("https://", "")
    retriever._ha3_client = Client(Config(
        endpoint=ep, instance_id=cfg.instance_id,
        access_user_name=cfg.access_user_name, access_pass_word=cfg.access_pass_word,
        protocol="HTTP",                          # public endpoint serves on HTTP/80 (G2)
    ))
    return retriever._ha3_client

retriever._get_ha3_client = _http_ha3_client

cfg = get_config()
print("=" * 78)
print(f"CONFIG  env={cfg.environment} simulate={cfg.simulate} (db={cfg.simulate_db} os={cfg.simulate_opensearch} api={cfg.simulate_api})")
print(f"        HA3={cfg.alibaba_vector.endpoint} table={cfg.alibaba_vector.table_name} fusion={cfg.alibaba_vector.hybrid_fusion} knn/text={cfg.alibaba_vector.knn_weight}/{cfg.alibaba_vector.text_weight}")
print(f"        LLM={cfg.llm.model}  embed={cfg.embedding.model}")
print(f"        RDS_host={os.environ.get('RAG_RDS_HOST','?')}  (used for neighbor-stitch/step-expand)")
print("=" * 78)

# ── 4. Discover the target SOP's permission so we query as a real employee would ──
import pymysql
target_doc_ids = set()
target_dept = None
target_perm = None
try:
    conn = pymysql.connect(
        host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT", "3306")),
        user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
        database=os.environ.get("RAG_RDS_DATABASE", "fuling_knowledge"),
        connect_timeout=8, charset="utf8mb4",
    )
    with conn.cursor() as c:
        c.execute(
            "SELECT doc_id, title, owner_dept, permission_level, current_version_no "
            "FROM document_meta WHERE title LIKE %s", ("%触电%",))
        rows = c.fetchall()
        print(f"\n[document_meta] docs matching title LIKE '%触电%': {len(rows)}")
        for doc_id, title, dept, perm, cur in rows:
            target_doc_ids.add(doc_id)
            target_dept = target_dept or dept
            target_perm = target_perm or perm
            print(f"  - {doc_id} | {title!r} | owner_dept={dept} permission={perm} cur_ver={cur}")
            c.execute(
                "SELECT version_no, COUNT(*), SUM(is_active=1), "
                "GROUP_CONCAT(DISTINCT chunk_type) "
                "FROM chunk_meta WHERE doc_id=%s GROUP BY version_no ORDER BY version_no", (doc_id,))
            for ver, n, act, types in c.fetchall():
                print(f"      v{ver}: {n} chunks, {act} active | types={types}")
    conn.close()
except Exception as e:
    print(f"\n[RDS discovery] skipped/failed ({type(e).__name__}: {str(e)[:120]})")

# ── 5. Run the REAL retrieval+generation path, as a user in the doc's dept ──
def run(label, user_dept):
    print("\n" + "#" * 78)
    print(f"# {label}  (user_dept={user_dept!r})  query={QUERY!r}")
    print("#" * 78)
    try:
        chunks = retriever.retrieve_and_enrich(QUERY, top_k=7, user_dept=user_dept)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"RETRIEVAL FAILED: {type(e).__name__}: {e}")
        return
    print(f"\nretrieve_and_enrich returned {len(chunks)} chunks (after stitch + step-expand):")
    hit_rank = None
    for i, ch in enumerate(chunks):
        mark = ""
        if ch.get("doc_id") in target_doc_ids:
            mark = "  <<< TARGET 触电 SOP"
            if hit_rank is None:
                hit_rank = i + 1
        print(f" [{i+1}] score={ch.get('score'):.3f} | {ch.get('title')} / {ch.get('section_title')} "
              f"| type={ch.get('chunk_type')} perm={ch.get('permission_level')} dept={ch.get('owner_dept')}{mark}")
        print(f"      {str(ch.get('chunk_text',''))[:160].replace(chr(10),' ')}")
    print(f"\n>>> 触电 SOP rank among retrieved chunks: "
          f"{('#'+str(hit_rank)) if hit_rank else 'NOT RETRIEVED'}")

    # LLM answer (exactly what /api/ask returns)
    from opensearch_pipeline.llm_generator import generate_answer
    try:
        res = generate_answer(QUERY, chunks, max_tokens=1024, temperature=0.1)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"GENERATION FAILED: {type(e).__name__}: {e}")
        return
    print("\n----- ANSWER (model=%s) -----" % res.get("model"))
    print(res["answer"])
    print("\n----- SOURCES cited -----")
    for s in res.get("sources", []):
        m = "  <<< 触电 SOP" if s.get("doc_id") in target_doc_ids else ""
        print(f"  - {s.get('doc_id')} | {s.get('title')} / {s.get('section')} (score={s.get('score')}){m}")

# As an employee in the SOP's own department (the realistic asker)
run("FULL PRODUCTION PATH — as employee in SOP's dept", target_dept)

# Also show what a generic public-only user sees (permission_level='public' filter)
if target_perm and str(target_perm).lower() != "public":
    run("FULL PRODUCTION PATH — generic public-only user", None)

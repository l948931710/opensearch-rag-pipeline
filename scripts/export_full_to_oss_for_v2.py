"""⛔ T12-TOMBSTONE(2026-07-31,node-ACL 阶段 A)—— 本脚本已**永久停用**,直接执行即退出。

原用途:把全部 active chunk 导成一份 JSONL 上 OSS,供 v2 表离线构建(一次性迁移,已完成)。

为什么封禁:下方 `fields` 是**手工镜像** `Chunk.to_ha3_doc()` 的结果,它
  · 把 `chunk_meta.owner_dept` 原样写进 `owner_dept` —— 而那是【检索投影轴】,node 模式
    文档在该列上是哨兵 `__acl_node_mode_v1__`;
  · **完全不带 `allowed_depts`** —— node 文档的 `d:`/`dx:` 授权值全部丢失。
两者合起来:用本脚本重建出来的表里,node 文档【谁也搜不到】;而若在投影收敛前跑,又会把
真实 owner 固化进新表、legacy owner 分支静默复活 = 权限重开。手工镜像必然随
`to_ha3_doc` 漂移,这正是 T12 要消灭的一类入口(设计稿 §9.1)。

要重新启用:**不要**改这里的字段字典。走受控路径 —— 投影值一律由
`acl_policy.project_doc_acl` 产出、记录形状一律由 `Chunk.to_ha3_doc(include_allowed_depts=True)`
产出,并同步更新 `tests/test_acl_projection_writers.py` 的 allowlist。

设计稿:`docs/permission_node_acl_design_2026-07-27_DRAFT.md` §9.1。
"""
# 无条件拦在一切副作用之前 —— **连 import 都不放行**:Python 的 import 会执行任意模块级
# 副作用，而本脚本下方原本就在 import 期读 .env.production 并连生产 RDS。
raise SystemExit(
    "⛔ scripts/export_full_to_oss_for_v2.py 已按 T12 永久停用:手工镜像 to_ha3_doc 会丢 "
    "allowed_depts 并把 node 哨兵 owner 固化进新表(重建后 node 文档无人可见)。\n"
    "   需要离线重建请改走 project_doc_acl + Chunk.to_ha3_doc(include_allowed_depts=True)。"
)

import os
import json
import hashlib
import datetime


def _load(p):
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1); os.environ[k.strip()] = v.strip().strip('"').strip("'")
_load(".env"); _load(".env.production")

import pymysql
import oss2
TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
OSS_DIR = f"opensearch/fuling-kb-chunks-v2-{TS}"
OSS_KEY = f"{OSS_DIR}/data.json"
LOCAL = f"scratch/export_v2_{TS}.json"
print(f"local={LOCAL}\noss=oss://{os.environ['RAG_OSS_BUCKET_NAME']}/{OSS_KEY}")

cache = json.load(open("scratch/embedding_cache.json"))
EMB_MODEL = "text-embedding-v4"
def ckey(t): return hashlib.md5(f"{EMB_MODEL}_{t}".encode()).hexdigest()


def _rds_ssl_kwargs():
    """P0-02/B3：显式 TLS 语义——配 RAG_RDS_SSL_CA 即验证 TLS，未配显式明文
    （堵 pymysql 2.x PREFERRED 随客户端 OpenSSL 漂移；与 prod_access 同语义）。"""
    ca = (os.environ.get("RAG_RDS_SSL_CA") or "").strip()
    if not ca:
        return {"ssl_disabled": True}
    verify = (os.environ.get("RAG_RDS_SSL_VERIFY_CERT", "true").strip().lower()
              not in ("0", "false", "no"))
    return {"ssl_ca": ca, "ssl_verify_cert": verify, "ssl_verify_identity": verify}
    # ⚠️ 只用顶层 ssl_* 参数,绝不与 ssl={...} 字典混传:pymysql 见任一顶层参数为真即用
    # 顶层参数【重建】ssl 配置并丢弃字典(ca 变 None→退到系统信任库,ApsaraDB 链必挂
    # "unable to get local issuer";2026-07-21 SAE 首开 CA 实弹踩坑)。


conn = pymysql.connect(host=os.environ["RAG_RDS_HOST"], port=int(os.environ.get("RAG_RDS_PORT","3306")),
    user=os.environ["RAG_RDS_USER"], password=os.environ["RAG_RDS_PASSWORD"],
    database=os.environ.get("RAG_RDS_DATABASE","fuling_knowledge"), connect_timeout=8, charset="utf8mb4",
    **_rds_ssl_kwargs())
with conn.cursor() as c:
    c.execute("""SELECT cm.id, cm.chunk_id, cm.doc_id, cm.version_no, cm.chunk_index, cm.page_num,
                        cm.section_title, cm.chunk_type, cm.chunk_text, cm.permission_level, cm.owner_dept,
                        cm.category_l1, cm.category_l2, cm.source_url, cm.extra_json,
                        COALESCE(dm.title, '') AS title
                 FROM chunk_meta cm LEFT JOIN document_meta dm ON cm.doc_id = dm.doc_id
                 WHERE cm.is_active=1""")
    rows = c.fetchall()
conn.close()
print(f"rows: {len(rows)}")

# Write JSONL
written = 0; missing = 0
with open(LOCAL, "w", encoding="utf-8") as f:
    for r in rows:
        (rds_id, chunk_id, doc_id, version_no, chunk_index, page_num, section_title, chunk_type,
         chunk_text, perm, dept, cat1, cat2, src_url, extra_json, title) = r
        if not chunk_text:
            missing += 1; continue
        k = ckey(chunk_text); dense = cache.get(k); sp = cache.get(f"sp_{k}", {}) or {}
        if not dense or len(dense) != 1024:
            missing += 1; continue
        extra = {}
        if extra_json:
            try: extra = json.loads(extra_json) if isinstance(extra_json,str) else (extra_json or {})
            except Exception: extra = {}
        # Mirror Chunk.to_ha3_doc()
        fields = {
            "id": int(rds_id),
            "chunk_id": chunk_id,
            "doc_id": doc_id,
            "version_no": int(version_no),
            "chunk_index": int(chunk_index or 0),
            "page_num": int(page_num or 0),
            "section_title": section_title or "",
            "chunk_type": [chunk_type] if chunk_type else ["text_chunk"],
            "chunk_text": chunk_text,
            "chunk_text_store": chunk_text,
            "dense_vector": dense,
            "sparse_vector_indices": sp.get("indices", []),
            "sparse_vector_values":  sp.get("values", []),
            "permission_level": perm or "public",
            "owner_dept": dept or "",
            "category_l1": cat1 or "",
            "category_l2": cat2 or "",
            "is_active": 1,
            "kb_type": "public" if (perm or "public")=="public" else "private",
            "title": title or "",
            "source_url": src_url or "",
            "source_image": extra.get("source_image") or "",
            "visual_summary": extra.get("visual_summary") or "",
        }
        f.write(json.dumps({"cmd":"add","fields": fields}, ensure_ascii=False) + "\n")
        written += 1
print(f"wrote {written} records ({missing} skipped) to {LOCAL} ({os.path.getsize(LOCAL)//(1024*1024)} MB)")

# Upload to OSS
ak=os.environ["RAG_OSS_ACCESS_KEY_ID"]; sk=os.environ["RAG_OSS_ACCESS_KEY_SECRET"]
bucket=oss2.Bucket(oss2.Auth(ak,sk), "oss-cn-hangzhou.aliyuncs.com", os.environ["RAG_OSS_BUCKET_NAME"])
print(f"uploading -> oss://{os.environ['RAG_OSS_BUCKET_NAME']}/{OSS_KEY}")
bucket.put_object_from_file(OSS_KEY, LOCAL)
print("DONE.\n\nWhen creating fuling_kb_chunks_v2 in the HA3 console, set the OSS data source path to:")
print(f"  bucket:  {os.environ['RAG_OSS_BUCKET_NAME']}")
print(f"  ossPath: /{OSS_KEY}")

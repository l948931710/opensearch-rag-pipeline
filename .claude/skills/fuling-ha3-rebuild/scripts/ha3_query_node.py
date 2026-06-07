# -*- coding: utf-8 -*-
"""
PyODPS 3 节点 — HA3 检索冒烟测试（在 DataStudio 里新建一个 PyODPS3 节点，粘贴本脚本，运行）。
在 VPC 内运行，走内网 API入口，无需公网。把日志贴回即可。

只需从你的『清理stage3』节点复制两处凭证：DASHSCOPE_API_KEY 和 HA3_PASSWORD。
"""
import subprocess, sys, json
subprocess.check_call([sys.executable, "-m", "pip", "install",
                       "alibabacloud_ha3engine_vector", "requests", "-t", "/tmp/pp", "-q"])
sys.path.insert(0, "/tmp/pp")
import requests

# ── 凭证（与你的 stage 节点一致；从『清理stage3』复制这两个值）──
DASHSCOPE_API_KEY = "<PASTE_DASHSCOPE_API_KEY>"        # 同 stage 节点
HA3_ENDPOINT   = "ha-cn-kgl4slr1n01.ha.aliyuncs.com"   # 内网 API入口（VPC 内可达）
HA3_INSTANCE   = "ha-cn-kgl4slr1n01"
HA3_USER       = "<PASTE_HA3_USER>"                    # 同 stage 节点
HA3_PASSWORD   = "<PASTE_HA3_PASSWORD>"                 # 同 stage 节点
TABLE          = "fuling_kb_chunks"

QUERY = "触电了怎么应急处理"
DIM, MODEL, TOPK = 1024, "text-embedding-v4", 5

# 1) 查询向量：原生 dense+sparse（与入库一致，才能对齐稀疏向量）
u = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
r = requests.post(u, json={"model": MODEL, "input": {"texts": [QUERY]},
                           "parameters": {"dimension": DIM, "output_type": "dense&sparse"}},
                  headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}"}, timeout=30)
r.raise_for_status()
emb = r.json()["output"]["embeddings"][0]
dense = emb["embedding"]
sp = sorted(emb.get("sparse_embedding", []), key=lambda x: x["index"])
sidx = [s["index"] for s in sp]; sval = [float(s["value"]) for s in sp]
print(f"query='{QUERY}'  dense={len(dense)}d  sparse_nonzero={len(sidx)}")

# 2) HA3 三路混合检索（dense kNN + sparse），内网直连
from alibabacloud_ha3engine_vector.client import Client
from alibabacloud_ha3engine_vector.models import Config, QueryRequest, SparseData
cli = Client(Config(endpoint=HA3_ENDPOINT, instance_id=HA3_INSTANCE,
                    access_user_name=HA3_USER, access_pass_word=HA3_PASSWORD))
sd = SparseData(count=[len(sidx)], indices=sidx, values=sval) if sidx else None
resp = cli.query(QueryRequest(table_name=TABLE, vector=dense, sparse_data=sd, top_k=TOPK,
                              include_vector=False,
                              output_fields=["id", "doc_id", "chunk_text_store", "title",
                                             "section_title", "chunk_type", "owner_dept"]))
body = resp.body
if isinstance(body, str): body = json.loads(body)
elif hasattr(body, "to_map"): body = body.to_map()
res = body.get("result") or body.get("hits") or body.get("data") or []
if isinstance(res, dict): res = res.get("hits") or res.get("items") or []
print(f"\n===== TOP {TOPK} =====")
if not res:
    print("NO RESULTS. raw:", json.dumps(body, ensure_ascii=False)[:1500]); sys.exit(0)
for i, it in enumerate(res):
    f = it.get("fields", it)
    print(f"[{i+1}] score={it.get('score', it.get('_score'))} | type={f.get('chunk_type')} | "
          f"{f.get('title')} / {f.get('section_title')}")
    print(f"     {str(f.get('chunk_text_store', f.get('chunk_text','')))[:200]}")
print("\nDONE")

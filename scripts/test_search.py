# -*- coding: utf-8 -*-
"""
OpenSearch 向量检索版 — 搜索验证脚本
用法: python scripts/test_search.py "员工住宿怎么申请"
"""
import sys
import json
import requests

# ── 配置（从环境变量读取） ──
import os
HA3_ENDPOINT = os.environ.get("RAG_HA3_ENDPOINT", "")
HA3_INSTANCE_ID = os.environ.get("RAG_HA3_INSTANCE_ID", "")
HA3_USER = os.environ.get("RAG_HA3_USER", "")
HA3_PASSWORD = os.environ.get("RAG_HA3_PASSWORD", "")
TABLE_NAME = os.environ.get("RAG_HA3_TABLE_NAME", "fuling_kb_chunks")

DASHSCOPE_API_KEY = None  # 会从 .env 读取
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIM = 1024
TOP_K = 5


def load_api_key():
    """从 .env 读取 DashScope API Key"""
    global DASHSCOPE_API_KEY
    import os
    # 先看环境变量
    DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("RAG_EMBEDDING_API_KEY")
    if DASHSCOPE_API_KEY:
        return
    # 再看 .env 文件
    env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DASHSCOPE_API_KEY=") or line.startswith("RAG_EMBEDDING_API_KEY="):
                    DASHSCOPE_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return
    if not DASHSCOPE_API_KEY:
        print("❌ 找不到 DashScope API Key，请设置环境变量 DASHSCOPE_API_KEY")
        sys.exit(1)


def get_embedding(query_text):
    """调用 DashScope 获取 dense + sparse embedding"""
    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    resp = requests.post(url, json={
        "model": EMBEDDING_MODEL,
        "input": [query_text],
        "dimensions": EMBEDDING_DIM,
        "output_type": "dense&sparse"
    }, headers={
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json"
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()["data"][0]

    dense = data["embedding"]
    sparse = data.get("sparse_embedding", {})
    sparse_indices = []
    sparse_values = []
    if sparse:
        sorted_pairs = sorted(sparse.items(), key=lambda x: int(x[0]))
        sparse_indices = [int(k) for k, v in sorted_pairs]
        sparse_values = [float(v) for k, v in sorted_pairs]

    return dense, sparse_indices, sparse_values


def search_opensearch(dense_vector, sparse_indices, sparse_values):
    """查询 OpenSearch 向量检索版"""
    from alibabacloud_ha3engine_vector.client import Client
    from alibabacloud_ha3engine_vector.models import Config, QueryRequest, SparseData

    config = Config(
        endpoint=HA3_ENDPOINT,
        instance_id=HA3_INSTANCE_ID,
        access_user_name=HA3_USER,
        access_pass_word=HA3_PASSWORD,
    )
    client = Client(config)

    # 构建稀疏数据
    sparse_data = None
    if sparse_indices:
        sparse_data = SparseData(
            count=[len(sparse_indices)],
            indices=sparse_indices,
            values=sparse_values,
        )

    request = QueryRequest(
        table_name=TABLE_NAME,
        vector=dense_vector,
        sparse_data=sparse_data,
        top_k=TOP_K,
        include_vector=False,
        output_fields=["id", "doc_id", "chunk_text", "title", "section_title", "category_l1"],
    )

    resp = client.query(request)
    return resp


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/test_search.py \"你的搜索问题\"")
        sys.exit(1)

    query = sys.argv[1]
    print(f"\n🔍 查询: {query}\n")

    # 1. 获取 embedding
    load_api_key()
    print("⏳ 生成查询向量 (dense + sparse)...")
    dense, sparse_idx, sparse_val = get_embedding(query)
    print(f"  ✅ Dense: {len(dense)} 维, Sparse: {len(sparse_idx)} 个非零项\n")

    # 2. 搜索 OpenSearch
    print("⏳ 查询 OpenSearch...")
    resp = search_opensearch(dense, sparse_idx, sparse_val)

    # 3. 解析结果
    body = resp.body
    if isinstance(body, str):
        body = json.loads(body)
    elif hasattr(body, 'to_map'):
        body = body.to_map()

    print(f"\n{'='*70}")
    print(f"  搜索结果 (Top {TOP_K})")
    print(f"{'='*70}\n")

    # 尝试解析不同的响应格式
    results = []
    if isinstance(body, dict):
        results = body.get("result", body.get("hits", body.get("data", [])))
        if isinstance(results, dict):
            results = results.get("hits", results.get("items", []))

    if not results:
        print("  ⚠️ 没有搜索结果")
        print(f"\n  原始响应: {json.dumps(body, ensure_ascii=False, indent=2)[:2000]}")
        return

    for i, item in enumerate(results):
        fields = item.get("fields", item)
        score = item.get("score", item.get("_score", "N/A"))
        chunk_text = fields.get("chunk_text", "")
        title = fields.get("title", "")
        section = fields.get("section_title", "")
        doc_id = fields.get("doc_id", "")
        category = fields.get("category_l1", "")

        print(f"  [{i+1}] Score: {score}")
        print(f"      文档: {title} ({doc_id})")
        if section:
            print(f"      章节: {section}")
        if category:
            print(f"      分类: {category}")
        print(f"      内容: {chunk_text[:200]}...")
        print()


if __name__ == "__main__":
    main()

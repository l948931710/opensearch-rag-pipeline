# -*- coding: utf-8 -*-
"""
test_local_retrieval_production.py — 本地 OpenSearch 检索测试（对齐线上策略）

策略对齐：
  1. Dense KNN + BM25 Hybrid（线上: Dense+Sparse+BM25 via HA3 RRF）
     本地: OpenSearch script_score(cosineSimilarity) + match(BM25) 加权融合
  2. 封面降权（短文本+无section降权）
  3. 邻居拼接（内存版 ±1 window）

用法:
  SIMULATE_API=false python tests/test_local_retrieval_production.py
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opensearch_pipeline.config import get_config
from opensearch_pipeline.retriever import get_query_embedding


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

INDEX_NAME = "fuling_knowledge_v1"
TOP_K = 7
STITCH_WINDOW = 1
DENSE_WEIGHT = 0.50
BM25_WEIGHT = 0.50  # 线上 sparse 由 HA3 内部处理，本地用 BM25 补偿
COVER_MAX_LEN = 200

TEST_QUERIES = [
    {
        "query": "奶茶杯测水试验的操作步骤",
        "expected_doc": "DOC_IMG_DOCX_001",
        "description": "应命中奶茶杯文档的文本或图片 chunk",
    },
    {
        "query": "电脑安装操作步骤",
        "expected_doc": "DOC_IMG_PDF_001",
        "description": "应命中电脑安装文档",
    },
    {
        "query": "奶茶杯测水试验的工具有哪些",
        "expected_doc": "DOC_IMG_DOCX_001",
        "description": "操作工具描述 — 可能在文本 chunk 里",
    },
    {
        "query": "电脑主板CPU安装方法",
        "expected_doc": "DOC_IMG_PDF_001",
        "description": "CPU安装步骤 — 可能在图片或文本 chunk",
    },
    {
        "query": "硬盘安装固定螺丝",
        "expected_doc": "DOC_IMG_PDF_001",
        "description": "硬盘安装 — 图片 chunk 中的 visual_summary 可能描述",
    },
]


def get_opensearch_client():
    from opensearchpy import OpenSearch
    config = get_config()
    os_cfg = config.opensearch
    auth = (os_cfg.auth_user, os_cfg.auth_password) if os_cfg.auth_user and os_cfg.auth_password else None
    return OpenSearch(
        hosts=[{"host": os_cfg.host, "port": os_cfg.port}],
        http_compress=True,
        http_auth=auth,
        use_ssl=os_cfg.use_ssl,
        verify_certs=os_cfg.verify_certs,
        ssl_assert_hostname=False,
        ssl_show_warn=False,
    )


def hybrid_search(client, query: str, dense_vector: list, top_k: int = TOP_K):
    """
    对齐线上策略的本地 Hybrid Search:
    - Sub-query 1: KNN (Dense knn_vector)
    - Sub-query 2: BM25 (match on chunk_text)
    - 融合: bool should 组合 + boosting 控制权重
    """
    body = {
        "size": top_k * 2,  # over-fetch for cover demotion
        "_source": [
            "doc_id", "chunk_text", "chunk_type", "title", "section_title",
            "chunk_index", "page_num", "source_image", "visual_summary",
            "owner_dept", "permission_level",
        ],
        "query": {
            "bool": {
                "should": [
                    {
                        "knn": {
                            "chunk_vector": {
                                "vector": dense_vector,
                                "k": top_k * 2,
                            }
                        }
                    },
                    {
                        "match": {
                            "chunk_text": {
                                "query": query,
                                "boost": BM25_WEIGHT / DENSE_WEIGHT,
                            }
                        }
                    },
                ],
            }
        },
    }

    resp = client.search(index=INDEX_NAME, body=body)
    return resp["hits"]["hits"]


def cover_demotion(hits):
    """封面降权: 短文本+无 section_title 的排后面（图片 chunk 除外）"""
    content_hits = []
    cover_hits = []
    for h in hits:
        src = h["_source"]
        text = src.get("chunk_text", "")
        has_section = bool(src.get("section_title"))
        chunk_type = src.get("chunk_type", "")
        # 图片 chunk 不参与封面降权（它们天然短文本、无 section_title，但有语义价值）
        if chunk_type == "image":
            content_hits.append(h)
        elif not has_section and len(text) < COVER_MAX_LEN:
            cover_hits.append(h)
        else:
            content_hits.append(h)
    return content_hits + cover_hits


def stitch_neighbors(hits, all_hits_by_doc):
    """邻居拼接: 对每个 hit, 拼接同文档的 ±1 chunk（按 _id 去重）"""
    stitched = []
    seen = set()
    for h in hits:
        os_id = h.get("_id", "")
        if os_id in seen:
            continue
        seen.add(os_id)

        src = h["_source"]
        doc_id = src.get("doc_id", "")
        chunk_type = src.get("chunk_type", "")

        # 图片 chunk 不参与邻居拼接（独立语义单元）
        if chunk_type == "image":
            stitched.append(h)
            continue

        # 文本 chunk: 尝试拼接同文档邻居
        center_idx = src.get("chunk_index", -1)
        neighbors = []
        if center_idx >= 0 and doc_id in all_hits_by_doc:
            for nb in all_hits_by_doc[doc_id]:
                nb_idx = nb["_source"].get("chunk_index", -1)
                if nb_idx >= 0 and abs(nb_idx - center_idx) <= STITCH_WINDOW:
                    neighbors.append(nb)
            neighbors.sort(key=lambda x: x["_source"].get("chunk_index", 0))

        if neighbors:
            stitched_text = "\n".join(nb["_source"].get("chunk_text", "") for nb in neighbors)
        else:
            stitched_text = src.get("chunk_text", "")

        result = dict(h)
        result["_source"] = dict(src)
        result["_source"]["chunk_text"] = stitched_text
        result["_source"]["_stitched"] = True
        result["_source"]["_neighbor_count"] = len(neighbors)
        stitched.append(result)
    return stitched


def fetch_all_chunks(client):
    """获取索引中所有 chunk, 按 doc_id 分组"""
    body = {"size": 1000, "query": {"match_all": {}}, "_source": ["doc_id", "chunk_text", "chunk_index", "chunk_type", "section_title", "page_num"]}
    resp = client.search(index=INDEX_NAME, body=body)
    by_doc = {}
    for h in resp["hits"]["hits"]:
        doc_id = h["_source"].get("doc_id", "")
        by_doc.setdefault(doc_id, []).append(h)
    return by_doc


def main():
    print("=" * 80)
    print("  本地 OpenSearch 检索测试 — 对齐线上策略 (Dense+BM25 Hybrid + 封面降权 + 邻居拼接)")
    print("=" * 80)

    config = get_config()
    simulate_api = os.environ.get("SIMULATE_API", "true").lower() == "true"

    # 1. 连接
    client = get_opensearch_client()
    info = client.info()
    print(f"\n📡 OpenSearch {info['version']['number']} @ localhost")

    # 检查索引
    if not client.indices.exists(index=INDEX_NAME):
        print(f"❌ 索引 {INDEX_NAME} 不存在。请先运行 pipeline DAG 1→2→3")
        return

    count = client.count(index=INDEX_NAME)["count"]
    print(f"📊 索引 {INDEX_NAME}: {count} chunks")

    # 预加载所有 chunk 用于邻居拼接
    all_hits_by_doc = fetch_all_chunks(client)
    total_text = sum(1 for docs in all_hits_by_doc.values() for d in docs if d["_source"].get("chunk_type") == "text_chunk")
    total_image = sum(1 for docs in all_hits_by_doc.values() for d in docs if d["_source"].get("chunk_type") == "image")
    print(f"   文本 chunks: {total_text}, 图片 chunks: {total_image}")

    # Embedding 缓存
    cache_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scratch", "embedding_cache.json")
    cache = {}
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            cache = json.load(f)

    def get_cached_query_embedding(query_text):
        model = config.embedding.model
        h = hashlib.md5(f"{model}_{query_text}".encode("utf-8")).hexdigest()
        sp_h = f"sp_{h}"
        if h in cache:
            return cache[h], cache.get(sp_h, {}), True
        # 调真实 API
        dense, sp_idx, sp_val = get_query_embedding(query_text)
        cache[h] = dense
        if sp_idx:
            cache[sp_h] = {"indices": sp_idx, "values": sp_val}
        # Save
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        return dense, {"indices": sp_idx, "values": sp_val}, False

    # 2. 检索测试
    print(f"\n{'═' * 80}")
    print(f"  🔎 检索测试 (Dense={DENSE_WEIGHT}, BM25={BM25_WEIGHT}, top_k={TOP_K}, stitch=±{STITCH_WINDOW})")
    print(f"{'═' * 80}")

    total_queries = len(TEST_QUERIES)
    correct_top1 = 0
    correct_top3 = 0
    correct_top5 = 0
    image_in_top5 = 0

    for qi, q_info in enumerate(TEST_QUERIES, 1):
        query = q_info["query"]
        expected_doc = q_info["expected_doc"]

        # Get query embedding
        dense, sparse_data, from_cache = get_cached_query_embedding(query)
        cache_tag = "cache" if from_cache else "API"

        print(f"\n  Q{qi}: {query}")
        print(f"      期望: {expected_doc} | embedding: {cache_tag}")

        # Hybrid search
        raw_hits = hybrid_search(client, query, dense)

        # Cover demotion
        demoted = cover_demotion(raw_hits)

        # Stitch neighbors
        final_hits = stitch_neighbors(demoted[:TOP_K], all_hits_by_doc)

        # Results
        for i, h in enumerate(final_hits[:5]):
            src = h["_source"]
            doc_id = src.get("doc_id", "?")
            chunk_type = src.get("chunk_type", "?")
            score = h.get("_score", 0)
            text_preview = src.get("chunk_text", "")[:80].replace("\n", " ")
            page = src.get("page_num", "?")
            icon = "🖼️" if chunk_type == "image" else "📝"
            match_tag = "✅" if doc_id == expected_doc else "  "
            nb_count = src.get("_neighbor_count", 0)
            stitch_tag = f" [+{nb_count-1}nb]" if nb_count > 1 else ""

            print(f"      {match_tag} #{i+1} {icon} [{chunk_type}] score={score:.4f} doc={doc_id} page={page}{stitch_tag}")
            print(f"           {text_preview}")

        # Score
        top_docs = [h["_source"].get("doc_id") for h in final_hits[:5]]
        top_types = [h["_source"].get("chunk_type") for h in final_hits[:5]]

        if top_docs and top_docs[0] == expected_doc:
            correct_top1 += 1
        if expected_doc in top_docs[:3]:
            correct_top3 += 1
        if expected_doc in top_docs[:5]:
            correct_top5 += 1
        if "image" in top_types[:5]:
            image_in_top5 += 1

    # Summary
    print(f"\n{'═' * 80}")
    print(f"  📊 检索结果汇总")
    print(f"{'═' * 80}")
    print(f"  Top-1 命中率: {correct_top1}/{total_queries} ({correct_top1/total_queries*100:.0f}%)")
    print(f"  Top-3 命中率: {correct_top3}/{total_queries} ({correct_top3/total_queries*100:.0f}%)")
    print(f"  Top-5 命中率: {correct_top5}/{total_queries} ({correct_top5/total_queries*100:.0f}%)")
    print(f"  图片 chunk 出现在 Top-5 的 query 数: {image_in_top5}/{total_queries}")
    print(f"\n  策略: Dense({DENSE_WEIGHT}) + BM25({BM25_WEIGHT}) + 封面降权 + 邻居拼接(±{STITCH_WINDOW})")
    print(f"  对齐线上: retrieve_and_enrich(top_k={TOP_K}, stitch_window={STITCH_WINDOW})")
    print(f"{'═' * 80}")


if __name__ == "__main__":
    main()

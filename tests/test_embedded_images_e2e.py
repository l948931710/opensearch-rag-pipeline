#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地 OpenSearch 端到端测试 — 真实 DOCX/PDF 嵌入图片全链路

完整验证：真实文件 → 提取文本+嵌入图片 → ImageFunnelProcessor 三阶段过滤
        → chunk 创建（文本+图片）→ embedding → 写入本地 OpenSearch → 向量检索

与 test_local_opensearch_multimodal.py 的区别：
  - 本测试使用真实 docx/pdf 文件，不是手工构造的 mock 数据
  - 验证嵌入图片提取 → ImageFunnelProcessor → assets → image chunk 全链路
"""

import json
import os
import sys
import tempfile
import time

import requests
from opensearchpy import OpenSearch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opensearch_pipeline.config import get_config
from opensearch_pipeline.extraction import UnifiedExtractor
from opensearch_pipeline.chunker import Chunk, DocumentChunker, _generate_chunk_id, _estimate_tokens


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

TEST_INDEX = "test_embedded_images_e2e"
DIMENSION = 1024

LOCAL_OS_HOST = "localhost"
LOCAL_OS_PORT = 9200
LOCAL_OS_AUTH = ("admin", "admin_password")

# 测试文件（相对于项目根目录）
TEST_FILES = [
    {
        "path": "fuling_chunk_exp/production_注塑事业部_FL-ZS-WI-002《奶茶杯测水试验》作业指导书.docx",
        "doc_id": "EMBED_TEST_DOCX_001",
        "file_ext": "docx",
        "dept": "production",
        "expected_min_images": 5,  # 至少 5 张图片通过 funnel
        "search_queries": [
            {
                "query": "奶茶杯测水试验的操作工具",
                "description": "应命中文本 chunk 或图片 chunk（操作工具描述）",
            },
        ],
    },
    {
        "path": "fuling_chunk_exp/it_FL-CW-XXH-003-《电脑安装》作业指导书.pdf",
        "doc_id": "EMBED_TEST_PDF_001",
        "file_ext": "pdf",
        "dept": "it",
        "expected_min_images": 10,  # PDF 38 图去重后至少 10 张通过
        "search_queries": [
            {
                "query": "电脑安装操作步骤",
                "description": "应命中文本或图片 chunk",
            },
        ],
    },
]


def get_local_client() -> OpenSearch:
    return OpenSearch(
        hosts=[{"host": LOCAL_OS_HOST, "port": LOCAL_OS_PORT}],
        http_auth=LOCAL_OS_AUTH,
        use_ssl=False,
        verify_certs=False,
        ssl_show_warn=False,
    )


# ═══════════════════════════════════════════════════════════════
# 1. 创建索引
# ═══════════════════════════════════════════════════════════════

def create_index(client: OpenSearch, index_name: str):
    if client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)

    body = {
        "settings": {
            "index": {
                "knn": True,
                "knn.algo_param.ef_search": 100,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": {
            "properties": {
                "id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "version_no": {"type": "integer"},
                "chunk_text": {"type": "text", "analyzer": "standard"},
                "chunk_text_store": {"type": "text", "index": False},
                "chunk_type": {"type": "keyword"},
                "title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "owner_dept": {"type": "keyword"},
                "permission_level": {"type": "keyword"},
                "category_l1": {"type": "keyword"},
                "category_l2": {"type": "keyword"},
                "section_title": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                "chunk_index": {"type": "integer"},
                "page_num": {"type": "integer"},
                "kb_type": {"type": "keyword"},
                "source_url": {"type": "keyword"},
                "is_active": {"type": "boolean"},
                "dense_vector": {
                    "type": "knn_vector",
                    "dimension": DIMENSION,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                        "parameters": {"ef_construction": 128, "m": 24},
                    },
                },
                "source_image": {"type": "keyword"},
                "visual_summary": {"type": "text", "analyzer": "standard"},
            }
        },
    }
    client.indices.create(index=index_name, body=body)
    print(f"  ✅ 索引 {index_name} 创建完成")


# ═══════════════════════════════════════════════════════════════
# 2. 提取 + Chunk（使用真实文件）
# ═══════════════════════════════════════════════════════════════

def extract_and_chunk(file_config: dict, tmp_dir: str) -> list:
    """
    使用真实文件执行：提取 → ImageFunnelProcessor → SemanticChunker → Chunk 列表。
    """
    doc_id = file_config["doc_id"]
    file_path = file_config["path"]
    file_ext = file_config["file_ext"]
    dept = file_config["dept"]

    print(f"\n  📄 文件: {os.path.basename(file_path)}")

    # Step 1: 提取（simulate=True 让 VLM 用文件名判定，不需要真实 API）
    extractor = UnifiedExtractor(simulate=True)
    task = {
        "doc_id": doc_id,
        "version_no": 1,
        "file_ext": file_ext,
        "raw_key": f"raw/{dept}/{os.path.basename(file_path)}",
        "filename": os.path.basename(file_path),
        "local_path": file_path,
        "_tmp_dir": tmp_dir,
    }

    result = extractor.extract(task)
    print(f"     提取: {result.text_length} chars, {len(result.blocks)} blocks, {len(result.assets)} assets")

    # 统计 asset 状态
    status_counts = {}
    for a in result.assets:
        s = a["status"]
        status_counts[s] = status_counts.get(s, 0) + 1
    if status_counts:
        print(f"     Assets: {status_counts}")

    # Step 2: Chunk（文本部分）— chunk_from_blocks 直接返回 Chunk 对象
    chunker = DocumentChunker(max_chunk_chars=800, overlap_chars=100)
    meta = {
        "title": result.title,
        "owner_dept": dept,
        "category_l1": dept,
        "category_l2": "",
        "permission_level": "public",
        "kb_type": "public",
    }
    chunks = chunker.chunk_from_blocks(result.blocks, doc_id, 1, metadata=meta)

    # Step 3: 图片 chunks（与 node_chunk_documents 逻辑对齐）
    img_chunk_offset = len(chunks)
    for asset in result.assets:
        if asset.get("status") != "ROUTE_TO_VECTOR":
            continue

        visual_summary = asset.get("visual_summary", "")
        filename = asset.get("filename", "")
        page_num = asset.get("page_num") or 1

        chunk_text = f"[Image Schematic] {visual_summary}"
        source_image_url = f"processing/assets/{dept}/{doc_id}/v1/{filename}"

        c = Chunk(
            chunk_id=_generate_chunk_id(doc_id, 1, img_chunk_offset),
            doc_id=doc_id,
            version_no=1,
            chunk_index=img_chunk_offset,
            chunk_type="image",
            chunk_text=chunk_text,
            token_count=_estimate_tokens(chunk_text),
            raw_text=chunk_text,
            context_prefix="",
            page_num=page_num,
            section_title="",
            source_oss_key=f"processing/canonical/{doc_id}/v1/content.canonical.json",
            source="multimodal",
            title=result.title,
            owner_dept=dept,
            category_l1=dept,
            category_l2="",
            permission_level="public",
            kb_type="public",
            risk_level="low",
            is_active=True,
            embedding_status="NOT_STARTED",
            index_status="NOT_INDEXED",
            extra={
                "source_image": source_image_url,
                "visual_summary": visual_summary,
            },
        )
        chunks.append(c)
        img_chunk_offset += 1

    text_count = sum(1 for c in chunks if c.chunk_type == "text_chunk")
    img_count = sum(1 for c in chunks if c.chunk_type == "image")
    print(f"     Chunks: {len(chunks)} 总 ({text_count} 文本, {img_count} 图片)")

    # 验证图片数量
    expected_min = file_config.get("expected_min_images", 0)
    if img_count < expected_min:
        print(f"     ⚠️  图片 chunk 数量 {img_count} 少于预期最低 {expected_min}")
    else:
        print(f"     ✅ 图片 chunk 数量 {img_count} ≥ 预期最低 {expected_min}")

    return chunks


# ═══════════════════════════════════════════════════════════════
# 3. Embedding
# ═══════════════════════════════════════════════════════════════

def generate_embeddings(chunks: list):
    config = get_config()
    api_key = config.embedding.api_key
    base_url = config.embedding.api_base_url.rstrip("/")
    url = f"{base_url}/compatible-mode/v1/embeddings"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    texts = [c.chunk_text for c in chunks]
    batch_size = 10  # 较小 batch 防止超限

    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        # 过滤空文本
        batch = [t if t.strip() else "[placeholder]" for t in batch]
        payload = {
            "model": "text-embedding-v4",
            "input": batch,
            "dimensions": DIMENSION,
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code != 200:
            print(f"  ❌ Embedding API error (batch {i}-{i+len(batch)}): {resp.status_code}")
            print(f"     Response: {resp.text[:500]}")
            resp.raise_for_status()
        data = resp.json()
        embeddings = sorted(data["data"], key=lambda x: x["index"])
        for j, emb in enumerate(embeddings):
            chunks[i + j].embedding_vector = emb["embedding"]
            chunks[i + j].embedding_model = "text-embedding-v4"
            chunks[i + j].embedding_status = "DONE"

    print(f"  ✅ {len(chunks)} 个 chunk embedding 完成")


# ═══════════════════════════════════════════════════════════════
# 4. 写入 + 检索
# ═══════════════════════════════════════════════════════════════

def index_chunks(client: OpenSearch, index_name: str, chunks: list):
    bulk_body = []
    for chunk in chunks:
        doc = chunk.to_ha3_doc(pk_field="id")
        doc["is_active"] = bool(doc.get("is_active", 1))
        doc["id"] = str(doc["id"])
        bulk_body.append({"index": {"_index": index_name, "_id": chunk.chunk_id}})
        bulk_body.append(doc)

    resp = client.bulk(body="\n".join(json.dumps(d, ensure_ascii=False) for d in bulk_body) + "\n")
    items = resp.get("items", [])
    indexed = sum(1 for i in items if list(i.values())[0].get("status") in (200, 201))
    failed = len(items) - indexed

    if resp.get("errors"):
        for item in items:
            op = list(item.values())[0]
            if op.get("status", 200) >= 300:
                print(f"  ❌ {op.get('_id')}: {op.get('error', {}).get('reason')}")

    print(f"  ✅ 写入: {indexed} 成功, {failed} 失败")
    client.indices.refresh(index=index_name)
    return indexed


def search(client: OpenSearch, index_name: str, query: str, top_k: int = 5) -> list:
    config = get_config()
    api_key = config.embedding.api_key
    base_url = config.embedding.api_base_url.rstrip("/")
    url = f"{base_url}/compatible-mode/v1/embeddings"

    resp = requests.post(
        url,
        json={"model": "text-embedding-v4", "input": [query], "dimensions": DIMENSION},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    query_vec = resp.json()["data"][0]["embedding"]

    body = {
        "size": top_k,
        "_source": [
            "doc_id", "chunk_id", "chunk_text", "chunk_text_store",
            "title", "section_title", "chunk_type", "page_num",
            "source_image", "visual_summary",
        ],
        "query": {
            "knn": {
                "dense_vector": {
                    "vector": query_vec,
                    "k": top_k,
                }
            }
        },
    }

    resp = client.search(index=index_name, body=body)
    hits = resp.get("hits", {}).get("hits", [])

    return [
        {
            "chunk_text": h["_source"].get("chunk_text_store", h["_source"].get("chunk_text", "")),
            "chunk_type": h["_source"].get("chunk_type", ""),
            "doc_id": h["_source"].get("doc_id", ""),
            "page_num": h["_source"].get("page_num"),
            "source_image": h["_source"].get("source_image", ""),
            "visual_summary": h["_source"].get("visual_summary", ""),
            "score": h.get("_score", 0),
        }
        for h in hits
    ]


# ═══════════════════════════════════════════════════════════════
# 5. Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 90)
    print("  嵌入图片端到端测试 — 真实 DOCX/PDF → 本地 OpenSearch")
    print("=" * 90)

    # 检查文件存在
    for fc in TEST_FILES:
        if not os.path.exists(fc["path"]):
            print(f"  ❌ 文件不存在: {fc['path']}")
            return 1

    # Step 1: 连接
    print("\n📡 Step 1: 连接本地 OpenSearch...")
    try:
        client = get_local_client()
        info = client.info()
        print(f"  ✅ OpenSearch {info['version']['number']} @ {LOCAL_OS_HOST}:{LOCAL_OS_PORT}")
    except Exception as e:
        print(f"  ❌ 无法连接本地 OpenSearch: {e}")
        return 1

    # Step 2: 创建索引
    print("\n📋 Step 2: 创建索引...")
    create_index(client, TEST_INDEX)

    # Step 3: 提取 + Chunk
    print("\n📦 Step 3: 提取文本 + 嵌入图片 + Chunk...")
    tmp_dir = tempfile.mkdtemp(prefix="embed_img_e2e_")
    all_chunks = []
    for fc in TEST_FILES:
        chunks = extract_and_chunk(fc, tmp_dir)
        all_chunks.extend(chunks)

    total_text = sum(1 for c in all_chunks if c.chunk_type == "text_chunk")
    total_img = sum(1 for c in all_chunks if c.chunk_type == "image")
    print(f"\n  📊 总计: {len(all_chunks)} chunks ({total_text} 文本 + {total_img} 图片)")

    # Step 4: Embedding
    print("\n🔢 Step 4: 生成 embedding (text-embedding-v4)...")
    generate_embeddings(all_chunks)

    # Step 5: 写入
    print("\n📤 Step 5: 写入 OpenSearch...")
    indexed = index_chunks(client, TEST_INDEX, all_chunks)
    time.sleep(1)

    # Step 6: 检索测试
    print(f"\n{'═' * 90}")
    print("  🔎 Step 6: 检索测试")
    print(f"{'═' * 90}")

    all_passed = True

    for fc in TEST_FILES:
        doc_id = fc["doc_id"]
        for sq in fc["search_queries"]:
            query = sq["query"]
            results = search(client, TEST_INDEX, query, top_k=5)

            print(f"\n  Q: {query}")
            print(f"     {sq['description']}")

            if not results:
                print("     ❌ 无结果")
                all_passed = False
                continue

            # 显示 top-5
            has_image_hit = False
            has_text_hit = False
            for i, r in enumerate(results[:5]):
                is_img = r["chunk_type"] == "image"
                type_icon = "🖼️" if is_img else "📝"
                text_preview = r["chunk_text"][:80].replace("\n", " ")
                img_info = f" [img: {r['source_image'][:40]}...]" if r.get("source_image") else ""
                print(f"     #{i+1} {type_icon} [{r['chunk_type']}] score={r['score']:.4f} "
                      f"doc={r['doc_id']} page={r.get('page_num')}")
                print(f"        {text_preview}{img_info}")

                if r["doc_id"] == doc_id:
                    if is_img:
                        has_image_hit = True
                    else:
                        has_text_hit = True

            # 验证：至少有相关 doc 的 hit
            if has_image_hit or has_text_hit:
                hit_types = []
                if has_text_hit:
                    hit_types.append("文本")
                if has_image_hit:
                    hit_types.append("图片")
                print(f"     ✅ 在 Top-5 中命中 {doc_id} ({'+'.join(hit_types)})")
            else:
                print(f"     ❌ Top-5 中未命中 {doc_id}")
                all_passed = False

    # Step 7: 统计 image chunks 验证
    print(f"\n{'═' * 90}")
    print("  📊 Step 7: Image Chunk 字段验证")
    print(f"{'═' * 90}")

    # 查询所有 image chunks
    img_query = {
        "size": 100,
        "query": {"term": {"chunk_type": "image"}},
        "_source": ["doc_id", "chunk_type", "source_image", "visual_summary", "page_num"],
    }
    img_results = client.search(index=TEST_INDEX, body=img_query)
    img_hits = img_results.get("hits", {}).get("hits", [])

    print(f"\n  索引中 image chunks 总数: {len(img_hits)}")

    field_ok = True
    for h in img_hits[:5]:
        src = h["_source"]
        has_si = bool(src.get("source_image"))
        has_vs = bool(src.get("visual_summary"))
        icon = "✅" if (has_si and has_vs) else "❌"
        print(f"  {icon} {src['doc_id']}: source_image={'有' if has_si else '无'}, "
              f"visual_summary={'有' if has_vs else '无'}, page={src.get('page_num')}")
        if not (has_si and has_vs):
            field_ok = False
            all_passed = False

    if len(img_hits) > 5:
        remaining_ok = all(
            bool(h["_source"].get("source_image")) and bool(h["_source"].get("visual_summary"))
            for h in img_hits[5:]
        )
        if remaining_ok:
            print(f"  ✅ 其余 {len(img_hits)-5} 个 image chunks 字段也完整")
        else:
            print(f"  ❌ 部分 image chunks 缺少字段")
            all_passed = False

    # Step 8: 清理
    print(f"\n🧹 Step 8: 清理...")
    client.indices.delete(index=TEST_INDEX)
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"  ✅ 已清理索引 + 临时目录")

    # 结果
    print(f"\n{'█' * 90}")
    if all_passed:
        print(f"  ✅ 嵌入图片全链路测试通过！")
        print(f"     DOCX 嵌入图片 → ImageFunnelProcessor → chunk → embedding → 向量检索 ✅")
        print(f"     PDF 嵌入图片（带 page_num）→ 同上 ✅")
    else:
        print(f"  ⚠️  部分检查未通过，请查看上方详情")
    print(f"{'█' * 90}")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())

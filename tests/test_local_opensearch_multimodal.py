#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本地 OpenSearch 端到端多模态测试

目标：在本地 OpenSearch (localhost:9200) 上验证修复后的图片 chunk 全链路：
  chunk 创建 → embedding → 写入 → 向量检索 → 验证 source_image / visual_summary 返回

字段映射严格对齐生产 HA3 表（fuling_kb_chunks），确保测试结论可信。
"""

import json
import math
import os
import sys
import time
import requests
from opensearchpy import OpenSearch

# ── 保证项目模块可导入 ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from opensearch_pipeline.config import get_config
from opensearch_pipeline.chunker import Chunk, _generate_chunk_id, _estimate_tokens


# ═══════════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════════

TEST_INDEX = "test_multimodal_e2e"
DIMENSION = 1024

# 本地 OpenSearch 直连（绕过 HA3 路由）
LOCAL_OS_HOST = "localhost"
LOCAL_OS_PORT = 9200
LOCAL_OS_AUTH = ("admin", "admin_password")


def get_local_client() -> OpenSearch:
    """直连本地 OpenSearch，不走 _get_opensearch_client 的 HA3 路由。"""
    return OpenSearch(
        hosts=[{"host": LOCAL_OS_HOST, "port": LOCAL_OS_PORT}],
        http_auth=LOCAL_OS_AUTH,
        use_ssl=False,
        verify_certs=False,
        ssl_show_warn=False,
    )


# ═══════════════════════════════════════════════════════════════
# 1. 创建索引 — 字段对齐生产 HA3 表
# ═══════════════════════════════════════════════════════════════

def create_production_aligned_index(client: OpenSearch, index_name: str):
    """
    创建与生产 HA3 表 fuling_kb_chunks 字段一一对齐的 OpenSearch 索引。

    生产 HA3 字段列表（来源：to_ha3_doc + 控制台）:
      id (INT64, PK)        → 开源版用 keyword
      doc_id (STRING)
      chunk_id (STRING)
      version_no (INT32)
      chunk_text (TEXT)     → BM25 倒排 + 存储
      chunk_type (STRING)
      title (STRING)
      owner_dept (STRING)
      permission_level (STRING)
      category_l1 (STRING)
      category_l2 (STRING)
      section_title (STRING)
      chunk_index (INT32)
      page_num (INT32)
      kb_type (STRING)
      chunk_text_store (TEXT) → 原文存储，检索返回用
      source_url (STRING)
      is_active (INT8)      → 开源版用 boolean
      dense_vector (VECTOR)  → 开源版用 knn_vector
      sparse_vector_indices → 开源版暂不支持原生 sparse，跳过
      sparse_vector_values  → 同上
      source_image (STRING)  → 🆕 图片 OSS 路径
      visual_summary (TEXT)  → 🆕 VLM 文本描述
    """
    if client.indices.exists(index=index_name):
        client.indices.delete(index=index_name)
        print(f"  🗑️  已删除旧索引 {index_name}")

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
                # ── PK & 关联字段 ──
                "id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "version_no": {"type": "integer"},

                # ── 文本内容 ──
                "chunk_text": {
                    "type": "text",
                    "analyzer": "standard",
                },
                "chunk_text_store": {
                    "type": "text",
                    "index": False,  # 仅存储，不索引
                },

                # ── 分类 & 权限 ──
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

                # ── 向量 ──
                "dense_vector": {
                    "type": "knn_vector",
                    "dimension": DIMENSION,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",  # 与 benchmark 一致
                        "engine": "lucene",
                        "parameters": {"ef_construction": 128, "m": 24},
                    },
                },

                # ── 🆕 多模态字段（对齐 HA3 控制台新增字段）──
                "source_image": {"type": "keyword"},
                "visual_summary": {
                    "type": "text",
                    "analyzer": "standard",
                },
            }
        },
    }

    client.indices.create(index=index_name, body=body)
    print(f"  ✅ 创建索引 {index_name} (对齐生产 HA3 字段)")


# ═══════════════════════════════════════════════════════════════
# 2. 构建测试数据 — 混合文本+图片 chunks
# ═══════════════════════════════════════════════════════════════

def build_test_chunks() -> list:
    """构建模拟真实业务的混合 chunk 数据集。"""

    chunks = []

    # ── 文档 1: 奶茶杯测水试验（含图片）──
    doc1_chunks = [
        {
            "type": "text_chunk",
            "text": "奶茶杯测水试验操作指导书。本作业指导书适用于注塑事业部全体员工。目的：检验奶茶杯的密封性能，确保产品质量。",
            "title": "奶茶杯测水试验操作指导书",
            "section": "封面",
        },
        {
            "type": "text_chunk",
            "text": "步骤一：准备工具。需要准备以下工具：胶带切割器（蓝色）、量杯（500ml）、纯净水、待测奶茶杯（10个一组）。将工具放置在操作台上。",
            "title": "奶茶杯测水试验操作指导书",
            "section": "步骤一 准备工具",
        },
        {
            "type": "text_chunk",
            "text": "步骤二：注水测试。用量杯量取350ml纯净水，缓慢注入奶茶杯中，注意不要溢出。密封杯盖后倒置放置5分钟，观察是否有渗漏。",
            "title": "奶茶杯测水试验操作指导书",
            "section": "步骤二 注水测试",
        },
        {
            "type": "image",
            "text": "[Image Schematic] 蓝色胶带切割器，内装透明胶带，橙色内芯。切割器上有 CAUTION SHARP BLADE 警示文字。置于木质桌面上。",
            "title": "奶茶杯测水试验操作指导书",
            "section": "步骤一 准备工具",
            "source_image": "processing/assets/production_注塑事业部/DOC_WI002/v1/image3.jpeg",
            "visual_summary": "蓝色胶带切割器，内装透明胶带，橙色内芯。切割器上有 CAUTION SHARP BLADE 警示文字。置于木质桌面上。",
        },
    ]

    # ── 文档 2: 注塑原料领用（含图片）──
    doc2_chunks = [
        {
            "type": "text_chunk",
            "text": "注塑原料领用作业指导书。规范注塑车间原材料领用流程，确保生产物料供应及时准确。",
            "title": "注塑原料领用作业指导书",
            "section": "封面",
        },
        {
            "type": "text_chunk",
            "text": "领料流程：生产计划员根据当日生产计划在U8+系统中创建领料申请单。填写存货编码、数量、名称及手册号等物料明细信息。",
            "title": "注塑原料领用作业指导书",
            "section": "领料流程",
        },
        {
            "type": "image",
            "text": "[Image Schematic] 领料申请单界面，显示单据号、日期、部门等信息，包含物料明细如存货编码、数量、名称及手册号。标注了操作按钮与字段编号，流程箭头指示数据关联。",
            "title": "注塑原料领用作业指导书",
            "section": "领料流程",
            "source_image": "processing/assets/production_注塑事业部/DOC_WI003/v1/image10.png",
            "visual_summary": "领料申请单界面，显示单据号、日期、部门等信息，包含物料明细如存货编码、数量、名称及手册号。标注了操作按钮与字段编号，流程箭头指示数据关联。",
        },
    ]

    # ── 文档 3: 工资核算操作手册（含图片）──
    doc3_chunks = [
        {
            "type": "text_chunk",
            "text": "工资核算管理操作手册（2025年5月28日初版）。本手册介绍U8+人力资源模块中工资核算的详细操作步骤。",
            "title": "工资核算管理操作手册",
            "section": "封面",
        },
        {
            "type": "text_chunk",
            "text": "工资单查询：登录U8+系统，进入人力资源→工资管理→成品日工资单。可按部门、日期范围筛选查看员工工资明细，包含人员编号、工种、工时、单价及总金额。",
            "title": "工资核算管理操作手册",
            "section": "工资单查询",
        },
        {
            "type": "image",
            "text": "[Image Schematic] 成品日工资单，显示2025年6月23日注塑事业部的员工工资明细，包括人员编号、工种、工时、单价及总金额等信息。",
            "title": "工资核算管理操作手册",
            "section": "工资单查询",
            "source_image": "processing/assets/it/DOC_IT001/v1/image38.png",
            "visual_summary": "成品日工资单，显示2025年6月23日注塑事业部的员工工资明细，包括人员编号、工种、工时、单价及总金额等信息。",
        },
    ]

    doc_configs = [
        ("DOC_WI002", 1, "production_注塑事业部", doc1_chunks),
        ("DOC_WI003", 1, "production_注塑事业部", doc2_chunks),
        ("DOC_IT001", 1, "it", doc3_chunks),
    ]

    for doc_id, version, dept, chunk_defs in doc_configs:
        for idx, cd in enumerate(chunk_defs):
            chunk_id = _generate_chunk_id(doc_id, version, idx)
            extra = {}
            if cd["type"] == "image":
                extra = {
                    "source_image": cd.get("source_image", ""),
                    "visual_summary": cd.get("visual_summary", ""),
                }

            c = Chunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                version_no=version,
                chunk_index=idx,
                chunk_type=cd["type"],
                chunk_text=cd["text"],
                token_count=_estimate_tokens(cd["text"]),
                raw_text=cd["text"],
                context_prefix="",
                embedding_text=cd["text"],
                page_num=1,
                section_title=cd.get("section", ""),
                source_oss_key=f"processing/canonical/{doc_id}/v{version}/content.canonical.json",
                source="multimodal" if cd["type"] == "image" else "text",
                title=cd.get("title", ""),
                owner_dept=dept,
                category_l1=dept.split("_")[0] if "_" in dept else dept,
                category_l2=dept.split("_")[1] if "_" in dept else "",
                permission_level="public",
                kb_type="public",
                risk_level="low",
                is_active=True,
                embedding_status="NOT_STARTED",
                index_status="NOT_INDEXED",
                extra=extra,
            )
            chunks.append(c)

    return chunks


# ═══════════════════════════════════════════════════════════════
# 3. 调用真实 API 生成 Embedding
# ═══════════════════════════════════════════════════════════════

def generate_real_embeddings(chunks: list):
    """调用 text-embedding-v4 API 为所有 chunks 生成真实向量。"""
    config = get_config()
    api_key = config.embedding.api_key
    base_url = config.embedding.api_base_url.rstrip("/")
    url = f"{base_url}/compatible-mode/v1/embeddings"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    texts = [c.chunk_text for c in chunks]

    # 批量调用（text-embedding-v4 支持批量）
    batch_size = 20
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i : i + batch_size]
        payload = {
            "model": "text-embedding-v4",
            "input": batch_texts,
            "dimensions": DIMENSION,
        }

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        embeddings = sorted(data["data"], key=lambda x: x["index"])
        for j, emb_data in enumerate(embeddings):
            chunk_idx = i + j
            chunks[chunk_idx].embedding_vector = emb_data["embedding"]
            chunks[chunk_idx].embedding_model = "text-embedding-v4"
            chunks[chunk_idx].embedding_status = "DONE"

    print(f"  ✅ {len(chunks)} 个 chunk 向量生成完成 (text-embedding-v4, {DIMENSION}维)")


# ═══════════════════════════════════════════════════════════════
# 4. 写入本地 OpenSearch — 使用 to_ha3_doc 格式
# ═══════════════════════════════════════════════════════════════

def index_chunks(client: OpenSearch, index_name: str, chunks: list):
    """
    使用 to_ha3_doc() 输出写入 OpenSearch，验证序列化逻辑。

    注意：HA3 用 `dense_vector`，开源 OpenSearch 也用 `dense_vector`（mapping 已对齐）。
    """
    bulk_body = []
    for chunk in chunks:
        # 使用 to_ha3_doc 保证字段与生产一致
        doc = chunk.to_ha3_doc(pk_field="id")

        # HA3 的 is_active 是 int(0/1)，开源版用 boolean
        doc["is_active"] = bool(doc.get("is_active", 1))

        # HA3 的 PK 是 INT64，开源版用 keyword string
        doc["id"] = str(doc["id"])

        bulk_body.append({"index": {"_index": index_name, "_id": chunk.chunk_id}})
        bulk_body.append(doc)

    resp = client.bulk(body="\n".join(json.dumps(d, ensure_ascii=False) for d in bulk_body) + "\n")
    errors = resp.get("errors", False)
    items = resp.get("items", [])

    indexed = sum(1 for i in items if list(i.values())[0].get("status") in (200, 201))
    failed = len(items) - indexed

    if errors:
        for item in items:
            op = list(item.values())[0]
            if op.get("status", 200) >= 300:
                print(f"  ❌ {op.get('_id', '?')}: {op.get('error', {}).get('reason', 'unknown')}")

    print(f"  ✅ 写入完成: {indexed} 成功, {failed} 失败")

    # 刷新索引使数据可搜索
    client.indices.refresh(index=index_name)
    return indexed


# ═══════════════════════════════════════════════════════════════
# 5. 向量检索 — 对齐生产 retriever 的 output_fields
# ═══════════════════════════════════════════════════════════════

def search_local(
    client: OpenSearch,
    index_name: str,
    query: str,
    top_k: int = 5,
) -> list:
    """
    使用本地 OpenSearch KNN + BM25 混合检索。
    output_fields 对齐生产 retriever.py L235-238。
    """
    config = get_config()
    api_key = config.embedding.api_key
    base_url = config.embedding.api_base_url.rstrip("/")

    # 1. 生成 query 向量
    url = f"{base_url}/compatible-mode/v1/embeddings"
    resp = requests.post(
        url,
        json={"model": "text-embedding-v4", "input": [query], "dimensions": DIMENSION},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    query_vec = resp.json()["data"][0]["embedding"]

    # 2. KNN 检索（对齐生产的 cosine 距离）
    body = {
        "size": top_k,
        "_source": [
            # ── 对齐生产 retriever.py output_fields ──
            "id", "doc_id", "chunk_id", "chunk_text", "chunk_text_store",
            "title", "section_title", "category_l1", "chunk_index",
            "page_num", "kb_type", "permission_level", "owner_dept",
            "chunk_type",
            # ── 🆕 多模态字段 ──
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

    results = []
    for hit in hits:
        src = hit.get("_source", {})
        results.append({
            "chunk_text": src.get("chunk_text_store", src.get("chunk_text", "")),
            "title": src.get("title", ""),
            "section_title": src.get("section_title", ""),
            "doc_id": src.get("doc_id", ""),
            "chunk_type": src.get("chunk_type", ""),
            "chunk_index": src.get("chunk_index", 0),
            "score": hit.get("_score", 0),
            # 🆕 多模态
            "source_image": src.get("source_image", ""),
            "visual_summary": src.get("visual_summary", ""),
        })

    return results


# ═══════════════════════════════════════════════════════════════
# 6. 测试用例
# ═══════════════════════════════════════════════════════════════

def run_tests(client: OpenSearch, index_name: str):
    """运行检索测试用例，验证图片 chunk 被正确召回。"""

    test_queries = [
        {
            "query": "胶带切割器上有什么警示文字？",
            "expect_type": "image",
            "expect_doc": "DOC_WI002",
            "expect_has_source_image": True,
            "expect_has_visual_summary": True,
            "description": "应命中图片 chunk（胶带切割器实拍）",
        },
        {
            "query": "领料申请单界面有哪些字段？",
            "expect_type": "image",
            "expect_doc": "DOC_WI003",
            "expect_has_source_image": True,
            "expect_has_visual_summary": True,
            "description": "应命中图片 chunk（申请单截图）",
        },
        {
            "query": "成品日工资单是哪个部门的？",
            "expect_type": "image",
            "expect_doc": "DOC_IT001",
            "expect_has_source_image": True,
            "expect_has_visual_summary": True,
            "description": "应命中图片 chunk（工资单截图）",
        },
        {
            "query": "奶茶杯注水测试怎么操作？",
            "expect_type": "text_chunk",
            "expect_doc": "DOC_WI002",
            "expect_has_source_image": False,
            "expect_has_visual_summary": False,
            "description": "应命中文本 chunk（注水测试步骤）",
        },
        {
            "query": "U8+系统如何查看员工工资明细？",
            "expect_type": "text_chunk",
            "expect_doc": "DOC_IT001",
            "expect_has_source_image": False,
            "expect_has_visual_summary": False,
            "description": "应命中文本 chunk（工资单查询步骤）",
        },
    ]

    print(f"\n{'═' * 90}")
    print(f"  检索测试（{len(test_queries)} 个 query）")
    print(f"{'═' * 90}")

    passed = 0
    failed = 0

    for i, tc in enumerate(test_queries):
        results = search_local(client, index_name, tc["query"], top_k=3)

        if not results:
            print(f"\n  ❌ Q{i+1}: {tc['query']}")
            print(f"     预期: {tc['description']}")
            print(f"     结果: 无结果返回")
            failed += 1
            continue

        top = results[0]
        checks = []

        # Check 1: Top-1 doc_id
        doc_ok = top["doc_id"] == tc["expect_doc"]
        checks.append(("doc_id", doc_ok, f"{top['doc_id']} (expect {tc['expect_doc']})"))

        # Check 2: chunk_type
        type_ok = top["chunk_type"] == tc["expect_type"]
        checks.append(("chunk_type", type_ok, f"{top['chunk_type']} (expect {tc['expect_type']})"))

        # Check 3: source_image
        has_si = bool(top.get("source_image"))
        si_ok = has_si == tc["expect_has_source_image"]
        checks.append(("source_image", si_ok, f"{'有' if has_si else '无'} (expect {'有' if tc['expect_has_source_image'] else '无'})"))

        # Check 4: visual_summary
        has_vs = bool(top.get("visual_summary"))
        vs_ok = has_vs == tc["expect_has_visual_summary"]
        checks.append(("visual_summary", vs_ok, f"{'有' if has_vs else '无'} (expect {'有' if tc['expect_has_visual_summary'] else '无'})"))

        all_ok = all(c[1] for c in checks)

        icon = "✅" if all_ok else "❌"
        print(f"\n  {icon} Q{i+1}: {tc['query']}")
        print(f"     {tc['description']}")
        print(f"     Top-1: [{top['chunk_type']}] {top['chunk_text'][:60]}... (score={top['score']:.4f})")

        for name, ok, detail in checks:
            check_icon = "✓" if ok else "✗"
            print(f"       {check_icon} {name}: {detail}")

        if all_ok:
            passed += 1
        else:
            failed += 1

        # 打印 top-3 完整结果
        if len(results) > 1:
            for j, r in enumerate(results[1:3], 2):
                print(f"     #{j}: [{r['chunk_type']}] {r['chunk_text'][:50]}... (score={r['score']:.4f})")

    print(f"\n{'─' * 90}")
    print(f"  通过: {passed}/{len(test_queries)}  失败: {failed}/{len(test_queries)}")
    print(f"{'═' * 90}")

    return passed, failed


# ═══════════════════════════════════════════════════════════════
# 7. Main
# ═══════════════════════════════════════════════════════════════

def main():
    print("=" * 90)
    print("  本地 OpenSearch 多模态端到端测试")
    print("  字段映射对齐生产 HA3 表 fuling_kb_chunks")
    print("=" * 90)

    # Step 1: 连接本地 OpenSearch
    print("\n📡 Step 1: 连接本地 OpenSearch...")
    client = get_local_client()
    info = client.info()
    print(f"  ✅ OpenSearch {info['version']['number']} @ {LOCAL_OS_HOST}:{LOCAL_OS_PORT}")

    # Step 2: 创建生产对齐索引
    print("\n📋 Step 2: 创建生产对齐索引...")
    create_production_aligned_index(client, TEST_INDEX)

    # Step 3: 构建测试数据
    print("\n📦 Step 3: 构建测试 chunks...")
    chunks = build_test_chunks()
    text_chunks = [c for c in chunks if c.chunk_type != "image"]
    image_chunks = [c for c in chunks if c.chunk_type == "image"]
    print(f"  ✅ {len(chunks)} 个 chunks (文本: {len(text_chunks)}, 图片: {len(image_chunks)})")

    # Step 4: 生成真实 embedding
    print("\n🔢 Step 4: 生成真实 embedding (text-embedding-v4)...")
    generate_real_embeddings(chunks)

    # Step 5: 验证 to_ha3_doc 序列化
    print("\n🔍 Step 5: 验证 to_ha3_doc 序列化...")
    for c in image_chunks:
        doc = c.to_ha3_doc()
        assert "source_image" in doc, f"FAIL: {c.chunk_id} 缺少 source_image"
        assert "visual_summary" in doc, f"FAIL: {c.chunk_id} 缺少 visual_summary"
        assert "dense_vector" in doc, f"FAIL: {c.chunk_id} 缺少 dense_vector"
        assert len(doc["dense_vector"]) == DIMENSION, f"FAIL: {c.chunk_id} 向量维度错误"
        print(f"  ✅ {c.chunk_id}: source_image + visual_summary + dense_vector({DIMENSION})")

    for c in text_chunks:
        doc = c.to_ha3_doc()
        assert "source_image" not in doc, f"FAIL: text chunk {c.chunk_id} 不应有 source_image"
        assert "visual_summary" not in doc, f"FAIL: text chunk {c.chunk_id} 不应有 visual_summary"
    print(f"  ✅ 文本 chunks 无多模态字段（正确）")

    # Step 6: 写入 OpenSearch
    print("\n📤 Step 6: 写入 OpenSearch...")
    indexed = index_chunks(client, TEST_INDEX, chunks)
    assert indexed == len(chunks), f"FAIL: 预期 {len(chunks)} 个写入成功, 实际 {indexed}"

    # Step 7: 等待索引刷新
    time.sleep(1)

    # Step 8: 检索测试
    print("\n🔎 Step 7: 检索测试...")
    passed, failed = run_tests(client, TEST_INDEX)

    # Step 8: 清理
    print(f"\n🧹 Step 8: 清理测试索引...")
    client.indices.delete(index=TEST_INDEX)
    print(f"  ✅ 已删除 {TEST_INDEX}")

    # 最终结果
    print(f"\n{'█' * 90}")
    if failed == 0:
        print(f"  ✅ 全部 {passed} 个测试通过！多模态修复验证成功。")
    else:
        print(f"  ❌ {failed} 个测试失败，请检查。")
    print(f"{'█' * 90}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
